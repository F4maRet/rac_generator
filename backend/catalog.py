"""
Каталог моделей провайдеров для окна «Каталог» в форме новой модели.

Источники (описаны в SOURCE_INFO ниже):

    openrouter - облачный агрегатор: сотни моделей, цены по каждой (видно,
                 какие бесплатные), а при наличии ключа - и популярность
    together   - облачный агрегатор Together AI: цены по каждой модели
    groq, gemini, mistral, deepseek, cerebras - облачные провайдеры: список
                 моделей с контекстом/лимитом ответа; цен по моделям их API
                 не отдаёт - показывается тариф провайдера в целом
    ollama     - локальный Ollama: список реально скачанных моделей
    lmstudio   - локальный LM Studio: список моделей, доступных на сервере
    custom     - любой другой OpenAI-совместимый сервер (/v1/models): OpenAI,
                 Fireworks, Hugging Face, свой vLLM и т.п. - только id моделей

Каждая модель из каталога должна потом реально генерировать текст через уже
существующие драйверы llm_providers.py, поэтому сюда включены только
провайдеры, чей формат генерации совместим с ними: gemini - свой драйвер,
все остальные облачные - OpenAI-совместимый /chat/completions (проверено по
документации каждого провайдера 2026-10-03). Популярность моделей из всех
этих API отдаёт только OpenRouter.

Запрос к провайдеру выполняет сервер, а не браузер: так нет проблем с CORS,
и API-ключ не покидает сервер (ключ берётся либо из формы, если его только
что вписали, либо из уже сохранённой модели с тем же адресом - см.
_resolve_api_key). В браузер уходит только нормализованный список моделей.

Про популярность у OpenRouter (проверено на живом API 2026-10-03): по
документации /models принимает параметр sort=most-popular, но без ключа
этот параметр молча игнорируется - ответ в том же порядке, что и без него.
Поэтому код не верит сортировке вслепую: параллельно запрашивается
sort=newest, и если даты создания в нём действительно идут по убыванию -
значит, сервер сортировку применил, и порядок most-popular можно честно
показывать как "популярность". Если нет - интерфейс так и пишет, что
популярность недоступна, а не выдаёт случайный порядок за популярный.
"""
import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

import db
from llm_providers import NETWORK_BLOCKED_HINT

# pricing_mode:
#   per_model  - цена известна по каждой модели (фильтр «бесплатные/платные» работает)
#   note       - API цен не отдаёт; показывается price_note - тариф провайдера в целом
#   local      - локальная модель, бесплатно
# is_free_default - что ставить в галочку «Платная модель» для моделей
#   такого провайдера (None - неизвестно, галочку не трогаем)
# host_hint - по какой части адреса узнавать уже сохранённые модели этого
#   провайдера (чтобы взять их ключ и пометить «уже добавлена»)
SOURCE_INFO = {
    "openrouter": {
        "label": "OpenRouter", "group": "Облачные агрегаторы", "provider_type": "openai_compat",
        "default_url": "https://openrouter.ai/api/v1", "host_hint": "openrouter.ai",
        "needs_key": False, "key_url": "https://openrouter.ai/keys",
        "pricing_mode": "per_model", "price_note": "", "is_free_default": None,
    },
    "together": {
        "label": "Together AI", "group": "Облачные агрегаторы", "provider_type": "openai_compat",
        "default_url": "https://api.together.ai/v1", "host_hint": "together.",
        "needs_key": True, "key_url": "https://api.together.ai/settings/api-keys",
        "pricing_mode": "per_model", "price_note": "", "is_free_default": None,
    },
    "groq": {
        "label": "Groq", "group": "Облачные провайдеры", "provider_type": "openai_compat",
        "default_url": "https://api.groq.com/openai/v1", "host_hint": "groq.com",
        "needs_key": True, "key_url": "https://console.groq.com/keys",
        "pricing_mode": "note", "price_note": "бесплатный тариф с лимитами", "is_free_default": True,
    },
    "gemini": {
        "label": "Google Gemini", "group": "Облачные провайдеры", "provider_type": "gemini",
        # адрес только для отображения: драйвер Gemini всегда обращается к API
        # Google напрямую, поле «Адрес» у моделей Gemini не используется
        "default_url": "https://generativelanguage.googleapis.com/v1beta", "host_hint": None,
        "needs_key": True, "key_url": "https://aistudio.google.com/apikey",
        "pricing_mode": "note", "price_note": "есть бесплатный лимит (у части моделей)", "is_free_default": True,
    },
    "mistral": {
        "label": "Mistral", "group": "Облачные провайдеры", "provider_type": "openai_compat",
        "default_url": "https://api.mistral.ai/v1", "host_hint": "mistral.ai",
        "needs_key": True, "key_url": "https://console.mistral.ai/api-keys",
        "pricing_mode": "note", "price_note": "бесплатный тариф «Experiment» с лимитами", "is_free_default": True,
    },
    "deepseek": {
        "label": "DeepSeek", "group": "Облачные провайдеры", "provider_type": "openai_compat",
        "default_url": "https://api.deepseek.com", "host_hint": "deepseek.com",
        "needs_key": True, "key_url": "https://platform.deepseek.com/api_keys",
        "pricing_mode": "note", "price_note": "платно, оплата за токены", "is_free_default": False,
    },
    "cerebras": {
        "label": "Cerebras", "group": "Облачные провайдеры", "provider_type": "openai_compat",
        "default_url": "https://api.cerebras.ai/v1", "host_hint": "cerebras.ai",
        "needs_key": True, "key_url": "https://cloud.cerebras.ai",
        "pricing_mode": "note", "price_note": "бесплатный тариф с лимитами", "is_free_default": True,
    },
    "ollama": {
        "label": "Ollama", "group": "Локально", "provider_type": "ollama",
        "default_url": "http://localhost:11434", "host_hint": None,
        "needs_key": False, "key_url": "",
        "pricing_mode": "local", "price_note": "локально, бесплатно", "is_free_default": True,
    },
    "lmstudio": {
        "label": "LM Studio", "group": "Локально", "provider_type": "openai_compat",
        "default_url": "http://localhost:1234/v1", "host_hint": None,
        "needs_key": False, "key_url": "",
        "pricing_mode": "local", "price_note": "локально, бесплатно", "is_free_default": True,
    },
    "custom": {
        "label": "Другой OpenAI-совместимый", "group": "Другое", "provider_type": "openai_compat",
        "default_url": "", "host_hint": None,
        "needs_key": False, "key_url": "",
        "pricing_mode": "note", "price_note": "тариф неизвестен", "is_free_default": None,
    },
}
SOURCES = tuple(SOURCE_INFO)
DEFAULT_BASE_URLS = {k: v["default_url"] for k, v in SOURCE_INFO.items()}

