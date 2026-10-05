import asyncio
import base64
import hmac
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import catalog
import db
import llm_providers
import docx_builder

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "..", "data")
OUTPUT_DIR = os.path.abspath(os.path.join(DATA_DIR, "output"))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))

os.makedirs(OUTPUT_DIR, exist_ok=True)
db.init_db()

app = FastAPI(title="Генератор рационализаторских предложений")

# CORS намеренно НЕ включён. Интерфейс отдаётся этим же сервером (тот же адрес),
# поэтому разрешение на запросы с других сайтов программе не нужно. Раньше
# здесь стояло allow_origins=["*"] - это позволяло ЛЮБОЙ открытой в браузере
# странице читать базу авторов, удалять записи и (через смену адреса модели
# или каталог) уводить сохранённые API-ключи на свой сервер (аудит 2026-10-03).


# ---------------------------- защита доступа (HTTP Basic) ----------------------------
# По умолчанию программа НЕ требует пароль - чтобы не сломать уже работающие
# установки при обновлении. Но раньше (до этого аудита) API-ключи нейросетей
# и персональные данные авторов (ФИО, звание, место службы) отдавались любому,
# кто достанет до порта контейнера, без единой проверки. Если задать переменные
# окружения APP_USERNAME и APP_PASSWORD (см. docker-compose.yml / README) -
# всё приложение (кроме /api/health, нужного для HEALTHCHECK) закрывается
# простым Basic Auth. Это не замена полноценной системе ролей, но закрывает
# самый грубый случай "порт торчит в сеть без пароля".
APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_USERNAME and APP_PASSWORD)

if not AUTH_ENABLED:
    print(
        "[ВНИМАНИЕ] Переменные APP_USERNAME/APP_PASSWORD не заданы - приложение "
        "НЕ защищено паролем. Любой, кто достанет до этого порта, увидит данные "
        "авторов и ключи API. Задайте обе переменные, чтобы включить защиту "
        "(см. README, раздел «Защита доступа»)."
    )


@app.middleware("http")
async def basic_auth_guard(request: Request, call_next):
    if not AUTH_ENABLED or request.url.path == "/api/health":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    authorized = False
    if header.startswith("Basic "):
        try:
            raw = base64.b64decode(header[6:], validate=True)
        except Exception:
            raw = b""
        # Сравниваем БАЙТЫ, а не строки: hmac.compare_digest на строках с
        # кириллицей бросает TypeError - раньше это означало, что с паролем
        # вида «Пароль2026» войти было невозможно в принципе. Браузеры кодируют
        # логин/пароль по-разному (обычно UTF-8, старые - Latin-1), поэтому
        # пробуем оба варианта.
        for enc in ("utf-8", "latin-1"):
            try:
                user, _, pwd = raw.decode(enc).partition(":")
            except UnicodeDecodeError:
                continue
            if hmac.compare_digest(user.encode("utf-8"), APP_USERNAME.encode("utf-8")) and \
                    hmac.compare_digest(pwd.encode("utf-8"), APP_PASSWORD.encode("utf-8")):
                authorized = True
                break
    if not authorized:
        # realm обязан быть ASCII - HTTP-заголовки кодируются в latin-1, и
        # кириллица здесь валит сам ответ 401 с UnicodeEncodeError (найдено
        # тестом) вместо того, чтобы просто попросить пароль
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="RAC generator"'})
    return await call_next(request)


# ---------------------------- маскирование API-ключей в ответах ----------------------------
# GET /api/models (и любой другой ответ, несущий модель целиком) больше не
# отдаёт api_key открытым текстом - раньше его было видно в обычной вкладке
# "Сеть" браузера у кого угодно, кто открыл страницу. Полный ключ остаётся
# только внутри БД и используется напрямую llm_providers при генерации -
# наружу (в JSON-ответах) выходит только маска.

def _mask_api_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 7:
        return "•" * len(key)
    return f"{key[:3]}…{key[-3:]}"


def _public_model(row: dict) -> dict:
    row = dict(row)
    real_key = row.get("api_key") or ""
    row["api_key_set"] = bool(real_key)
    row["api_key"] = _mask_api_key(real_key)
    return row


def _public_models(rows: list) -> list:
    return [_public_model(r) for r in rows]


