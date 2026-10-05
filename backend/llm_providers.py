"""
Слой доступа к нейросетям.

Изменение по сравнению с первой версией: провайдер больше не "жёстко прошитый
класс с одним набором настроек", а строка в таблице llm_models (см. db.py) —
пользователь сам добавляет, редактирует и включает/выключает модели на
вкладке "Настройки", в любом количестве и с любыми ключами (бесплатными или
платными). provider_type определяет только ПРОТОКОЛ обращения к API:

    openai_compat - любой OpenAI-совместимый /chat/completions (OpenRouter,
                    Groq, Together, локальный vLLM и т.п. - base_url свой)
    gemini        - Google Generative Language API
    ollama        - локальный /api/chat

Автоматическое переключение: generate_proposal_content пробует модели по
очереди в порядке priority (по возрастанию), пока одна не ответит успешно.
Если у модели закончился бесплатный лимит (HTTP 429 и т.п.) или любая другая
ошибка - это фиксируется в списке "попыток" и мы переходим к следующей
включённой модели. Если ответа не дала ни одна - поднимается ошибка со
сводкой по каждой модели.

ВАЖНО (аудит 2026-10-03): HTTP-слой переведён с синхронного urllib на
httpx.AsyncClient. Причина - синхронная генерация держала поток gunicorn-
воркера занятым на всё время перебора моделей (до ~90 сек на попытку x 3
попытки эскалации x N моделей в цепочке - в худшем случае десятки минут),
и при малом числе воркеров это могло затормозить вообще все остальные
запросы к приложению (проверку моделей, работу с авторами и т.п.). Формат
запроса/ответа у каждого провайдера (openai_compat/ollama/gemini) оставлен
БЕЗ ИЗМЕНЕНИЙ - менялся только транспорт, а не то, что и как отправляется.
"""
import asyncio
import json
import os
import re
import time

import httpx

import db
import text_clean


class ProviderError(Exception):
    """
    message - короткое, всегда читаемое объяснение (видно сразу в таблице/
    прогресс-баре). raw_response - ПОЛНЫЙ, ничем не обрезанный текст ответа
    провайдера, если он был получен (например, ответ похож на JSON, но не
    разбирается, или в нём не хватает ожидаемых полей) - сохраняется в
    журнал событий модели целиком и доступен для скачивания, а не обрезается
    до нескольких сотен символов "на глаз". is_truncation - специально
    помечает случай "ответ обрезан по max_tokens" (запрос прошёл, но не
    хватило места дописать), чтобы вызывающий код мог сам повторить попытку
    с увеличенным лимитом. is_too_large / detected_max_tokens - обратный
    случай: провайдер вообще ОТКАЗАЛСЯ принимать запрос, потому что
    запрошенный max_tokens больше, чем модель поддерживает - тут эскалация
    "просто увеличить ещё" не поможет, но если провайдер прислал точный
    предел, есть смысл повторить попытку именно с ним. retry_after -
    заполняется только для HTTP 429 с заголовком Retry-After: небольшое
    известное время ожидания, после которого стоит повторить ТОТ ЖЕ запрос
    (многие лимиты частоты запросов снимаются за секунды).
    """

    def __init__(self, message: str, raw_response: str = None, is_truncation: bool = False,
                 is_too_large: bool = False, detected_max_tokens: int = None):
        super().__init__(message)
        self.raw_response = raw_response
        self.is_truncation = is_truncation
        self.is_too_large = is_too_large
        self.detected_max_tokens = detected_max_tokens
        self.retry_after = None


# Реальная генерация документа может честно занимать больше времени (крупный
# ответ, загруженный бесплатный сервис) - для неё используем длинный таймаут.
# А вот проверка "жива ли модель" ("проверить" / "Пинг всех") не должна
# заставлять пользователя ждать почти минуту-две на одну зависшую модель -
# для неё используем короткий таймаут, чтобы кнопка не казалась "зависшей".
# 240 сек (раньше 90): русский текст шести разделов - это примерно 4-6 тыс.
# токенов, и медленная бесплатная модель могла просто не успеть дописать ответ
# за 90 сек. Общий потолок всей генерации (GENERATION_DEADLINE_SECONDS ниже)
# от этого не меняется. Значение можно переопределить переменной окружения.
GENERATION_TIMEOUT = int(os.environ.get("GENERATION_TIMEOUT_SECONDS", "240"))
CHECK_TIMEOUT = 20

# Общий потолок на ВЕСЬ перебор моделей/эскалаций одной генерации - раньше
# такого потолка не было вовсе, и в худшем случае (несколько моделей в
# цепочке, у каждой по 3 попытки эскалации по 90 сек) пользователь мог ждать
# результат десятками минут, прежде чем увидеть финальную ошибку. Теперь
# после этого времени перебор останавливается с понятным сообщением -
# значение можно переопределить переменной окружения, если у конкретной
# установки осознанно длинная цепочка медленных локальных моделей.
GENERATION_DEADLINE_SECONDS = int(os.environ.get("GENERATION_DEADLINE_SECONDS", "600"))

# Сколько секунд готовы подождать по заголовку Retry-After при HTTP 429,
# прежде чем просто сдаться и перейти к следующей попытке/модели - многие
# лимиты частоты запросов (не дневной лимит!) снимаются за считаные секунды,
# и короткое ожидание экономит дневной лимит следующей модели в цепочке.
RATE_LIMIT_MAX_WAIT = 15