# Облачные списки меняются нечасто - кэшируем на час (кнопка «Обновить»
# обходит кэш). Локальные дешёвые и меняются, когда человек скачал/удалил
# модель - для них кэш короткий.
CACHE_TTL_SECONDS = {k: (30 if v["pricing_mode"] == "local" else 3600) for k, v in SOURCE_INFO.items()}
CACHE_TTL_SECONDS["custom"] = 60
CATALOG_TIMEOUT = 20
USER_AGENT = "rac-generator/1.0 (+https://github.com)"

_cache: dict = {}


class CatalogError(Exception):
    """status - HTTP-код ответа браузеру: 400, если исправить может сам
    пользователь (нет ключа, неверный адрес), 502 - ошибка на стороне провайдера."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def public_sources() -> list:
    """Описание источников для интерфейса (без внутренних полей)."""
    return [
        {
            "id": key, "label": info["label"], "group": info["group"],
            "default_url": info["default_url"], "needs_key": info["needs_key"],
            "key_url": info["key_url"], "pricing_mode": info["pricing_mode"],
            "price_note": info["price_note"], "host_hint": info["host_hint"],
            "provider_type": info["provider_type"],
        }
        for key, info in SOURCE_INFO.items()
    ]


# ---------------------------- общие помощники ----------------------------

def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _root(url: str) -> str:
    """Адрес без хвостового "/" и "/v1" - чтобы "http://localhost:1234/v1" и
    "http://localhost:1234" считались одним и тем же сервером."""
    url = (url or "").strip().rstrip("/")
    if url.lower().endswith("/v1"):
        url = url[:-3]
    return url.rstrip("/")


def _same_server(a: str, b: str) -> bool:
    return _root(a).lower() == _root(b).lower()


def _row_matches_source(row: dict, source: str, base_url: str) -> bool:
    """Относится ли сохранённая модель программы к этому источнику каталога
    (по типу API и адресу). Используется и для поиска ключа, и для пометки
    «уже добавлена»."""
    info = SOURCE_INFO[source]
    row_url = (row.get("base_url") or "").lower()
    ptype = row.get("provider_type")
    if source == "gemini":
        return ptype == "gemini"
    if source == "ollama":
        return ptype == "ollama" and _same_server(row_url or DEFAULT_BASE_URLS["ollama"], base_url)
    if info["host_hint"]:
        return ptype == "openai_compat" and info["host_hint"] in row_url
    # LM Studio и произвольный сервер - узнаём по совпадению адреса
    return ptype == "openai_compat" and bool(row_url) and _same_server(row_url, base_url)


# Один и тот же сервис под двумя адресами - чтобы ключ, сохранённый для
# api.together.xyz, подходил и для каталога по api.together.ai
_HOST_ALIASES = {"api.together.xyz": "api.together.ai"}


def _key_destination(url: str) -> str:
    try:
        netloc = (urlparse((url or "").strip()).netloc or "").lower()
    except ValueError:
        return ""
    return _HOST_ALIASES.get(netloc, netloc)


def _resolve_api_key(source: str, base_url: str, api_key: str, model_id) -> tuple:
    """Возвращает (ключ, откуда_взят). Ключ никогда не уходит в браузер -
    используется только для запроса каталога с сервера.

    Сохранённый ключ используется, ТОЛЬКО если адрес каталога указывает на тот
    же сервер, для которого ключ сохранён. Раньше ключ подбирался по провайдеру,
    а запрос шёл на адрес из поля каталога - вписав туда чужой адрес (или просто
    опечатавшись), можно было отправить настоящий ключ постороннему серверу
    (аудит 2026-10-03)."""
    if api_key:
        return api_key, "form"
    candidates = []
    if model_id:
        row = db.get_model(model_id)
        if row:
            candidates.append(row)
    candidates.extend(db.list_models())
    target = _key_destination(base_url)
    for row in candidates:
        key = row.get("api_key") or ""
        if not key or not _row_matches_source(row, source, base_url):
            continue
        if source == "gemini" or _key_destination(row.get("base_url") or "") == target:
            return key, "saved"
    return "", None


_LOCAL_HOSTS = ("localhost", "127.0.0.1")


def _docker_hint(url: str) -> str:
    if _host(url) not in _LOCAL_HOSTS:
        return ""
    return (
        " Если программа запущена в Docker - «localhost» внутри контейнера означает "
        "сам контейнер, а не ваш компьютер: укажите адрес через host.docker.internal "
        f"(например, {url.replace('localhost', 'host.docker.internal').replace('127.0.0.1', 'host.docker.internal')})."
    )


def _connection_hint(source: str, url: str) -> str:
    label = SOURCE_INFO[source]["label"]
    if source not in ("ollama", "lmstudio"):
        hint = f"Не удалось подключиться к {label} ({url})."
        if _host(url) in _LOCAL_HOSTS:
            return hint + " Проверьте, что сервер запущен." + _docker_hint(url)
        return hint + " Проверьте адрес и что у сервера с программой есть доступ в интернет."
    app_name = label
    hint = (
        f"Не удалось подключиться к {app_name} по адресу {url}. Проверьте, что {app_name} "
        f"запущен"
    )
    if source == "lmstudio":
        hint += " и в нём включён сервер (вкладка «Developer» → «Start Server»)"
    hint += "."
    return hint + _docker_hint(url)


def _suggest_max_tokens(is_reasoning: bool, max_output, context_length) -> int:
    """Стартовое значение «Максимум токенов ответа» для новой модели.
    4000 - как у остальных моделей по умолчанию; рассуждающим моделям сразу
    вдвое больше, потому что часть лимита у них уходит на внутренние
    рассуждения (именно из-за этого раньше обрезались ответы GPT-OSS).
    Значение не превышает то, что модель вообще умеет выдать, и для моделей
    с маленьким контекстом оставляет место под сам промт."""
    value = 8000 if is_reasoning else 4000
    if isinstance(max_output, int) and max_output > 0:
        value = min(value, max_output)
    if isinstance(context_length, int) and 0 < context_length < 16000:
        value = min(value, max(256, context_length // 2))
    return max(256, int(value))


# ---------------------------- OpenRouter ----------------------------

def _per_million(value):
    """OpenRouter отдаёт цену за ОДИН токен строкой ("0.0000003"); в
    интерфейсе привычнее цена за 1 млн токенов. Отрицательное значение
    ("-1") у служебных моделей вроде openrouter/auto означает "цена зависит
    от того, куда уйдёт запрос" - считаем её неизвестной."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None
    return round(f * 1_000_000, 4)