def _key_destination(provider_type: str, base_url: str) -> str:
    """Куда фактически уйдёт ключ модели: Gemini - всегда к API Google (адрес
    у таких моделей не используется), остальные - на хост:порт из base_url."""
    if provider_type == "gemini":
        return "gemini"
    from urllib.parse import urlparse
    try:
        return (urlparse((base_url or "").strip()).netloc or "").lower()
    except ValueError:
        return ""


# ---------------------------- schemas ----------------------------

class AuthorIn(BaseModel):
    rank: str = ""
    full_name: str
    position: str = ""
    workplace: str = ""


class GenerateIn(BaseModel):
    topic: str
    extra_context: str = ""
    preferred_model_id: int | None = None
    request_id: str | None = None


class CancelGenerationIn(BaseModel):
    request_id: str


MIN_MAX_TOKENS = 256  # ниже этого значения почти ни одна модель не успеет дописать даже заголовок JSON


class ModelIn(BaseModel):
    name: str
    provider_type: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    is_paid: bool = False
    priority: int = 100
    max_tokens: int = 4000

    @field_validator("max_tokens")
    @classmethod
    def _validate_max_tokens(cls, v):
        # 0 (и любое "пустое"/отрицательное значение) раньше тихо отключало
        # лимит: "if max_tokens: payload['max_tokens'] = max_tokens" в
        # llm_providers.py попросту пропускал ключ при falsy-значении, и
        # запрос уходил провайдеру без ограничения вовсе - неожидано и
        # по-разному вело себя у разных провайдеров. Сама логика драйверов
        # (у каждого провайдера свой параметр: max_tokens / num_predict /
        # maxOutputTokens) не трогается - фикс только на границе ввода, чтобы
        # не задеть отправку запроса на генерацию.
        if v < MIN_MAX_TOKENS:
            raise ValueError(f"Максимум токенов ответа должен быть не меньше {MIN_MAX_TOKENS}")
        return v


class ModelUpdateIn(ModelIn):
    enabled: bool = True
    # Если False - значение поля api_key в этом запросе ИГНОРИРУЕТСЯ и в БД
    # остаётся прежний ключ. Это обязательно, раз GET /api/models больше не
    # возвращает ключ открытым текстом (см. _mask_api_key) - фронтенд физически
    # не может переслать обратно то, чего у него нет, поэтому по умолчанию
    # ключ не меняется, и явно меняется только когда пользователь впишет новый.
    api_key_changed: bool = False


class CatalogIn(BaseModel):
    source: str  # ключ из catalog.SOURCE_INFO: openrouter, groq, gemini, ..., custom
    base_url: str = ""
    # Ключ, только что вписанный в форму (ещё не сохранённый). Передаётся в
    # теле POST-запроса, а не в адресе - чтобы не оседал в логах доступа.
    api_key: str = ""
    # id редактируемой модели - её сохранённый ключ можно использовать для
    # запроса каталога, не пересылая сам ключ в браузер и обратно
    model_id: int | None = None
    refresh: bool = False


class SavedEndpointIn(BaseModel):
    label: str
    url: str


class SignatoryIn(BaseModel):
    full_name: str


class AuthorAssignment(BaseModel):
    author_id: int
    # 0..100: раньше проверялась только сумма, и набор 150, -50, 0, 0, 0, 0
    # проходил - в документ попадало «-50 процентов»
    percent: int = Field(..., ge=0, le=100)


class ProposalIn(BaseModel):
    title: str
    sections: dict  # section1..section6
    authors: list[AuthorAssignment] = Field(..., min_length=6, max_length=6)
    vice_head: str = "Врио начальника"
    vice_fio: str = "И.Иванов"
    topic: str = ""
    model_used: str = ""


# ---------------------------- понятные ошибки проверки ввода ----------------------------
# Стандартный ответ FastAPI на неверные данные (422) - список объектов на
# английском. Интерфейс показывал его как «Ошибка: [object Object]». Теперь
# сервер сам формирует одну понятную строку по-русски.

_FIELD_LABELS = {
    "percent": "Процент вклада", "max_tokens": "Максимум токенов ответа", "priority": "Приоритет",
    "author_id": "Автор", "authors": "Авторы", "title": "Наименование", "sections": "Разделы",
    "name": "Название", "provider_type": "Тип API", "model": "Модель", "full_name": "ФИО",
    "topic": "Тема", "source": "Источник каталога", "base_url": "Адрес",
}