async def _http_post_json_async(client: httpx.AsyncClient, url, payload, headers=None, timeout=GENERATION_TIMEOUT):
    req_headers = {
        "Content-Type": "application/json",
        # Без явного User-Agent некоторые провайдеры (например, Groq за
        # Cloudflare) блокируют запросы как похожие на бота ещё ДО того, как
        # они доходят до самого API - выглядит как ошибка ключа/доступа,
        # хотя ключ тут ни при чём.
        "User-Agent": "rac-generator/1.0 (+https://github.com)",
    }
    if headers:
        req_headers.update(headers)
    try:
        resp = await client.post(url, json=payload, headers=req_headers, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError("Превышено время ожидания ответа")
    except httpx.RequestError as e:
        raise ProviderError(f"Сетевая ошибка / сервис недоступен: {e}")

    if resp.status_code >= 400:
        body = resp.text
        classification = _classify_http_error(resp.status_code, body)
        err = ProviderError(
            classification["message"],
            raw_response=body or None,
            is_too_large=(classification["kind"] == "too_large"),
            detected_max_tokens=classification["detected_max_tokens"],
        )
        if classification["kind"] == "rate_limit":
            err.retry_after = _extract_retry_after(resp.headers)
        raise err

    try:
        return resp.json()
    except ValueError:
        raise ProviderError("Ответ сервера не является JSON.", raw_response=resp.text)


def _extract_retry_after(headers) -> int | None:
    """Читает стандартный заголовок Retry-After (секунды). Формат
    HTTP-даты (вместо числа секунд) встречается редко у LLM-провайдеров -
    в этом случае просто не повторяем автоматически (вернём None), это не
    критично: пользователь всё равно увидит понятную причину ошибки."""
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        value = int(float(raw.strip()))
        return value if 0 < value <= 3600 else None
    except (ValueError, AttributeError):
        return None


NETWORK_BLOCKED_HINT = (
    "Провайдер заблокировал запрос по сети (HTTP {code}: «Access denied. Please check your "
    "network settings»). Так отвечают на запросы с IP-адресов VPN и дата-центров - ключ тут ни "
    "при чём. Учтите: к провайдеру обращается сервер программы, а не ваш браузер, поэтому "
    "значение имеет IP, с которого в интернет выходит сервер (если сервер работает на этом же "
    "компьютере с включённым VPN - это адрес VPN)."
)


def _classify_http_error(code: int, body: str) -> dict:
    """Возвращает {"message": str, "kind": str, "detected_max_tokens": int|None}.
    kind помогает вызывающему коду решить, стоит ли пробовать другое значение
    max_tokens автоматически: "too_large" - провайдер сам сказал, что
    запрошенное значение больше допустимого для этой модели (это НЕ то же
    самое, что "ответ обрезался" - тут запрос вообще не отправился, но
    провайдер обычно указывает точный потолок, которым можно воспользоваться
    вместо того, чтобы просто сдаться)."""
    low = body.lower()
    if code == 429 or "rate limit" in low or "quota" in low or "insufficient_quota" in low:
        return {"kind": "rate_limit", "message": f"Лимит запросов исчерпан (HTTP {code})", "detected_max_tokens": None}
    if code == 401:
        return {"kind": "auth", "message": f"Неверный или отсутствующий API-ключ (HTTP {code})", "detected_max_tokens": None}
    if code == 403:
        # 403 - это НЕ то же самое, что 401: ключ может быть совершенно
        # рабочим, просто запрос по какой-то другой причине отклонён именно
        # для этого запроса/модели. Пытаемся распознать конкретную причину -
        # полный текст ответа в любом случае доступен в журнале модели.
        if "network settings" in low:
            # "Access denied. Please check your network settings." - так Groq
            # (и другие провайдеры за Cloudflare) отвечают на запросы с IP-адресов
            # VPN и дата-центров. Запрос делает сервер программы, а не браузер
            # пользователя, поэтому важен IP, с которого в интернет выходит сервер.
            return {"kind": "network_blocked", "detected_max_tokens": None, "message": NETWORK_BLOCKED_HINT.format(code=code)}
        if "model_permission_blocked" in low or "blocked at the organization level" in low or "blocked at the project level" in low:
            return {"kind": "permission", "detected_max_tokens": None, "message": (
                f"Модель заблокирована настройками доступа в аккаунте провайдера (HTTP {code}) - "
                f"ключ тут ни при чём. У Groq это правится в Model Permissions: "
                f"console.groq.com → Settings → Limits (организация) и Settings → Project → Limits "
                f"(проект) - модель может быть явно заблокирована или не входить в список разрешённых."
            )}
        if "country" in low or "region" in low or "territory" in low or "unsupported_location" in low:
            return {"kind": "region", "detected_max_tokens": None, "message": f"Доступ запрещён по региону/стране (HTTP {code}) - ограничение на стороне провайдера, не ошибка ключа."}
        return {"kind": "forbidden", "detected_max_tokens": None, "message": (
            f"Доступ запрещён (HTTP {code}) - это не обязательно неверный ключ: возможна блокировка "
            f"конкретной модели в настройках аккаунта провайдера, ограничение по региону или блокировка "
            f"на уровне CDN/Cloudflare. Полный ответ сервера сохранён в журнале модели - там обычно "
            f"написана точная причина."
        )}
    if code == 404:
        return {"kind": "not_found", "message": f"Модель или адрес не найдены (HTTP {code})", "detected_max_tokens": None}
    if "max_tokens" in low or "maximum context length" in low or "context_length_exceeded" in low or "max_completion_tokens" in low or "maxoutputtokens" in low:
        ceiling = _extract_token_ceiling(body)
        extra = f" Провайдер указал точный допустимый предел: {ceiling}." if ceiling else ""
        return {"kind": "too_large", "detected_max_tokens": ceiling, "message": (
            f"Модель отклонила запрошенный объём ответа (max_tokens) - для неё задано слишком "
            f"большое значение (HTTP {code}).{extra} Программа попробует скорректировать значение "
            f"автоматически."
        )}
    return {"kind": "other", "message": f"Ошибка провайдера (HTTP {code}) - полный ответ сохранён в журнале модели.", "detected_max_tokens": None}


def _extract_token_ceiling(body: str):
    """Пытается вытащить точное число из типичных формулировок вида
    "must be at most 8192", "<= 8192", "maximum context length is 8192
    tokens", "limit of 8192" и т.п. Возвращает None, если не нашлось -
    тогда просто откатываемся к прежнему (меньшему) значению."""
    patterns = [
        r"at most (\d{2,7})",
        r"<=\s*(\d{2,7})",
        r"maximum(?:\s+context\s+length)?\s*(?:is|of)?\s*[:\s]?\s*(\d{2,7})",
        r"limit(?:\s+of)?\s+(\d{2,7})",
        r"exceeds?\s+(?:the\s+)?(?:model'?s\s+)?(?:maximum\s+)?(?:of\s+)?(\d{2,7})",
    ]
    for p in patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            try:
                value = int(m.group(1))
                if 16 <= value <= 5_000_000:  # отсеиваем случайные мелкие/огромные числа не по теме
                    return value
            except ValueError:
                continue
    return None


# ---------------------------- драйверы протоколов ----------------------------

_TRUNCATION_HINT = (
    "Увеличьте «Максимум токенов ответа» у этой модели в Настройках. Если это "
    "рассуждающая модель (например, GPT-OSS, QwQ, o-серия) - часть лимита уходит "
    "на внутренние рассуждения ещё до начала самого ответа, так что для неё "
    "обычно нужен запас заметно больше обычного."
)


def _extract_embedded_error(data) -> str:
    """Некоторые прокси-агрегаторы (характерно для OpenRouter) при сбое
    апстрим-провайдера отвечают HTTP 200 (формально "успех"), но кладут
    в тело {"error": {"message": ..., "code": ..., "metadata": {...}}}
    вместо ожидаемых choices/candidates. _http_post_json_async такое не ловит -
    он проверяет только код HTTP-ответа. Без этой проверки такой ответ
    выглядел бы как невнятное "неожиданный формат ответа", хотя на самом
    деле причина конкретная и обычно временная (апстрим перегружен и т.п.)."""
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    parts = [str(err.get("message") or err)]
    if err.get("code"):
        parts.append(f"код {err['code']}")
    meta = err.get("metadata")
    if isinstance(meta, dict):
        reason = meta.get("error_type") or meta.get("provider_name") or meta.get("reason")
        if reason:
            parts.append(str(reason))
    return " | ".join(parts)


class BaseDriver:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    async def chat(self, client: httpx.AsyncClient, system: str, user: str,
                    timeout: int = GENERATION_TIMEOUT, max_tokens: int = None) -> str:
        raise NotImplementedError


class OpenAICompatDriver(BaseDriver):
    """Любой OpenAI-совместимый /chat/completions (OpenRouter, Groq, Together, ...)."""

    async def chat(self, client: httpx.AsyncClient, system: str, user: str,
                    timeout: int = GENERATION_TIMEOUT, max_tokens: int = None) -> str:
        base_url = (self.cfg.get("base_url") or "").rstrip("/")
        api_key = self.cfg.get("api_key", "")
        model = self.cfg.get("model", "")
        if not base_url or not model:
            raise ProviderError("Не задан адрес (base_url) или модель")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        # OpenRouter-специфика: некоторые модели (например, дефолтная бесплатная
        # "deepseek/deepseek-chat-v3.1:free") - гибридные "thinking"-модели,
        # которые по умолчанию включают режим рассуждений и тратят на него
        # часть бюджета max_tokens ещё ДО того, как начнут писать сам
        # JSON-ответ. Для задачи "выдай готовый JSON" рассуждения не нужны, а
        # их включённость по умолчанию - основная причина обрезки ответа по
        # max_tokens именно у OpenRouter (у Groq таких моделей в дефолтном
        # наборе нет, поэтому там проблема не проявляется). OpenRouter
        # понимает унифицированный параметр reasoning - выключаем его здесь.
        # Для моделей, где reasoning обязателен (o-серия, QwQ, GPT-OSS и т.п.),
        # OpenRouter либо проигнорирует этот параметр, либо отключить его
        # действительно нельзя - тогда работает обычная эскалация max_tokens.
        if "openrouter.ai" in base_url.lower():
            payload["reasoning"] = {"enabled": False}
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = await _http_post_json_async(client, f"{base_url}/chat/completions", payload, headers, timeout=timeout)
        embedded_error = _extract_embedded_error(data)
        if embedded_error:
            # HTTP был "успешным" (иначе это поймал бы _http_post_json_async), но
            # в теле лежит ошибка апстрим-провайдера - типично для
            # OpenRouter при перегрузке/сбое конкретного бэкенда модели
            raise ProviderError(
                f"Провайдер вернул ошибку в теле ответа при HTTP 200 (агрегатор/апстрим): {embedded_error}",
                raw_response=json.dumps(data, ensure_ascii=False, indent=2),
            )
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError):
            raise ProviderError(
                "Неожиданный формат ответа - нет ожидаемых полей choices/message/content.",
                raw_response=json.dumps(data, ensure_ascii=False, indent=2),
            )
        if choice.get("finish_reason") == "length":
            # ответ гарантированно оборван по max_tokens - это не "модель не
            # смогла вернуть JSON", а конкретная и легко устранимая причина,
            # так что сообщаем об этом отдельно и сразу, не дожидаясь, пока
            # разбор JSON провалится с менее понятной ошибкой
            raise ProviderError(f"Ответ обрезан по достижении лимита max_tokens. {_TRUNCATION_HINT}", raw_response=content, is_truncation=True)
        return content