def _is_text_chat_model(m: dict) -> bool:
    mid = (m.get("id") or "").lower()
    if "embed" in mid or "rerank" in mid:
        return False
    arch = m.get("architecture") or {}
    outs = arch.get("output_modalities") or []
    ins = arch.get("input_modalities") or []
    if outs and "text" not in outs:
        return False
    if ins and "text" not in ins:
        return False
    return True


def _normalize_openrouter(m: dict, base_url: str, rank) -> dict:
    pricing = m.get("pricing") or {}
    price_in = _per_million(pricing.get("prompt"))
    price_out = _per_million(pricing.get("completion"))
    mid = m.get("id") or ""
    is_free = mid.endswith(":free") or (price_in == 0 and price_out == 0)
    params = m.get("supported_parameters") or []
    is_reasoning = "reasoning" in params or "include_reasoning" in params
    top = m.get("top_provider") or {}
    max_output = top.get("max_completion_tokens")
    max_output = max_output if isinstance(max_output, int) else None
    context = m.get("context_length") or top.get("context_length")
    context = context if isinstance(context, int) else None
    description = (m.get("description") or "").strip().replace("\n", " ")
    if len(description) > 220:
        description = description[:217].rstrip() + "..."
    return {
        "id": mid,
        "name": m.get("name") or mid,
        "description": description,
        "context_length": context,
        "max_output": max_output,
        "price_in": price_in,
        "price_out": price_out,
        "is_free": bool(is_free),
        "is_reasoning": bool(is_reasoning),
        "created": m.get("created") if isinstance(m.get("created"), (int, float)) else None,
        "popularity_rank": rank,
        "expires": m.get("expiration_date") or None,
        "details": "",
        "provider_type": "openai_compat",
        "base_url": base_url,
        "suggested_max_tokens": _suggest_max_tokens(is_reasoning, max_output, context),
    }