def _validation_message(err: dict) -> str:
    loc = [p for p in err.get("loc", []) if p not in ("body", "query", "path")]
    field = next((p for p in reversed(loc) if isinstance(p, str)), "")
    label = _FIELD_LABELS.get(field, field or "Значение")
    if field == "percent" and len(loc) >= 2 and isinstance(loc[-2], int):
        label = f"Процент у автора {loc[-2] + 1}"
    t = err.get("type", "")
    ctx = err.get("ctx") or {}
    if t in ("int_parsing", "int_from_float", "int_type"):
        return f"{label}: нужно целое число"
    if t == "greater_than_equal":
        return f"{label}: не меньше {ctx.get('ge')}"
    if t == "less_than_equal":
        return f"{label}: не больше {ctx.get('le')}"
    if t == "missing":
        return f"{label}: обязательное поле"
    if t in ("too_short", "too_long") and field == "authors":
        return "Нужно ровно 6 авторов"
    if t == "value_error":  # сообщение из наших собственных проверок (field_validator)
        return str(ctx.get("error") or err.get("msg", "")).replace("Value error, ", "")
    return f"{label}: {err.get('msg', 'неверное значение')}"


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    messages = list(dict.fromkeys(_validation_message(e) for e in exc.errors()))
    return JSONResponse(status_code=422, content={"detail": "; ".join(messages)})


# ---------------------------- health ----------------------------

# Версия кода сервера. Должна совпадать с APP_VERSION в frontend/app.js -
# при каждом обновлении меняются обе. Нужна потому, что интерфейс (frontend/)
# сервер читает с диска при каждом запросе, а свой Python-код - только один
# раз при запуске: если обновить файлы и не перезапустить сервер (или не
# пересобрать Docker-образ), браузер получит новый интерфейс, а отвечать ему
# будет старый код - новые функции тогда падают с невнятным "эндпоинт не
# существует". Интерфейс сравнивает версии и прямо говорит, что делать.
APP_VERSION = "2026.10.05-about"


@app.get("/api/health")
def api_health():
    return {"status": "ok", "version": APP_VERSION}


# ---------------------------- authors ----------------------------

@app.get("/api/authors")
def api_list_authors(q: str = ""):
    return db.list_authors(q)


@app.post("/api/authors")
def api_create_author(author: AuthorIn):
    author_id = db.create_author(author.rank, author.full_name, author.position, author.workplace)
    return db.get_author(author_id)


@app.put("/api/authors/{author_id}")
def api_update_author(author_id: int, author: AuthorIn):
    if not db.get_author(author_id):
        raise HTTPException(404, "Автор не найден")
    db.update_author(author_id, author.rank, author.full_name, author.position, author.workplace)
    return db.get_author(author_id)


@app.delete("/api/authors/{author_id}")
def api_delete_author(author_id: int):
    db.delete_author(author_id)
    return {"ok": True}


AUTHOR_CSV_HEADERS_RU = ["Звание", "ФИО", "Должность", "Место службы"]
# распознаём и русские заголовки (как в собственном экспорте), и английские
# названия полей (на случай ручного/программного редактирования файла) -
# без учёта регистра
AUTHOR_CSV_HEADER_MAP = {
    "звание": "rank", "rank": "rank",
    "фио": "full_name", "full_name": "full_name", "фио полностью": "full_name",
    "должность": "position", "position": "position",
    "место службы": "workplace", "workplace": "workplace", "место военной службы (работы)": "workplace",
}


@app.get("/api/authors/export")
def api_export_authors():
    import csv
    import io

    authors = db.list_authors()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(AUTHOR_CSV_HEADERS_RU)
    for a in authors:
        writer.writerow([a["rank"], a["full_name"], a["position"], a["workplace"]])
    # BOM в начале - иначе Excel на Windows не распознаёт UTF-8 и показывает
    # кириллицу битыми символами (частый повод для путаницы у пользователей)
    content = "\ufeff" + buf.getvalue()
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="authors.csv"'},
    )