class OllamaDriver(BaseDriver):
    async def chat(self, client: httpx.AsyncClient, system: str, user: str,
                    timeout: int = GENERATION_TIMEOUT, max_tokens: int = None) -> str:
        base_url = (self.cfg.get("base_url") or "http://localhost:11434").rstrip("/")
        model = self.cfg.get("model", "")
        if not model:
            raise ProviderError("Не задана модель")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        # у Ollama свой параметр (options.num_predict), а не max_tokens; и что
        # особенно важно - если его вообще не передать, Ollama по умолчанию
        # обрывает ответ на 128 токенах (!), поэтому здесь всегда подставляем
        # хоть какое-то разумное значение, даже если вызывающий код его не передал
        payload["options"] = {"num_predict": max_tokens or 4000}
        data = await _http_post_json_async(client, f"{base_url}/api/chat", payload, timeout=timeout)
        embedded_error = _extract_embedded_error(data)
        if embedded_error:
            raise ProviderError(
                f"Провайдер вернул ошибку в теле ответа при HTTP 200: {embedded_error}",
                raw_response=json.dumps(data, ensure_ascii=False, indent=2),
            )
        try:
            content = data["message"]["content"]
        except KeyError:
            raise ProviderError(
                "Неожиданный формат ответа Ollama - нет поля message.",
                raw_response=json.dumps(data, ensure_ascii=False, indent=2),
            )
        if data.get("done_reason") == "length":
            raise ProviderError(f"Ответ обрезан по достижении лимита num_predict. {_TRUNCATION_HINT}", raw_response=content, is_truncation=True)
        return content