def _created_desc(models: list) -> bool:
    """True, если поле created идёт по невозрастанию - признак того, что
    сервер действительно применил sort=newest (а не проигнорировал его)."""
    values = [m.get("created") for m in models if isinstance(m.get("created"), (int, float))]
    if len(values) < 5:
        return False
    return all(values[i] >= values[i + 1] for i in range(len(values) - 1))


async def _openrouter_get(client, url, sort, api_key):
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return await client.get(url, params={"sort": sort}, headers=headers, timeout=CATALOG_TIMEOUT)


async def _fetch_openrouter(base_url: str, api_key: str) -> dict:
    url = f"{base_url.rstrip('/')}/models"
    warnings = []
    try:
        async with httpx.AsyncClient() as client:
            r_pop, r_new = await asyncio.gather(
                _openrouter_get(client, url, "most-popular", api_key),
                _openrouter_get(client, url, "newest", api_key),
            )
            if api_key and (r_pop.status_code in (401, 403) or r_new.status_code in (401, 403)):
                # ключ не приняли - список всё равно доступен публично, просто
                # без гарантии сортировки по популярности
                warnings.append(
                    f"OpenRouter не принял сохранённый ключ (HTTP {r_pop.status_code}) - "
                    f"каталог загружен без ключа."
                )
                api_key = ""
                r_pop, r_new = await asyncio.gather(
                    _openrouter_get(client, url, "most-popular", ""),
                    _openrouter_get(client, url, "newest", ""),
                )
    except httpx.TimeoutException:
        raise CatalogError(f"OpenRouter не ответил за {CATALOG_TIMEOUT} сек - попробуйте ещё раз чуть позже.")
    except httpx.RequestError:
        raise CatalogError(_connection_hint("openrouter", url))

    if r_pop.status_code >= 400:
        raise CatalogError(f"OpenRouter вернул ошибку HTTP {r_pop.status_code}: {r_pop.text[:300]}")
    try:
        popular = (r_pop.json() or {}).get("data") or []
    except ValueError:
        raise CatalogError("OpenRouter вернул ответ не в формате JSON.")
    try:
        newest = (r_new.json() or {}).get("data") or [] if r_new.status_code < 400 else []
    except ValueError:
        newest = []

    sort_honored = _created_desc(newest)
    popular_ids = [m.get("id") for m in popular]
    newest_ids = [m.get("id") for m in newest]
    popularity_available = sort_honored and popular_ids != newest_ids
    if not popularity_available:
        if api_key:
            warnings.append(
                "OpenRouter не применил сортировку по популярности - показан его порядок по умолчанию."
            )
        else:
            warnings.append(
                "Сортировка по популярности недоступна без ключа OpenRouter: без ключа сервер "
                "игнорирует её и отдаёт список в своём порядке по умолчанию. Добавьте хотя бы "
                "одну модель OpenRouter с ключом (или впишите ключ в форму) - и популярность появится."
            )

    models = []
    rank = 0
    for m in popular:
        if not _is_text_chat_model(m):
            continue
        rank += 1
        models.append(_normalize_openrouter(m, base_url, rank if popularity_available else None))
        models[-1]["default_order"] = rank
    return {"models": models, "popularity_available": popularity_available, "warnings": warnings,
            "used_key": bool(api_key)}


# ---------------------------- Ollama ----------------------------

def _human_size(num_bytes) -> str:
    if not isinstance(num_bytes, (int, float)) or num_bytes <= 0:
        return ""
    gb = num_bytes / (1024 ** 3)
    return f"{gb:.1f} ГБ" if gb >= 1 else f"{num_bytes / (1024 ** 2):.0f} МБ"