@app.post("/api/authors/import")
async def api_import_authors(file: UploadFile = File(...)):
    import csv
    import io

    raw = (await file.read()).decode("utf-8-sig", errors="replace")  # -sig сам снимает BOM, если он есть
    await file.close()
    if not raw.strip():
        raise HTTPException(400, "Файл пуст")

    try:
        dialect = csv.Sniffer().sniff(raw[:2000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # запасной вариант - обычная запятая
    reader = csv.reader(io.StringIO(raw), dialect)
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "Файл пуст")

    header = [h.strip().lower() for h in rows[0]]
    field_order = [AUTHOR_CSV_HEADER_MAP.get(h) for h in header]
    if "full_name" not in field_order:
        raise HTTPException(
            400,
            f"Не распознаны заголовки колонок: {rows[0]!r}. Ожидались «Звание, ФИО, Должность, Место службы» "
            f"(порядок неважен, но колонка с ФИО обязательна) - как в файле, который отдаёт «Скачать .csv».",
        )

    created = updated = skipped = 0
    errors = []
    for i, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue  # пустая строка
        values = {}
        for col_idx, field in enumerate(field_order):
            if field and col_idx < len(row):
                values[field] = row[col_idx].strip()
        full_name = values.get("full_name", "")
        if not full_name:
            skipped += 1
            errors.append(f"строка {i}: пропущена - нет ФИО")
            continue
        _, was_created = db.upsert_author_by_name(
            values.get("rank", ""), full_name, values.get("position", ""), values.get("workplace", "")
        )
        if was_created:
            created += 1
        else:
            updated += 1

    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors[:20]}


# ---------------------------- llm models (список нейросетей) ----------------------------

@app.get("/api/models")
def api_list_models():
    return _public_models(db.list_models())


@app.get("/api/models/provider-types")
def api_provider_types():
    return llm_providers.PROVIDER_TYPE_LABELS


@app.post("/api/models")
def api_create_model(m: ModelIn):
    if m.provider_type not in llm_providers.DRIVERS:
        raise HTTPException(400, f"Неизвестный тип провайдера: {m.provider_type}")
    # .strip(): пробелы/перевод строки по краям - частый мусор при вставке ключа
    model_id = db.create_model(m.name, m.provider_type, m.base_url, m.api_key.strip(), m.model, m.is_paid, m.priority, m.max_tokens)
    return _public_model(db.get_model(model_id))


@app.put("/api/models/{model_id}")
def api_update_model(model_id: int, m: ModelUpdateIn):
    existing = db.get_model(model_id)
    if not existing:
        raise HTTPException(404, "Модель не найдена")
    if m.provider_type not in llm_providers.DRIVERS:
        raise HTTPException(400, f"Неизвестный тип провайдера: {m.provider_type}")
    # см. ModelUpdateIn.api_key_changed - без этой проверки сохранение любой
    # другой правки модели (приоритет, галочка "включена" и т.п.) молча
    # затёрло бы реальный ключ маской, которую фронтенд получил из GET-ответа
    api_key = m.api_key.strip() if m.api_key_changed else existing["api_key"]
    # Сохранённый ключ привязан к серверу, для которого его вписали. Если адрес
    # модели меняют на ДРУГОЙ сервер, не вписав ключ заново, старый ключ ушёл
    # бы туда при первой же проверке/генерации - так его можно было увести,
    # даже не видя (аудит 2026-10-03). Поэтому при смене сервера ключ нужно
    # ввести заново (или явно удалить).
    if (not m.api_key_changed and existing["api_key"]
            and _key_destination(m.provider_type, m.base_url) != _key_destination(existing["provider_type"], existing["base_url"])):
        raise HTTPException(
            400,
            "Адрес модели изменён на другой сервер - впишите API-ключ для него заново. "
            "Сохранённый ключ отправляется только на тот сервер, для которого его сохранили.",
        )
    db.update_model(model_id, m.name, m.provider_type, m.base_url, api_key, m.model, m.is_paid, m.priority, m.enabled, m.max_tokens)
    return _public_model(db.get_model(model_id))


@app.delete("/api/models/{model_id}")
def api_delete_model(model_id: int):
    db.delete_model(model_id)
    return {"ok": True}


@app.post("/api/models/{model_id}/check")
def api_check_model(model_id: int):
    row = db.get_model(model_id)
    if not row:
        raise HTTPException(404, "Модель не найдена")
    result = llm_providers.check_model_availability(row)
    db.set_model_check_result(model_id, result["status"], result["message"], raw_response=result.get("raw_response"))
    return _public_model(db.get_model(model_id))