class GeminiDriver(BaseDriver):
    async def chat(self, client: httpx.AsyncClient, system: str, user: str,
                    timeout: int = GENERATION_TIMEOUT, max_tokens: int = None) -> str:
        api_key = self.cfg.get("api_key", "")
        model = self.cfg.get("model", "gemini-2.0-flash")
        if not api_key:
            raise ProviderError("Не задан API-ключ")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        generation_config = {"temperature": 0.7}
        if max_tokens:
            generation_config["maxOutputTokens"] = max_tokens
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation_config,
        }
        data = await _http_post_json_async(client, url, payload, timeout=timeout)
        embedded_error = _extract_embedded_error(data)
        if embedded_error:
            raise ProviderError(
                f"Провайдер вернул ошибку в теле ответа при HTTP 200: {embedded_error}",
                raw_response=json.dumps(data, ensure_ascii=False, indent=2),
            )
        try:
            candidate = data["candidates"][0]
            parts = candidate["content"]["parts"]
            content = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError):
            raise ProviderError(
                "Неожиданный формат ответа Gemini - нет ожидаемых полей candidates/content/parts.",
                raw_response=json.dumps(data, ensure_ascii=False, indent=2),
            )
        if data["candidates"][0].get("finishReason") == "MAX_TOKENS":
            raise ProviderError(f"Ответ обрезан по достижении лимита maxOutputTokens. {_TRUNCATION_HINT}", raw_response=content, is_truncation=True)
        return content


DRIVERS = {
    "openai_compat": OpenAICompatDriver,
    "gemini": GeminiDriver,
    "ollama": OllamaDriver,
}

PROVIDER_TYPE_LABELS = {
    "openai_compat": "OpenAI-совместимый API (OpenRouter, Groq, DeepSeek, LM Studio, свой сервер...)",
    "gemini": "Google Gemini",
    "ollama": "Ollama (локально)",
}


def _driver_for(model_row: dict) -> BaseDriver:
    cls = DRIVERS.get(model_row["provider_type"])
    if not cls:
        raise ProviderError(f"Неизвестный тип провайдера: {model_row['provider_type']}")
    return cls({
        "base_url": model_row.get("base_url", ""),
        "api_key": model_row.get("api_key", ""),
        "model": model_row.get("model", ""),
    })


# ---------------------------- проверка доступности ----------------------------