def _iso_to_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def _fetch_ollama(base_url: str) -> dict:
    root = _root(base_url)
    url = f"{root}/api/tags"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=CATALOG_TIMEOUT)
    except httpx.TimeoutException:
        raise CatalogError(f"Ollama по адресу {root} не ответил за {CATALOG_TIMEOUT} сек.")
    except httpx.RequestError:
        raise CatalogError(_connection_hint("ollama", root))
    if resp.status_code >= 400:
        raise CatalogError(
            f"Ollama вернул HTTP {resp.status_code} на {url} - проверьте, что адрес указывает именно на Ollama."
        )
    try:
        items = (resp.json() or {}).get("models") or []
    except ValueError:
        raise CatalogError(f"По адресу {url} ответил не Ollama (ответ не JSON).")

    models = []
    for i, m in enumerate(items, start=1):
        name = m.get("name") or m.get("model") or ""
        if not name or "embed" in name.lower():
            continue  # модели-эмбеддинги текст не пишут
        det = m.get("details") or {}
        details = " · ".join(x for x in [det.get("parameter_size"), det.get("quantization_level"), _human_size(m.get("size"))] if x)
        models.append({
            "id": name, "name": name, "description": "", "context_length": None, "max_output": None,
            "price_in": None, "price_out": None, "is_free": True, "is_reasoning": False,
            "created": _iso_to_ts(m.get("modified_at")), "popularity_rank": None, "expires": None,
            "details": details, "provider_type": "ollama", "base_url": root,
            "suggested_max_tokens": 4000, "default_order": i,
        })
    warnings = []
    if not models:
        warnings.append("Ollama доступен, но в нём нет ни одной скачанной текстовой модели - скачайте модель командой «ollama pull <имя>».")
    return {"models": models, "popularity_available": False, "warnings": warnings, "used_key": False}


# ---------------------------- LM Studio ----------------------------

async def _fetch_lmstudio(base_url: str, api_key: str) -> dict:
    root = _root(base_url)
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # Сначала "родной" REST API LM Studio (/api/v0/models) - в нём есть тип
    # модели (llm/vlm/embeddings), загружена ли она сейчас и размер контекста.
    # Если версия LM Studio его не знает - откатываемся к OpenAI-совместимому
    # /v1/models, где есть только id.
    try:
        async with httpx.AsyncClient() as client:
            rich = None
            try:
                r = await client.get(f"{root}/api/v0/models", headers=headers, timeout=CATALOG_TIMEOUT)
                if r.status_code < 400:
                    rich = (r.json() or {}).get("data")
            except ValueError:
                rich = None
            if rich is None:
                r = await client.get(f"{root}/v1/models", headers=headers, timeout=CATALOG_TIMEOUT)
                if r.status_code >= 400:
                    raise CatalogError(
                        f"LM Studio вернул HTTP {r.status_code} на {root}/v1/models - проверьте адрес и что сервер в LM Studio запущен."
                    )
                try:
                    basic = (r.json() or {}).get("data") or []
                except ValueError:
                    raise CatalogError(f"По адресу {root} ответил не LM Studio (ответ не JSON).")
    except httpx.TimeoutException:
        raise CatalogError(f"LM Studio по адресу {root} не ответил за {CATALOG_TIMEOUT} сек.")
    except httpx.RequestError:
        raise CatalogError(_connection_hint("lmstudio", f"{root}/v1"))

    models = []
    if rich is not None:
        for i, m in enumerate(rich, start=1):
            mid = m.get("id") or ""
            if not mid or m.get("type") == "embeddings" or "embed" in mid.lower():
                continue
            ctx = m.get("max_context_length")
            ctx = ctx if isinstance(ctx, int) else None
            state = m.get("state")
            details = " · ".join(x for x in [
                m.get("arch"), m.get("quantization"),
                "загружена" if state == "loaded" else ("не загружена" if state == "not-loaded" else None),
            ] if x)
            models.append({
                "id": mid, "name": mid, "description": "", "context_length": ctx, "max_output": None,
                "price_in": None, "price_out": None, "is_free": True, "is_reasoning": False,
                "created": None, "popularity_rank": None, "expires": None, "details": details,
                "provider_type": "openai_compat", "base_url": f"{root}/v1",
                "suggested_max_tokens": _suggest_max_tokens(False, None, ctx), "default_order": i,
            })
    else:
        for i, m in enumerate(basic, start=1):
            mid = m.get("id") or ""
            if not mid or "embed" in mid.lower():
                continue
            models.append({
                "id": mid, "name": mid, "description": "", "context_length": None, "max_output": None,
                "price_in": None, "price_out": None, "is_free": True, "is_reasoning": False,
                "created": None, "popularity_rank": None, "expires": None, "details": "",
                "provider_type": "openai_compat", "base_url": f"{root}/v1",
                "suggested_max_tokens": 4000, "default_order": i,
            })
    warnings = []
    if not models:
        warnings.append("LM Studio доступен, но в нём нет ни одной текстовой модели - скачайте модель во вкладке поиска LM Studio.")
    return {"models": models, "popularity_available": False, "warnings": warnings, "used_key": False}


# ---------------------------- облачные провайдеры (Groq, Gemini, Mistral, ...) ----------------------------

# Модели, которые не пишут текст в чате (распознавание речи, синтез речи,
# картинки, эмбеддинги, модерация и т.п.) - в каталоге для генерации
# рацпредложений они бесполезны.
_NON_CHAT_RE = re.compile(
    r"(embed|whisper|tts|speech|transcri|audio|dall-e|image|imagen|veo|sora|moderation|"
    r"guard|rerank|ocr|realtime|orpheus|playai|aqa|robotics|computer-use|babbage|davinci)",
    re.IGNORECASE,
)