@app.get("/api/models/{model_id}/log")
def api_model_log(model_id: int, limit: int = 30, offset: int = 0):
    if not db.get_model(model_id):
        raise HTTPException(404, "Модель не найдена")
    limit = max(1, min(limit, 100))
    events, total = db.list_model_events(model_id, limit=limit, offset=offset)
    return {"events": events, "total": total}


@app.get("/api/models/{model_id}/log/{event_id}/raw")
def api_download_model_event_raw(model_id: int, event_id: int):
    """Полный, ничем не обрезанный текст ответа провайдера для одной записи
    журнала - отдельным файлом, а не как часть короткого message в таблице."""
    event = db.get_model_event(model_id, event_id)
    if not event or event.get("raw_response") is None:
        raise HTTPException(404, "Для этой записи не сохранён полный ответ")
    filename = f"model_{model_id}_event_{event_id}_response.txt"
    return Response(
        content=event["raw_response"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/models/{model_id}/log")
def api_clear_model_log(model_id: int):
    if not db.get_model(model_id):
        raise HTTPException(404, "Модель не найдена")
    db.clear_model_events(model_id)
    return {"ok": True}


@app.get("/api/stats")
def api_stats():
    return db.get_stats_summary()


@app.get("/api/catalog/sources")
def api_catalog_sources():
    """Список источников каталога (провайдеров) с их адресами по умолчанию,
    ссылкой на получение ключа и тарифом - интерфейс строит выбор провайдера
    по этим данным, а не по своему отдельному списку."""
    return catalog.public_sources()


@app.post("/api/catalog")
async def api_catalog(payload: CatalogIn):
    """Каталог моделей провайдера для окна «Каталог» в форме новой модели -
    см. backend/catalog.py."""
    if payload.source not in catalog.SOURCES:
        raise HTTPException(400, f"Неизвестный источник каталога: {payload.source}")
    try:
        return await catalog.get_catalog(
            payload.source, payload.base_url, payload.api_key, payload.model_id, payload.refresh
        )
    except catalog.CatalogError as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/models/check-all")
def api_check_all_models():
    """Пингует только ВКЛЮЧЁННЫЕ модели параллельно и сохраняет результат по каждой.
    Выключенные модели не пингуются - незачем тратить их лимит/время впустую,
    раз они всё равно не участвуют в автопереключении при генерации."""
    rows = db.list_models(enabled_only=True)
    if not rows:
        return _public_models(db.list_models())

    def _check(row):
        # Сама проверка модели (check_model_availability) уже ловит свои
        # ошибки и всегда возвращает dict. А вот запись результата в SQLite -
        # отдельная операция: 8 потоков пишут в один файл параллельно, и даже
        # в WAL-режиме возможна "database is locked". Раньше это не было
        # поймано, и pool.map() поднимал исключение наружу при итерировании -
        # одна неудачная запись рушила ответ по ВСЕМ моделям (пользователь
        # вместо "7 из 8 проверено" получал голый HTTP 500). Теперь ошибка
        # записи результата для одной модели не мешает остальным.
        try:
            result = llm_providers.check_model_availability(row)
            db.set_model_check_result(row["id"], result["status"], result["message"], raw_response=result.get("raw_response"))
        except Exception as e:
            try:
                db.set_model_check_result(row["id"], "error", f"Непредвиденная ошибка при проверке: {e}")
            except Exception:
                pass  # даже запись ошибки не удалась (БД недоступна) - не валим остальные модели
        return row["id"]

    with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
        list(pool.map(_check, rows))

    return _public_models(db.list_models())


# ---------------------------- saved endpoints (адреса API) ----------------------------

@app.get("/api/base-urls")
def api_list_base_urls():
    return db.list_saved_endpoints()


@app.post("/api/base-urls")
def api_create_base_url(e: SavedEndpointIn):
    label = e.label.strip() or e.url.strip()
    url = e.url.strip()
    if not url:
        raise HTTPException(400, "Адрес не может быть пустым")
    db.create_saved_endpoint(label, url)
    return db.list_saved_endpoints()


@app.delete("/api/base-urls/{endpoint_id}")
def api_delete_base_url(endpoint_id: int):
    db.delete_saved_endpoint(endpoint_id)
    return {"ok": True}


# ---------------------------- подписанты (сохранённые ФИО) ----------------------------

@app.get("/api/signatories")
def api_list_signatories():
    return db.list_signatories()


@app.post("/api/signatories")
def api_create_signatory(s: SignatoryIn):
    full_name = s.full_name.strip()
    if not full_name:
        raise HTTPException(400, "ФИО не может быть пустым")
    db.create_signatory(full_name)
    return db.list_signatories()


@app.delete("/api/signatories/{signatory_id}")
def api_delete_signatory(signatory_id: int):
    db.delete_signatory(signatory_id)
    return {"ok": True}


# ---------------------------- шаблон документа ----------------------------
# Хранится в DATA_DIR (том, переживает пересборку Docker-образа), а не рядом
# с кодом backend/template.docx (тот запекается в образ при `docker build` -
# именно расхождение между "где редактируют файл" и "откуда его читает
# программа" обычно и есть причина "шаблон не обновляется").

@app.get("/api/template/status")
def api_template_status():
    custom_exists = os.path.exists(docx_builder.CUSTOM_TEMPLATE_PATH)
    active_path = docx_builder.current_template_path()
    stat = os.stat(active_path)
    return {
        "custom_active": custom_exists,
        "size_bytes": stat.st_size,
        # с часовым поясом - иначе браузер принимал время сервера (UTC) за местное
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


@app.post("/api/template")
async def api_upload_template(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(400, "Нужен файл с расширением .docx")

    MAX_TEMPLATE_BYTES = 20 * 1024 * 1024  # 20 МБ с запасом - готовый шаблон весит десятки КБ
    tmp_path = docx_builder.CUSTOM_TEMPLATE_PATH + ".uploading"
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    written = 0
    try:
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_TEMPLATE_BYTES:
                    raise HTTPException(400, f"Файл слишком большой (>{MAX_TEMPLATE_BYTES // (1024*1024)} МБ) - это не похоже на обычный .docx-шаблон")
                f.write(chunk)
    except HTTPException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    finally:
        await file.close()

    ok, message = docx_builder.validate_template(tmp_path)
    if not ok:
        os.remove(tmp_path)
        raise HTTPException(400, message)

    os.replace(tmp_path, docx_builder.CUSTOM_TEMPLATE_PATH)
    return {"ok": True, "message": message}


@app.delete("/api/template")
def api_reset_template():
    if os.path.exists(docx_builder.CUSTOM_TEMPLATE_PATH):
        os.remove(docx_builder.CUSTOM_TEMPLATE_PATH)
    return {"ok": True}


@app.get("/api/template/download")
def api_download_template():
    path = docx_builder.current_template_path()
    return FileResponse(
        path,
        filename="template.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------------------------- generation ----------------------------

@app.post("/api/generate")
async def api_generate(payload: GenerateIn):
    try:
        content = await llm_providers.generate_proposal_content(
            payload.topic, payload.extra_context, payload.preferred_model_id
        )
    except llm_providers.ProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Не удалось сгенерировать текст: {e}")
    return content


# request_id -> очередь событий для уже запущенной потоковой генерации.
# Генерация выполняется в ОТДЕЛЬНОЙ asyncio-задаче (см. _run_generation_task),
# а не прямо внутри обработчика запроса - если вкладка браузера закроется
# или страница перезагрузится посреди долгого перебора моделей, HTTP-соединение
# обрывается, но задача продолжает работать и всё равно сохранит итог в
# db.generation_runs, доступный потом через GET /api/generate/result/{request_id}.
# Раньше обрыв соединения означал потерю уже потраченного лимита нейросети
# без всякого результата.
_generation_queues: dict[str, "asyncio.Queue"] = {}
_background_tasks: set = set()  # сильные ссылки на фоновые задачи - событийный цикл хранит только слабые


async def _run_generation_task(topic: str, extra_context: str, preferred_model_id, request_id: str):
    queue = _generation_queues.get(request_id)
    try:
        db.start_generation_run(request_id)
    except Exception:
        pass

    final_status, final_data, final_message = "error", None, "Генерация не вернула результат."
    try:
        async for event in llm_providers.generate_proposal_content_stream(
            topic, extra_context, preferred_model_id, request_id
        ):
            if queue is not None:
                await queue.put(event)
            try:
                db.touch_generation_run(request_id)  # «жива» - см. api_generate_result
            except Exception:
                pass
            if event["stage"] == "done":
                final_status, final_data, final_message = "done", event["data"], None
            elif event["stage"] == "error":
                final_status, final_message = "error", event["message"]
            elif event["stage"] == "cancelled":
                final_status, final_message = "cancelled", event.get("message")
    except Exception as e:
        final_status, final_message = "error", f"Внутренняя ошибка генерации: {e}"
    finally:
        try:
            db.finish_generation_run(request_id, final_status, data=final_data, message=final_message)
        except Exception:
            pass
        if queue is not None:
            await queue.put(None)  # сигнал конца потока для читателя очереди


@app.post("/api/generate/stream")
async def api_generate_stream(payload: GenerateIn):
    """
    Тот же генератор текста, но потоково: каждая строка ответа - отдельный
    JSON-объект с полем "stage" (sending/escalating/rate_limited/received/
    forming/retry/done/error/cancelled). Используется прогресс-баром на
    вкладке «Новое предложение», чтобы показывать реальные этапы, а не
    имитацию. request_id (если передан) позволяет остановить перебор моделей
    через POST /api/generate/cancel и позже забрать результат через
    GET /api/generate/result/{request_id}, даже если соединение этого запроса
    уже не активно (см. комментарий у _generation_queues).
    """
    request_id = payload.request_id or str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _generation_queues[request_id] = queue

    task = asyncio.create_task(
        _run_generation_task(payload.topic, payload.extra_context, payload.preferred_model_id, request_id)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    async def event_stream():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            # убираем очередь из общего реестра, но саму фоновую задачу НЕ
            # отменяем - она должна доработать и сохранить результат, даже
            # если браузер уже отключился
            _generation_queues.pop(request_id, None)

    return StreamingResponse(event_stream(), media_type="application/x-ndjson", headers={"X-Request-Id": request_id})


@app.get("/api/generate/result/{request_id}")
def api_generate_result(request_id: str):
    """
    Позволяет забрать итог генерации по request_id, даже если исходное
    соединение /api/generate/stream было потеряно (перезагрузка страницы,
    закрытая вкладка, сетевой сбой) - сама генерация продолжается на сервере
    независимо от браузера (см. _run_generation_task) и сохраняет результат
    здесь по завершении.
    """
    run = db.get_generation_run(request_id)
    if not run:
        raise HTTPException(404, "Генерация с этим request_id не найдена (либо уже выполнена давно и запись подчищена, либо такого запроса не было)")
    status, message = run["status"], run["message"]
    if status == "running" and _run_looks_dead(run["updated_at"]):
        # Живая генерация отмечается на каждом шаге (db.touch_generation_run), а
        # между шагами проходит не больше одной попытки обращения к модели. Если
        # отметок нет дольше - фоновая задача оборвалась (чаще всего сервер
        # перезапустили посреди генерации). Раньше страница в этом случае
        # до 6 часов показывала «ещё выполняется» и опрашивала сервер.
        status = "error"
        message = "Генерация прервалась - вероятно, сервер был перезапущен во время неё. Запустите генерацию заново."
    return {"status": status, "data": run["data"], "message": message, "updated_at": run["updated_at"]}


def _run_looks_dead(updated_at: str) -> bool:
    limit = llm_providers.GENERATION_TIMEOUT + llm_providers.RATE_LIMIT_MAX_WAIT + 120
    try:
        age = datetime.utcnow().timestamp() - datetime.fromisoformat(updated_at).timestamp()
    except (TypeError, ValueError):
        return False
    return age > limit


@app.post("/api/generate/cancel")
def api_generate_cancel(payload: CancelGenerationIn):
    """
    Останавливает перебор моделей у уже запущенной потоковой генерации с этим
    request_id: уже отправленный конкретной модели запрос долетит до конца
    (простой HTTP-клиент не может прервать его на лету), но следующая попытка
    в цепочке автопереключения уже не начнётся - именно она обычно и жжёт
    больше всего лимита при "неудачном" перезапуске генерации.
    """
    db.request_generation_cancel(payload.request_id)
    return {"ok": True}


# ---------------------------- proposals ----------------------------

@app.get("/api/proposals")
def api_list_proposals():
    return db.list_proposals()


@app.post("/api/proposals")
def api_create_proposal(payload: ProposalIn):
    if len(payload.authors) != 6:
        raise HTTPException(400, "Нужно ровно 6 авторов")
    total_percent = sum(a.percent for a in payload.authors)
    if total_percent != 100:
        raise HTTPException(400, f"Сумма процентов должна быть равна 100 (сейчас {total_percent})")

    author_dicts = []
    for a in payload.authors:
        rec = db.get_author(a.author_id)
        if not rec:
            raise HTTPException(400, f"Автор с id={a.author_id} не найден")
        author_dicts.append(rec)

    required_sections = [f"section{i}" for i in range(1, 7)]
    missing = [s for s in required_sections if s not in payload.sections or not str(payload.sections[s]).strip()]
    if missing:
        raise HTTPException(400, f"Не заполнены разделы: {', '.join(missing)}")

    filename = f"rac_{uuid.uuid4().hex[:10]}.docx"
    output_path = os.path.join(OUTPUT_DIR, filename)

    try:
        docx_builder.build_docx(
            output_path=output_path,
            title=payload.title,
            sections=payload.sections,
            authors=author_dicts,
            percentages=[a.percent for a in payload.authors],
            vice_head=payload.vice_head or "Врио начальника",
            vice_fio=payload.vice_fio or "И.Иванов",
            proposal_date=docx_builder.today_local(),  # по Москве, а не по UTC контейнера
        )
    except Exception as e:
        raise HTTPException(500, f"Ошибка при сборке документа: {e}")

    proposal_id = db.create_proposal(
        title=payload.title,
        topic=payload.topic,
        sections=payload.sections,
        authors=author_dicts,
        vice_fio=payload.vice_fio,
        docx_filename=filename,
        provider=payload.model_used,
    )
    return {"id": proposal_id, "download_url": f"/api/proposals/{proposal_id}/download"}


@app.get("/api/proposals/{proposal_id}/download")
def api_download_proposal(proposal_id: int):
    rec = db.get_proposal(proposal_id)
    if not rec:
        raise HTTPException(404, "Не найдено")
    path = os.path.join(OUTPUT_DIR, rec["docx_filename"])
    if not os.path.exists(path):
        raise HTTPException(404, "Файл не найден на диске")
    safe_title = "".join(c for c in rec["title"][:60] if c.isalnum() or c in " _-").strip() or "rac_predlozhenie"
    return FileResponse(path, filename=f"{safe_title}.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.delete("/api/proposals/{proposal_id}")
def api_delete_proposal(proposal_id: int):
    rec = db.get_proposal(proposal_id)
    if not rec:
        raise HTTPException(404, "Не найдено")
    path = os.path.join(OUTPUT_DIR, rec["docx_filename"])
    if os.path.exists(path):
        os.remove(path)
    db.delete_proposal(proposal_id)
    return {"ok": True}


# ---------------------------- catch-all для несуществующих /api/* ----------------------------
# Без этого маршрута опечатка в пути (например, при будущих правках фронтенда)
# не долетала бы до FastAPI как понятный 404 - StaticFiles(html=True), смонтированный
# на "/", перехватывает ЛЮБОЙ непойманный путь и отдаёт index.html с кодом 200.
# Для /api/... это означает, что res.ok в app.js будет true, а res.json() упадёт
# с невнятным "Unexpected token '<'" вместо понятной ошибки "эндпоинт не найден".
@app.api_route("/api/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_catch_all(full_path: str, request: Request):
    # Если такой адрес есть, но вызван не тем методом (например, GET вместо
    # POST), Starlette отдаёт запрос сюда, а не отвечает 405 сам - этот маршрут
    # совпадает и по адресу, и по методу. Без этой проверки ответ был бы
    # вводящим в заблуждение "не существует" для вполне существующего адреса.
    path = request.url.path
    allowed = sorted({
        method
        for route in app.routes
        if isinstance(route, APIRoute) and route.name != "api_catch_all" and route.path_regex.match(path)
        for method in route.methods
    })
    if allowed:
        raise HTTPException(
            405,
            f"Метод {request.method} не поддерживается для {path} (поддерживается: {', '.join(allowed)})",
            headers={"Allow": ", ".join(allowed)},
        )
    raise HTTPException(404, f"Эндпоинт /api/{full_path} не существует")


# ---------------------------- frontend static ----------------------------

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