async def _check_model_availability_async(model_row: dict) -> dict:
    driver = _driver_for(model_row)
    started = time.time()
    try:
        async with httpx.AsyncClient() as client:
            reply = await driver.chat(
                client,
                "Ты - тестовый ассистент для проверки связи.",
                "Ответь одним словом: OK",
                timeout=CHECK_TIMEOUT,
                max_tokens=50,
            )
        elapsed_ms = int((time.time() - started) * 1000)
        snippet = (reply or "").strip().replace("\n", " ")[:60]
        return {"status": "ok", "message": f"Отвечает за {elapsed_ms} мс ({snippet!r})", "raw_response": None}
    except ProviderError as e:
        if getattr(e, "is_truncation", False):
            # у рассуждающих моделей (GPT-OSS, QwQ и т.п.) внутренние
            # рассуждения могут не уложиться даже в тестовый бюджет токенов -
            # это НЕ означает, что модель недоступна: настоящая генерация
            # использует бюджет на порядки больше. Показываем как "доступна",
            # а не пугаем пользователя ложной "ошибкой".
            return {"status": "ok", "message": (
                "Модель отвечает, но не уложилась в маленький тестовый бюджет токенов - "
                "типично для рассуждающих моделей. Для настоящей генерации это не проблема, "
                "у неё бюджет («Максимум токенов ответа») намного больше."
            ), "raw_response": getattr(e, "raw_response", None)}
        return {"status": "error", "message": str(e), "raw_response": getattr(e, "raw_response", None)}
    except Exception as e:
        return {"status": "error", "message": f"Непредвиденная ошибка: {e}", "raw_response": None}


def check_model_availability(model_row: dict) -> dict:
    """Лёгкий тестовый запрос, чтобы показать в Настройках, жива ли модель.
    Синхронная обёртка над асинхронной реализацией: вызывается из обычных
    (sync def) FastAPI-роутов и из ThreadPoolExecutor (см. main.py), где
    нет своего активного event loop, так что asyncio.run() здесь безопасен -
    это НЕ та же самая долгая генерация, ради которой затевался переход на
    httpx (см. generate_proposal_content_stream), а короткая проверка на
    несколько секунд, так что отдельный event loop на вызов не накладно."""
    return asyncio.run(_check_model_availability_async(model_row))


# ---------------------------- генерация с автопереключением ----------------------------

SYSTEM_PROMPT = (
    "Ты - опытный военный инженер-рационализатор, который готовит рационализаторские "
    "предложения (рацпредложения) для подачи в бюро изобретательства и рационализации "
    "воинской части в соответствии с требованиями Министерства обороны РФ. "
    "Твой текст должен быть техническим, конкретным, правдоподобным, опираться на реальные "
    "принципы техники и общеизвестные (открытые) сведения, без выдуманных секретных данных, "
    "без указания реальных грифов секретности и без персональных данных реальных людей. "
    "Пиши по-русски, официально-деловым техническим стилем, без воды и без markdown-разметки "
    "(никаких звёздочек, решёток и списков через дефис - только обычный связный текст абзацами). "
    "Каждое утверждение подкрепляй конкретикой - технической причиной, механизмом действия, "
    "ориентировочной цифрой (размер, допуск, материал, время, процент) - вместо общих фраз "
    "вроде «повышает эффективность» или «улучшает качество» без пояснения, за счёт чего именно. "
    "Избегай и шаблонных оборотов ИИ-текстов («таким образом», «в заключение следует отметить», "
    "«инновационное решение», «уникальный», «революционный», «играет важную роль») - пиши так, "
    "как пишут в реальных технических документах. Все 6 разделов описывают ОДНО и то же "
    "техническое решение: названия узлов, материалов и параметров, введённые в разделе 3, должны "
    "затем точно так же (теми же словами) встречаться в разделах 2 и 6, а не меняться от раздела "
    "к разделу - текст не должен противоречить сам себе."
)