# Модели, которые тратят часть лимита на внутренние рассуждения - для них
# сразу предлагается увеличенный «Максимум токенов ответа». Эвристика по
# названию: у Groq/Cerebras/Mistral/DeepSeek API признака «рассуждающая» нет.
_REASONING_RE = re.compile(
    r"(gpt-oss|qwq|deepseek-r1|r1-distill|reasoner|reasoning|thinking|magistral|qwen3|"
    r"(^|[/-])o[134](-mini|-pro)?($|-))",
    re.IGNORECASE,
)


def _looks_reasoning(model_id: str) -> bool:
    return bool(_REASONING_RE.search(model_id or ""))


def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _item(source: str, mid: str, base_url: str, order: int, *, name=None, context=None, max_output=None,
          price_in=None, price_out=None, is_free=None, is_reasoning=None, created=None, details="",
          description="", expires=None) -> dict:
    """Нормализованная строка каталога - одинаковая для всех провайдеров."""
    info = SOURCE_INFO[source]
    if is_reasoning is None:
        is_reasoning = _looks_reasoning(mid)
    if is_free is None:
        is_free = info["is_free_default"]
    context = _int_or_none(context)
    max_output = _int_or_none(max_output)
    description = (description or "").strip().replace("\n", " ")
    if len(description) > 220:
        description = description[:217].rstrip() + "..."
    return {
        "id": mid,
        "name": name or mid,
        "description": description,
        "context_length": context,
        "max_output": max_output,
        "price_in": price_in,
        "price_out": price_out,
        "is_free": is_free,
        "is_reasoning": bool(is_reasoning),
        "created": created if isinstance(created, (int, float)) and created > 0 else None,
        "popularity_rank": None,
        "expires": expires or None,
        "details": details,
        "provider_type": info["provider_type"],
        "base_url": base_url,
        "suggested_max_tokens": _suggest_max_tokens(bool(is_reasoning), max_output, context),
        "default_order": order,
    }


def _http_error_message(source: str, code: int, body: str, url: str) -> str:
    info = SOURCE_INFO[source]
    label = info["label"]
    low = (body or "").lower()
    if "network settings" in low:
        # проверяем раньше "ключа": в этом ответе нет ничего про ключ, но 403
        # иначе легко принять за проблему с доступом по ключу
        return f"{label}: " + NETWORK_BLOCKED_HINT.format(code=code)
    if code == 401 or (code in (400, 403) and any(s in low for s in ("api key", "api_key", "apikey", "unauthorized", "invalid key", "authentication"))):
        msg = f"{label} не принял API-ключ (HTTP {code}) - проверьте, что ключ верный и не отозван."
        if info["key_url"]:
            msg += f" Получить новый ключ: {info['key_url']}"
        return msg
    if any(s in low for s in ("location", "region", "country", "territory")):
        return (
            f"{label} отказал в доступе по региону (HTTP {code}) - сервис недоступен из страны, "
            f"где работает сервер программы. Это ограничение провайдера, а не ошибка ключа."
        )
    if code == 404:
        return f"{label}: по адресу {url} список моделей не найден (HTTP 404) - проверьте адрес."
    if code == 429:
        return f"{label}: слишком много запросов (HTTP 429) - подождите немного и нажмите «Обновить»."
    return f"{label} вернул ошибку HTTP {code}: {(body or '')[:200]}"