def build_user_prompt(topic: str, extra_context: str = "") -> str:
    return f"""Тема (концепция) рационализаторского предложения: "{topic}"
{f'Дополнительные пояснения автора: {extra_context}' if extra_context else ''}

Сгенерируй содержимое рационализаторского предложения строго в формате JSON (без markdown, без пояснений вокруг, только один JSON-объект) со следующими ключами. Пиши развёрнуто и по существу - это официальный технический документ, а не краткая аннотация; ориентиры по объёму ниже - это МИНИМУМ, а не потолок:

"title" - наименование предложения, ОБЯЗАТЕЛЬНО начинающееся со слова "Способ " или "Модель " (одно предложение, без точки в конце);
"section1" - НАЗНАЧЕНИЕ ПРЕДЛОЖЕНИЯ (не менее 150 слов, 2-4 абзаца: для чего предназначено, какую задачу решает, в каких условиях применяется);
"section2" - СВЕДЕНИЯ О НЕДОСТАТКАХ СУЩЕСТВУЮЩИХ ТЕХНИЧЕСКИХ РЕШЕНИЙ (АНАЛОГОВ), УСТРАНЯЕМЫХ ЗАЯВЛЕННЫМ ПРЕДЛОЖЕНИЕМ (не менее 200 слов, 3-5 абзацев: назови 2-3 РАЗНЫХ существующих подхода/аналога по отдельности (а не один обобщённый "аналог"), у каждого - свой конкретный недостаток с технической причиной, и прямое соответствие "недостаток аналога -> чем именно его устраняет предлагаемое решение");
"section3" - ОПИСАНИЕ ПРЕДЛАГАЕМОГО ТЕХНИЧЕСКОГО РЕШЕНИЯ (не менее 350 слов, 4-7 абзацев: подробное техническое описание конструкции/алгоритма, принцип действия по шагам, из каких узлов/компонентов состоит и как они взаимодействуют, какие материалы или технологии применяются - дай узлам/материалам конкретные названия и используй их именно в этом виде далее по тексту);
"section4" - ГРАФИЧЕСКОЕ ИЗОБРАЖЕНИЕ ПРЕДЛОЖЕНИЯ (1 короткий абзац - подпись/пояснение к тому, какой чертёж, схему или фото автору следует приложить; сам рисунок автор вставляет вручную);
"section5" - СТЕПЕНЬ РАЗРАБОТКИ ПРЕДЛОЖЕНИЯ, МАСШТАБ И МЕСТО ЕГО ПРИМЕНЕНИЯ (не менее 150 слов, 3-5 абзацев);
"section6" - ОБОСНОВАНИЕ ПОЛОЖИТЕЛЬНОГО ЭФФЕКТА ОТ ПРИМЕНЕНИЯ ПРЕДЛОЖЕНИЯ (не менее 200 слов, 3-5 абзацев: технический, эксплуатационный, экономический эффект и влияние на боеготовность - каждый вид эффекта отдельно и конкретно, с ориентировочными показателями, и явно привязан к недостаткам, названным в разделе 2, - то есть видно, какой именно эффект закрывает какой именно недостаток).

Перед тем как выдать ответ, мысленно проверь: заполнены ли все 6 разделов без пропусков; не повторяется ли один и тот же оборот речи в разных разделах; есть ли в каждом разделе хотя бы одна конкретная цифра или техническая деталь; совпадают ли названия узлов/материалов из раздела 3 с тем, что упомянуто в разделах 2 и 6.

Каждый section-текст - строка, где абзацы разделены символом \\n\\n. Верни только валидный JSON, без пояснений до или после него."""


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# strict=False: многие модели пишут в значениях JSON настоящие переводы строк
# вместо \n - строго по стандарту это ошибка, но смысл однозначен
_JSON_DECODER = json.JSONDecoder(strict=False)


def extract_json(text: str) -> dict:
    """Находит в ответе модели JSON-объект с разделами.

    Раньше бралось всё от первой «{» до последней «}» одним куском - и ответ
    не разбирался, если модель (а) перед JSON рассуждала в блоке <think> и
    упоминала там фигурные скобки (DeepSeek-R1, Qwen3 и другие рассуждающие
    модели через Ollama/LM Studio/Groq), (б) после JSON дописала пояснение со
    скобками, (в) поставила внутри строки настоящий перевод строки. Модель
    тогда считалась сломанной, и лимит уходил впустую. Теперь блоки <think>
    вырезаются, а объект ищется с каждой «{» по порядку, пока не найдётся
    тот, в котором есть поля рацпредложения."""
    original = text
    if not isinstance(text, str):
        raise ProviderError("В ответе модели не найден JSON-объект.", raw_response=str(original))
    cleaned = _THINK_BLOCK.sub("", text)
    first_error = None
    fallback = None
    for m in re.finditer(r"\{", cleaned):
        try:
            obj, _ = _JSON_DECODER.raw_decode(cleaned, m.start())
        except json.JSONDecodeError as e:
            first_error = first_error or e
            continue
        if isinstance(obj, dict):
            if "title" in obj or "section1" in obj:
                return obj
            if fallback is None:
                fallback = obj
    if fallback is not None:
        return fallback  # объект без нужных полей - их отсутствие подробно сообщит вызывающий код
    if "{" not in cleaned:
        raise ProviderError("В ответе модели не найден JSON-объект.", raw_response=original)
    raise ProviderError(f"Ответ похож на JSON, но не разбирается: {first_error}", raw_response=original)


def _normalize_generated(data: dict) -> dict:
    """Приводит ответ модели к виду, который ждут интерфейс и документ:
    чистый текст разделов (абзацы через пустую строку, без markdown и
    служебных символов) и наименование с заглавной «Способ»/«Модель»."""
    for i in range(1, 7):
        key = f"section{i}"
        if key in data:
            data[key] = text_clean.body_text(data[key])
    title = text_clean.clean_title(data.get("title", ""))
    low = title.lower()
    if low.startswith("способ") or low.startswith("модель"):
        # раньше «способ ...» со строчной превращалось в «Способ способ ...»
        title = title[:1].upper() + title[1:]
    else:
        title = "Способ " + title
    data["title"] = title
    return data


def _ordered_models(preferred_model_id=None):
    models = db.list_models(enabled_only=True)
    if not models:
        return []
    if preferred_model_id:
        # явно выбрана конкретная модель (не "Автоматически") - пробуем ТОЛЬКО
        # её, без переключения на остальные. Цепочка автопереключения имеет
        # смысл только при выборе "Автоматически (по приоритету)" - иначе
        # пользователь, специально выбравший одну модель, не может быть
        # уверен, что использовалась именно она, и рискует незапланированно
        # сжечь лимит других нейросетей.
        return [m for m in models if m["id"] == preferred_model_id]
    return models