async def _get_json(source: str, url: str, api_key: str = "", params: dict = None, auth: str = "bearer"):
    """GET с понятными сообщениями об ошибках. auth="query" - ключ в параметре
    ?key=... (Gemini); в тексте ошибок адрес выводится БЕЗ ключа."""
    headers = {"User-Agent": USER_AGENT}
    params = dict(params or {})
    if api_key:
        if auth == "query":
            params["key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params=params, timeout=CATALOG_TIMEOUT)
    except httpx.TimeoutException:
        raise CatalogError(f"{SOURCE_INFO[source]['label']} не ответил за {CATALOG_TIMEOUT} сек - попробуйте ещё раз чуть позже.")
    except httpx.RequestError:
        raise CatalogError(_connection_hint(source, url))
    if resp.status_code >= 400:
        raise CatalogError(_http_error_message(source, resp.status_code, resp.text, url))
    try:
        return resp.json()
    except ValueError:
        raise CatalogError(f"{SOURCE_INFO[source]['label']}: по адресу {url} ответ не в формате JSON - проверьте адрес.")


def _data_list(payload) -> list:
    """OpenAI-совместимые API отдают {"data": [...]}, Together - голый массив."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def _result(models: list, warnings=None, used_key=False) -> dict:
    return {"models": models, "popularity_available": False, "warnings": warnings or [], "used_key": used_key}


async def _fetch_groq(base_url: str, api_key: str) -> dict:
    data = _data_list(await _get_json("groq", f"{base_url.rstrip('/')}/models", api_key))
    models = []
    for m in data:
        mid = m.get("id") or ""
        if not mid or m.get("active") is False or _NON_CHAT_RE.search(mid):
            continue
        models.append(_item(
            "groq", mid, base_url, len(models) + 1,
            context=m.get("context_window"), max_output=m.get("max_completion_tokens"),
            created=m.get("created"), details=m.get("owned_by") or "",
        ))
    return _result(models, used_key=True)


async def _fetch_gemini(base_url: str, api_key: str) -> dict:
    url = f"{SOURCE_INFO['gemini']['default_url']}/models"
    items, token = [], None
    for _ in range(5):  # постраничная выдача; 5 страниц по 1000 - с огромным запасом
        params = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        page = await _get_json("gemini", url, api_key, params=params, auth="query")
        items.extend(page.get("models") or [])
        token = page.get("nextPageToken")
        if not token:
            break
    models = []
    for m in items:
        name = m.get("name") or ""
        # драйвер обращается к models/{id}:generateContent - id без префикса "models/"
        mid = name[len("models/"):] if name.startswith("models/") else name
        methods = m.get("supportedGenerationMethods") or []
        if not mid or "generateContent" not in methods or _NON_CHAT_RE.search(mid):
            continue
        thinking = m.get("thinking")
        models.append(_item(
            "gemini", mid, "", len(models) + 1,
            name=m.get("displayName") or mid, description=m.get("description"),
            context=m.get("inputTokenLimit"), max_output=m.get("outputTokenLimit"),
            is_reasoning=thinking if isinstance(thinking, bool) else None,
        ))
    return _result(models, used_key=True)


async def _fetch_mistral(base_url: str, api_key: str) -> dict:
    data = _data_list(await _get_json("mistral", f"{base_url.rstrip('/')}/models", api_key))
    models = []
    for m in data:
        mid = m.get("id") or ""
        caps = m.get("capabilities") or {}
        if not mid or m.get("archived") or caps.get("completion_chat") is False or _NON_CHAT_RE.search(mid):
            continue
        canonical = m.get("name") or ""
        details = f"псевдоним для {canonical}" if canonical and canonical != mid else ""
        deprecation = m.get("deprecation")
        models.append(_item(
            "mistral", mid, base_url, len(models) + 1,
            description=m.get("description"), context=m.get("max_context_length"),
            created=m.get("created"), details=details,
            expires=str(deprecation)[:10] if deprecation else None,
        ))
    return _result(models, used_key=True)


async def _fetch_deepseek(base_url: str, api_key: str) -> dict:
    data = _data_list(await _get_json("deepseek", f"{base_url.rstrip('/')}/models", api_key))
    models = []
    for m in data:
        mid = m.get("id") or ""
        if not mid or _NON_CHAT_RE.search(mid):
            continue
        outs = m.get("output_modalities") or []
        if outs and "text" not in outs:
            continue
        effort = m.get("effort") or {}
        has_thinking = bool(isinstance(effort, dict) and effort.get("supported_levels"))
        models.append(_item(
            "deepseek", mid, base_url, len(models) + 1,
            name=m.get("name") or mid, context=m.get("context_window"),
            max_output=m.get("max_output_tokens"),
            is_reasoning=True if (has_thinking or "reasoner" in mid) else None,
        ))
    return _result(models, used_key=True)


async def _fetch_together(base_url: str, api_key: str) -> dict:
    data = _data_list(await _get_json("together", f"{base_url.rstrip('/')}/models", api_key))
    models = []
    for m in data:
        mid = m.get("id") or ""
        if not mid or m.get("type") not in ("chat", "language", "code") or _NON_CHAT_RE.search(mid):
            continue
        pricing = m.get("pricing") or {}
        price_in, price_out, hourly = pricing.get("input"), pricing.get("output"), pricing.get("hourly") or 0
        price_in = price_in if isinstance(price_in, (int, float)) and price_in >= 0 else None
        price_out = price_out if isinstance(price_out, (int, float)) and price_out >= 0 else None
        if price_in == 0 and price_out == 0 and hourly > 0:
            continue  # доступна только на выделенном сервере с почасовой оплатой, не по обычному API
        is_free = mid.endswith("-Free") or (price_in == 0 and price_out == 0)
        models.append(_item(
            "together", mid, base_url, len(models) + 1,
            name=m.get("display_name") or mid, context=m.get("context_length"),
            price_in=price_in, price_out=price_out, is_free=is_free,
            created=m.get("created"), details=m.get("organization") or "",
        ))
    return _result(models, used_key=True)


async def _fetch_cerebras(base_url: str, api_key: str) -> dict:
    data = _data_list(await _get_json("cerebras", f"{base_url.rstrip('/')}/models", api_key))
    models = []
    for m in data:
        mid = m.get("id") or ""
        if not mid or _NON_CHAT_RE.search(mid):
            continue
        models.append(_item("cerebras", mid, base_url, len(models) + 1,
                            created=m.get("created"), details=m.get("owned_by") or ""))
    return _result(models, used_key=True)


async def _fetch_custom(base_url: str, api_key: str) -> dict:
    """Любой OpenAI-совместимый сервер: GET {адрес}/models -> {"data":[{"id":...}]}.
    Подходит для OpenAI, Fireworks, Hugging Face router, своего vLLM и т.п."""
    url = f"{base_url.rstrip('/')}/models"
    data = _data_list(await _get_json("custom", url, api_key))
    if not data:
        raise CatalogError(
            f"По адресу {url} не найден список моделей в формате OpenAI ({{\"data\": [...]}}). "
            f"Обычно адрес должен заканчиваться на /v1 (например, https://api.openai.com/v1)."
        )
    models = []
    for m in data:
        mid = (m.get("id") if isinstance(m, dict) else None) or ""
        if not mid or _NON_CHAT_RE.search(mid):
            continue
        models.append(_item(
            "custom", mid, base_url, len(models) + 1,
            context=m.get("context_length") or m.get("context_window") or m.get("max_model_len"),
            created=m.get("created"), details=m.get("owned_by") or "",
        ))
    warnings = []
    if not api_key:
        warnings.append("Запрос выполнен без API-ключа - если сервер его требует, впишите ключ в форму модели.")
    return _result(models, warnings, used_key=bool(api_key))


_CLOUD_FETCHERS = {
    "groq": _fetch_groq,
    "gemini": _fetch_gemini,
    "mistral": _fetch_mistral,
    "deepseek": _fetch_deepseek,
    "together": _fetch_together,
    "cerebras": _fetch_cerebras,
    "custom": _fetch_custom,
}


# ---------------------------- точка входа ----------------------------

def _mark_already_added(source: str, base_url: str, models: list) -> list:
    """Помечает модели, которые уже есть в списке нейросетей программы, -
    чтобы случайно не добавить одну и ту же модель дважды."""
    existing = {}
    # если одна и та же модель заведена несколько раз (например, ваша копия и
    # выключенная заготовка) - показываем включённую с наивысшим приоритетом
    rows = sorted(db.list_models(), key=lambda r: (not r.get("enabled"), r.get("priority") or 0))
    for row in rows:
        if row.get("model") and _row_matches_source(row, source, base_url):
            existing.setdefault(row["model"], row["name"])
    out = []
    for m in models:
        m = dict(m)
        m["already_added_as"] = existing.get(m["id"])
        out.append(m)
    return out


def _missing_key_message(source: str) -> str:
    info = SOURCE_INFO[source]
    return (
        f"Чтобы получить список моделей {info['label']}, нужен API-ключ. Впишите его в поле "
        f"«API-ключ» в форме модели (сохранять заранее не обязательно) и нажмите «Обновить» - "
        f"или сохраните хотя бы одну модель {info['label']} с ключом, тогда он подставится сам. "
        f"Получить ключ: {info['key_url']}"
    )


async def get_catalog(source: str, base_url: str = "", api_key: str = "", model_id=None, refresh: bool = False) -> dict:
    if source not in SOURCES:
        raise CatalogError(f"Неизвестный источник каталога: {source}", 400)
    info = SOURCE_INFO[source]
    if source == "gemini":
        base_url = info["default_url"]  # драйвер Gemini всё равно ходит только сюда
    base_url = (base_url or "").strip() or info["default_url"]
    if not base_url:
        raise CatalogError(
            "Укажите адрес OpenAI-совместимого сервера - обычно он заканчивается на /v1 "
            "(например, https://api.openai.com/v1).", 400,
        )
    if not base_url.lower().startswith(("http://", "https://")):
        raise CatalogError("Адрес должен начинаться с http:// или https://", 400)

    key, key_source = ("", None)
    if source != "ollama":
        key, key_source = _resolve_api_key(source, base_url, api_key, model_id)
    if info["needs_key"] and not key:
        raise CatalogError(_missing_key_message(source), 400)

    cache_key = (source, _root(base_url).lower(), bool(key))
    cached = _cache.get(cache_key)
    now = time.time()
    if cached and not refresh and now - cached[0] < CACHE_TTL_SECONDS[source]:
        result, from_cache, fetched_at = cached[1], True, cached[0]
    else:
        if source == "openrouter":
            result = await _fetch_openrouter(base_url.rstrip("/"), key)
        elif source == "ollama":
            result = await _fetch_ollama(base_url)
        elif source == "lmstudio":
            result = await _fetch_lmstudio(base_url, key)
        else:
            result = await _CLOUD_FETCHERS[source](base_url, key)
        _cache[cache_key] = (now, result)
        from_cache, fetched_at = False, now

    return {
        "source": source,
        "base_url": base_url,
        "pricing_mode": info["pricing_mode"],
        "price_note": info["price_note"],
        "models": _mark_already_added(source, base_url, result["models"]),
        "popularity_available": result["popularity_available"],
        "warnings": result["warnings"],
        "used_key": result["used_key"],
        "key_source": key_source if result["used_key"] else None,
        "from_cache": from_cache,
        "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).replace(tzinfo=None).isoformat(),
    }