async def generate_proposal_content_stream(topic: str, extra_context: str = "", preferred_model_id=None, request_id: str = None):
    """
    То же самое переключение между моделями, но как АСИНХРОННЫЙ генератор
    событий для прогресс-бара на фронтенде: реальные этапы (отправили запрос /
    получили ответ / собираем разделы / готово), без фейковой имитации. Каждое
    событие - dict с ключом "stage". Дополнительно фиксирует результат каждой
    попытки в БД (llm_models.last_check_*), чтобы отметка "лимит исчерпан" в
    интерфейсе отражала реальное использование, а не только ручные проверки.

    request_id: если задан, перед КАЖДОЙ следующей попыткой модели проверяется
    db.is_generation_cancelled(request_id) - это позволяет пользователю
    остановить перебор моделей кнопкой "Остановить генерацию" и не сжигать
    лимиты остальных нейросетей из цепочки автопереключения.

    Общее время работы этой функции ограничено GENERATION_DEADLINE_SECONDS
    (см. выше) - без этого потолка цепочка из нескольких медленных/ошибающихся
    моделей могла растянуться на десятки минут при каждой генерации.
    """
    models = _ordered_models(preferred_model_id)
    if not models:
        if preferred_model_id:
            message = "Выбранная модель недоступна (выключена или удалена в Настройках). Выберите другую модель или «Автоматически»."
        else:
            message = "Нет ни одной включённой нейросети. Добавьте и включите модель на вкладке «Настройки»."
        yield {"stage": "error", "message": message}
        return

    user_prompt = build_user_prompt(topic, extra_context)
    required = ["title", "section1", "section2", "section3", "section4", "section5", "section6"]
    attempts = []
    start_time = time.monotonic()

    # При обнаружении обрезки по max_tokens пробуем ТУ ЖЕ модель ещё раз со
    # увеличенным лимитом, прежде чем сдаваться и переходить к следующей
    # модели в цепочке - именно это "чинит" обрезанный ответ автоматически,
    # а не просто сообщает о проблеме. MAX_TOKENS_HARD_CAP - разумный потолок,
    # чтобы не улететь в неадекватно большие (и дорогие/медленные) значения.
    ESCALATION_FACTORS = [1, 2, 4]
    MAX_TOKENS_HARD_CAP = 32000

    try:
        async with httpx.AsyncClient() as client:
            for m in models:
                if db.is_generation_cancelled(request_id):
                    yield {"stage": "cancelled", "message": "Генерация остановлена пользователем."}
                    return

                base_max_tokens = m.get("max_tokens") or 4000
                # очередь значений max_tokens для этой модели: обычная эскалация
                # (1x, 2x, 4x), НО если провайдер вернёт "слишком большое значение"
                # с точным указанным потолком - он добавляется в очередь вместо
                # того, чтобы считать это финальным провалом модели
                queue = [min(int(base_max_tokens * f), MAX_TOKENS_HARD_CAP) for f in ESCALATION_FACTORS]
                queue = list(dict.fromkeys(queue))  # без дублей, если потолок всех "прижал" к одному числу
                tried = set()

                while True:
                    elapsed = time.monotonic() - start_time
                    remaining_budget = GENERATION_DEADLINE_SECONDS - elapsed
                    if remaining_budget <= 5:
                        yield {"stage": "error", "message": (
                            f"Превышено общее время ожидания генерации ({GENERATION_DEADLINE_SECONDS} сек) - "
                            f"перебор моделей остановлен, чтобы не ждать бесконечно. Попробуйте включить меньше "
                            f"моделей в цепочке, выбрать конкретную модель вместо «Автоматически», или проверить "
                            f"доступность моделей кнопкой «проверить»."
                        )}
                        return

                    if db.is_generation_cancelled(request_id):
                        yield {"stage": "cancelled", "message": "Генерация остановлена пользователем."}
                        return

                    remaining = [v for v in queue if v not in tried]
                    if not remaining:
                        break  # для этой модели больше нечего пробовать - переходим к следующей модели
                    attempt_max_tokens = remaining[0]
                    tried.add(attempt_max_tokens)
                    is_first_attempt = len(tried) == 1
                    attempt_timeout = max(5, min(GENERATION_TIMEOUT, remaining_budget))

                    if is_first_attempt:
                        yield {"stage": "sending", "model": m["name"], "model_id": m["id"]}
                    else:
                        yield {"stage": "escalating", "model": m["name"], "model_id": m["id"], "max_tokens": attempt_max_tokens}

                    try:
                        driver = _driver_for(m)
                        try:
                            raw = await driver.chat(client, SYSTEM_PROMPT, user_prompt, timeout=attempt_timeout, max_tokens=attempt_max_tokens)
                        except ProviderError as rate_err:
                            # HTTP 429 с коротким Retry-After - вероятно, лимит ЧАСТОТЫ
                            # запросов (а не дневной лимит), который снимется за секунды;
                            # одна короткая пауза и повтор ТОГО ЖЕ запроса экономит дневной
                            # лимит следующей модели в цепочке, которую иначе пришлось бы
                            # расходовать совсем напрасно.
                            retry_after = getattr(rate_err, "retry_after", None)
                            if (retry_after and retry_after <= RATE_LIMIT_MAX_WAIT
                                    and retry_after < remaining_budget - 2
                                    and not db.is_generation_cancelled(request_id)):
                                yield {"stage": "rate_limited", "model": m["name"], "model_id": m["id"], "seconds": retry_after}
                                await asyncio.sleep(retry_after)
                                raw = await driver.chat(client, SYSTEM_PROMPT, user_prompt, timeout=attempt_timeout, max_tokens=attempt_max_tokens)
                            else:
                                raise

                        if db.is_generation_cancelled(request_id):
                            yield {"stage": "cancelled", "message": "Генерация остановлена пользователем."}
                            return
                        yield {"stage": "received", "model": m["name"], "model_id": m["id"]}

                        data = extract_json(raw)
                        missing = [k for k in required if k not in data or not str(data[k]).strip()]
                        if missing:
                            raise ProviderError(f"В ответе отсутствуют поля: {', '.join(missing)}", raw_response=raw)
                        data = _normalize_generated(data)
                        # раздел мог состоять из одной разметки (например, «---») и после очистки опустеть
                        missing = [k for k in required if not str(data[k]).strip()]
                        if missing:
                            raise ProviderError(f"В ответе пустые поля: {', '.join(missing)}", raw_response=raw)
                        title = data["title"]

                        attempts.append({"model_id": m["id"], "name": m["name"], "ok": True})
                        data["_used_model"] = {"id": m["id"], "name": m["name"]}
                        data["_attempts"] = attempts

                        if attempt_max_tokens != base_max_tokens:
                            # "самообучение": раз обычного значения не хватило, а этого
                            # хватило - запоминаем его, чтобы в следующий раз генерация
                            # сразу начиналась с рабочего лимита, без повторной эскалации
                            data["_max_tokens_escalated_to"] = attempt_max_tokens
                            try:
                                db.update_model_max_tokens(m["id"], attempt_max_tokens)
                            except Exception:
                                pass

                        try:
                            total_chars = sum(len(str(data.get(f"section{i}", ""))) for i in range(1, 7))
                            note = f" (лимит автоматически увеличен до {attempt_max_tokens})" if attempt_max_tokens != base_max_tokens else ""
                            db.set_model_check_result(
                                m["id"], "ok",
                                f"Успешно использована при генерации{note}. Заголовок: {title[:80]!r}. Объём текста разделов: ~{total_chars} символов.",
                                event_type="generation",
                            )
                        except Exception:
                            pass

                        yield {"stage": "forming", "model": m["name"]}
                        yield {"stage": "done", "data": data}
                        return
                    except Exception as e:
                        error_text = str(e)
                        raw_response = getattr(e, "raw_response", None)
                        is_truncation = getattr(e, "is_truncation", False)
                        is_too_large = getattr(e, "is_too_large", False)
                        detected_ceiling = getattr(e, "detected_max_tokens", None)

                        try:
                            db.set_model_check_result(m["id"], "error", error_text, event_type="generation", raw_response=raw_response)
                        except Exception:
                            pass

                        if is_too_large:
                            # провайдер сам отказался принять такой большой max_tokens -
                            # "увеличивать ещё" точно не поможет, но если он указал
                            # точный предел - добавляем именно его в очередь, а не
                            # сдаёмся сразу (вдруг именно этого предела хватит, чтобы
                            # ответ не обрезался)
                            local_hard_cap = min(MAX_TOKENS_HARD_CAP, (detected_ceiling or attempt_max_tokens - 1))
                            queue = [v for v in queue if v <= local_hard_cap]
                            if detected_ceiling and detected_ceiling <= local_hard_cap and detected_ceiling not in queue:
                                queue.append(detected_ceiling)
                            if any(v not in tried for v in queue):
                                continue  # есть ещё что попробовать в пределах узнанного потолка
                            attempts.append({"model_id": m["id"], "name": m["name"], "ok": False, "error": error_text})
                            yield {"stage": "retry", "model": m["name"], "error": error_text}
                            break

                        if is_truncation and any(v not in tried for v in queue):
                            # не финальный провал модели - пробуем следующий шаг эскалации
                            # на ТОЙ ЖЕ модели, не переходя к следующей в цепочке
                            continue

                        attempts.append({"model_id": m["id"], "name": m["name"], "ok": False, "error": error_text})
                        yield {"stage": "retry", "model": m["name"], "error": error_text}
                        break

        details = "; ".join(f"{a['name']} — {a['error']}" for a in attempts)
        if len(attempts) == 1:
            message = f"Модель «{attempts[0]['name']}» не смогла сгенерировать текст: {attempts[0]['error']}"
        else:
            message = f"Не удалось получить ответ ни от одной из {len(attempts)} включённых моделей: {details}"
        yield {"stage": "error", "message": message}
    finally:
        # снимаем флаг отмены при любом завершении (успех/ошибка/отмена/обрыв
        # клиента) - иначе он остался бы в БД мусором на будущее
        db.clear_generation_cancel(request_id)


async def generate_proposal_content(topic: str, extra_context: str = "", preferred_model_id=None) -> dict:
    """
    Пробует включённые модели по очереди (сначала preferred_model_id, если
    задан, затем остальные по приоритету), пока одна не ответит успешно.
    Возвращает контент + служебные поля _used_model / _attempts, чтобы
    интерфейс мог показать, какая модель сработала и что было перед этим.
    (Тонкая обёртка над generate_proposal_content_stream для вызовов, которым
    не нужен пошаговый прогресс.)
    """
    async for event in generate_proposal_content_stream(topic, extra_context, preferred_model_id):
        if event["stage"] == "done":
            return event["data"]
        if event["stage"] in ("error", "cancelled"):
            raise ProviderError(event["message"])
    raise ProviderError("Генерация не вернула результат.")
