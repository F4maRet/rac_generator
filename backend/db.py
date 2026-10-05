import sqlite3
import json
import os
import time
from datetime import datetime
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "..", "data")
DB_PATH = os.path.abspath(os.path.join(DATA_DIR, "app.db"))

# Сколько секунд соединение ждёт, пока другой процесс (другой gunicorn-воркер)
# освободит запись, прежде чем выдать "database is locked". Стандартные 5 сек
# бывает мало при параллельной записи (например, «Пинг всех» пишет результат
# сразу по 8 моделям).
SQLITE_BUSY_TIMEOUT = 15


def _ensure_column(conn, table: str, column: str, coldef: str):
    """Безопасная миграция "на лету": добавляет колонку, если её ещё нет
    (для тех, кто уже пользуется программой и обновил код - у них в БД
    ещё нет новых полей вроде max_tokens).

    При нескольких gunicorn-воркерах оба могут одновременно увидеть, что
    колонки нет, и оба выполнить ALTER TABLE - второй получит "duplicate
    column name". Это не ошибка: колонку уже добавил соседний воркер."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def _is_lock_error(e: Exception) -> bool:
    text = str(e).lower()
    return "locked" in text or "busy" in text


def init_db():
    """Создаёт таблицы/миграции и начальные данные.

    Вызывается при импорте main.py в КАЖДОМ gunicorn-воркере одновременно. На
    совершенно новой базе это гонка: перевод базы в режим WAL требует
    монопольной блокировки, и проигравший воркер раньше сразу падал с
    "database is locked" - gunicorn видел упавший воркер и останавливал весь
    сервер (Docker затем перезапускал контейнер, и со второй попытки всё
    поднималось, но первый запуск выглядел как падение). Все шаги здесь
    идемпотентны, поэтому при блокировке просто ждём и повторяем."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    last_error = None
    for attempt in range(20):
        try:
            _init_db_once()
            return
        except sqlite3.OperationalError as e:
            if not _is_lock_error(e):
                raise
            last_error = e
            time.sleep(0.1 * (attempt + 1))  # суммарно до ~20 сек ожидания
    raise last_error


def _init_db_once():
    with get_conn() as conn:
        # WAL: читатели не блокируют писателя и наоборот - при нескольких
        # gunicorn-воркерах, стучащихся в один SQLite-файл, это заметно снижает
        # число "database is locked" и ускоряет параллельные запросы
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS authors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rank TEXT NOT NULL DEFAULT '',
                full_name TEXT NOT NULL,
                position TEXT NOT NULL DEFAULT '',
                workplace TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT '',
                sections_json TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                nic_head TEXT NOT NULL DEFAULT '',
                docx_filename TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS llm_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                is_paid INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 100,
                max_tokens INTEGER NOT NULL DEFAULT 4000,
                last_check_status TEXT,
                last_check_message TEXT,
                last_check_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS saved_endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS model_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                raw_response TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_events_model ON model_events(model_id, id DESC);

            CREATE TABLE IF NOT EXISTS signatories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_runs (
                request_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                data_json TEXT,
                message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _ensure_column(conn, "llm_models", "max_tokens", "INTEGER NOT NULL DEFAULT 4000")
        _ensure_column(conn, "model_events", "raw_response", "TEXT")
    _seed_default_models()
    _seed_default_endpoints()


def _seed_default_models():
    """При первом запуске добавляет заготовки популярных бесплатных провайдеров
    (выключены по умолчанию - до того, как пользователь впишет свой API-ключ).

    BEGIN IMMEDIATE берёт блокировку записи ДО проверки "таблица пуста?" -
    иначе два gunicorn-воркера на новой базе оба видели пустую таблицу и оба
    добавляли заготовки, получались дубли. Теперь второй ждёт первого и
    видит, что заготовки уже есть."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute("SELECT COUNT(*) AS c FROM llm_models").fetchone()["c"]
        if count > 0:
            return
        # (имя, тип, base_url, api_key, модель, платная?, приоритет, max_tokens)
        # max_tokens подобран консервативно-щедро под 6 разделов рацпредложения;
        # если конкретный тариф/модель ограничивает ответ меньшим числом -
        # провайдер вернёт понятную ошибку, и значение можно поправить точечно
        # прямо в карточке модели, не трогая остальные.
        defaults = [
            ("OpenRouter (DeepSeek, бесплатно)", "openai_compat", "https://openrouter.ai/api/v1", "", "deepseek/deepseek-chat-v3.1:free", 0, 10, 4000),
            ("Groq (Llama 3.3, бесплатно)", "openai_compat", "https://api.groq.com/openai/v1", "", "llama-3.3-70b-versatile", 0, 20, 4000),
            ("Google Gemini (бесплатный лимит)", "gemini", "", "", "gemini-2.0-flash", 0, 30, 6000),
            ("DeepSeek (напрямую, платно)", "openai_compat", "https://api.deepseek.com", "", "deepseek-chat", 1, 35, 6000),
            ("Ollama (локально)", "ollama", "http://localhost:11434", "", "llama3.1", 0, 40, 6000),
            ("LM Studio (локально)", "openai_compat", "http://localhost:1234/v1", "", "", 0, 50, 6000),
        ]
        now = datetime.utcnow().isoformat()
        for name, ptype, base_url, api_key, model, is_paid, priority, max_tokens in defaults:
            conn.execute(
                """INSERT INTO llm_models
                   (name, provider_type, base_url, api_key, model, is_paid, enabled, priority, max_tokens, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (name, ptype, base_url, api_key, model, is_paid, priority, max_tokens, now),
            )


def _seed_default_endpoints():
    """Заготовки часто используемых адресов API, чтобы не вводить их вручную каждый раз.
    BEGIN IMMEDIATE - по той же причине, что и в _seed_default_models."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute("SELECT COUNT(*) AS c FROM saved_endpoints").fetchone()["c"]
        if count > 0:
            return
        defaults = [
            ("OpenRouter", "https://openrouter.ai/api/v1"),
            ("Groq", "https://api.groq.com/openai/v1"),
            ("Together AI", "https://api.together.xyz/v1"),
            ("DeepSeek (напрямую)", "https://api.deepseek.com"),
            ("Ollama (локально)", "http://localhost:11434"),
            ("LM Studio (локально)", "http://localhost:1234/v1"),
        ]
        now = datetime.utcnow().isoformat()
        for label, url in defaults:
            conn.execute(
                "INSERT OR IGNORE INTO saved_endpoints (label, url, created_at) VALUES (?, ?, ?)",
                (label, url, now),
            )


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- authors ----------

def list_authors(query: str = ""):
    with get_conn() as conn:
        if query:
            like = f"%{query}%"
            rows = conn.execute(
                "SELECT * FROM authors WHERE full_name LIKE ? OR rank LIKE ? OR position LIKE ? OR workplace LIKE ? ORDER BY full_name",
                (like, like, like, like),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM authors ORDER BY full_name").fetchall()
        return [dict(r) for r in rows]


def get_author(author_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM authors WHERE id = ?", (author_id,)).fetchone()
        return dict(row) if row else None


def create_author(rank: str, full_name: str, position: str, workplace: str):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO authors (rank, full_name, position, workplace, created_at) VALUES (?, ?, ?, ?, ?)",
            (rank, full_name, position, workplace, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def get_author_by_name(full_name: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM authors WHERE full_name=?", (full_name,)).fetchone()
        return dict(row) if row else None


def upsert_author_by_name(rank: str, full_name: str, position: str, workplace: str):
    """Импорт из файла: если автор с таким ФИО уже есть - обновляет остальные
    поля, а не создаёт дубликат ("перенос между серверами" обычно и означает
    "слить с уже существующей базой", а не затереть её). full_name - здесь
    естественный "ключ" человека (по нему же строится автодополнение)."""
    existing = get_author_by_name(full_name)
    if existing:
        update_author(existing["id"], rank, full_name, position, workplace)
        return existing["id"], False
    new_id = create_author(rank, full_name, position, workplace)
    return new_id, True


def update_author(author_id: int, rank: str, full_name: str, position: str, workplace: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE authors SET rank=?, full_name=?, position=?, workplace=? WHERE id=?",
            (rank, full_name, position, workplace, author_id),
        )


def delete_author(author_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM authors WHERE id=?", (author_id,))


# ---------- proposals ----------

def create_proposal(title, topic, sections: dict, authors: list, vice_fio: str, docx_filename: str, provider: str):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO proposals (title, topic, sections_json, authors_json, nic_head, docx_filename, provider, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                topic,
                json.dumps(sections, ensure_ascii=False),
                json.dumps(authors, ensure_ascii=False),
                vice_fio,  # столбец в БД исторически называется nic_head (см. миграцию VICE_HEAD/VICE_FIO)
                docx_filename,
                provider,
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def list_proposals():
    """Лёгкий список для вкладки «История»: без sections_json/authors_json -
    их всё равно не парсить и не показывать в списке, а на каждую запись это
    была бы бесполезная работа (JSON += куда больше самих строк таблицы)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, topic, nic_head, docx_filename, provider, created_at FROM proposals ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_proposal(proposal_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["sections"] = json.loads(d.pop("sections_json"))
        d["authors"] = json.loads(d.pop("authors_json"))
        return d


def delete_proposal(proposal_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM proposals WHERE id=?", (proposal_id,))


# ---------- settings (key/value, used for LLM provider configuration) ----------

def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_all_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# ---------- отмена генерации ----------
# Флаг хранится в общей таблице settings (файл SQLite, а не память процесса) -
# это важно, если приложение запущено с несколькими gunicorn-воркерами:
# запрос на отмену может прийти в ДРУГОЙ процесс, чем тот, что выполняет саму
# генерацию, поэтому обычный dict/set в памяти тут не сработал бы надёжно.

def request_generation_cancel(request_id: str):
    set_setting(f"cancel:{request_id}", str(int(datetime.utcnow().timestamp())))
    _prune_stale_cancel_flags()


def _prune_stale_cancel_flags(max_age_seconds: int = 3600):
    """Если запрос на отмену пришёл уже ПОСЛЕ того, как генерация сама
    завершилась (редкая гонка), clear_generation_cancel для него никогда не
    вызовется - флаг остался бы в settings мусором навсегда. Подчищаем такие
    "осиротевшие" флаги (старше часа) при каждой новой отмене - дёшево и
    не даёт таблице настроек тихо расти на пустом месте при долгой работе."""
    cutoff = int(datetime.utcnow().timestamp()) - max_age_seconds
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'cancel:%'").fetchall()
        for r in rows:
            try:
                is_stale = int(r["value"]) < cutoff
            except (ValueError, TypeError):
                is_stale = True  # старый формат значения ("1") - тоже подчищаем
            if is_stale:
                conn.execute("DELETE FROM settings WHERE key=?", (r["key"],))


def is_generation_cancelled(request_id: str) -> bool:
    if not request_id:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM settings WHERE key=?", (f"cancel:{request_id}",)).fetchone()
        return row is not None


def clear_generation_cancel(request_id: str):
    if not request_id:
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (f"cancel:{request_id}",))


# ---------- llm_models (список нейросетей: свои бесплатные/платные модели) ----------

def list_models(enabled_only: bool = False):
    with get_conn() as conn:
        sql = "SELECT * FROM llm_models"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY priority ASC, id ASC"
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def get_model(model_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM llm_models WHERE id=?", (model_id,)).fetchone()
        return dict(row) if row else None


def create_model(name, provider_type, base_url, api_key, model, is_paid, priority, max_tokens=4000):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO llm_models
               (name, provider_type, base_url, api_key, model, is_paid, enabled, priority, max_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (name, provider_type, base_url, api_key, model, int(is_paid), int(priority), int(max_tokens), datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def update_model(model_id, name, provider_type, base_url, api_key, model, is_paid, priority, enabled, max_tokens=4000):
    with get_conn() as conn:
        conn.execute(
            """UPDATE llm_models
               SET name=?, provider_type=?, base_url=?, api_key=?, model=?, is_paid=?, priority=?, enabled=?, max_tokens=?
               WHERE id=?""",
            (name, provider_type, base_url, api_key, model, int(is_paid), int(priority), int(enabled), int(max_tokens), model_id),
        )


def update_model_max_tokens(model_id: int, max_tokens: int):
    """Точечное обновление одного поля - используется автоматическим
    "самообучением" лимита токенов после успешной генерации с увеличенным
    значением (см. llm_providers.generate_proposal_content_stream), чтобы не
    пересобирать весь набор полей модели ради одного числа."""
    with get_conn() as conn:
        conn.execute("UPDATE llm_models SET max_tokens=? WHERE id=?", (int(max_tokens), model_id))


def delete_model(model_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM llm_models WHERE id=?", (model_id,))
        conn.execute("DELETE FROM model_events WHERE model_id=?", (model_id,))


RAW_RESPONSE_MAX_CHARS = 1_000_000  # ~1 МБ текста - с запасом хватает на любой вменяемый ответ API


def set_model_check_result(model_id: int, status: str, message: str, event_type: str = "check", raw_response: str = None):
    """Обновляет "текущий" статус модели (для бейджей в таблице) И добавляет
    запись в журнал событий (для модалки с историей) - одним вызовом, чтобы
    эти два места не могли разойтись между собой. raw_response, если есть -
    ПОЛНЫЙ, ничем не обрезанный текст ответа провайдера (для скачивания из
    журнала); в бейджах/списке используется только короткое message.

    Если base_url настроен неправильно (например, указывает на обычный сайт,
    а не на API) и ответ оказался не JSON-ошибкой API, а, скажем, многомегабайтной
    HTML-страницей - без ограничения такие ответы накапливались бы в БД без
    всякого разумного предела (до 200 записей на модель, каждая - потенциально
    гигантский текст). Обрезаем с явной пометкой - для диагностики обрезанного
    ответа обычно достаточно и начала текста."""
    if raw_response is not None and len(raw_response) > RAW_RESPONSE_MAX_CHARS:
        raw_response = (
            raw_response[:RAW_RESPONSE_MAX_CHARS]
            + f"\n\n...[обрезано, исходный ответ длиннее {RAW_RESPONSE_MAX_CHARS} символов]..."
        )
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE llm_models SET last_check_status=?, last_check_message=?, last_check_at=? WHERE id=?",
            (status, message, now, model_id),
        )
        conn.execute(
            "INSERT INTO model_events (model_id, event_type, status, message, raw_response, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (model_id, event_type, status, message, raw_response, now),
        )
        # не даём журналу расти бесконечно - оставляем последние 200 записей на модель
        conn.execute(
            """DELETE FROM model_events WHERE model_id=? AND id NOT IN (
                   SELECT id FROM model_events WHERE model_id=? ORDER BY id DESC LIMIT 200
               )""",
            (model_id, model_id),
        )


def list_model_events(model_id: int, limit: int = 30, offset: int = 0):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, model_id, event_type, status, message, created_at,
                      (raw_response IS NOT NULL) AS has_raw_response
               FROM model_events WHERE model_id=? ORDER BY id DESC LIMIT ? OFFSET ?""",
            (model_id, limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM model_events WHERE model_id=?", (model_id,)).fetchone()["c"]
        return [dict(r) for r in rows], total


def get_model_event(model_id: int, event_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM model_events WHERE model_id=? AND id=?", (model_id, event_id)
        ).fetchone()
        return dict(row) if row else None


def clear_model_events(model_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM model_events WHERE model_id=?", (model_id,))


# ---------- saved_endpoints (адреса API, чтобы не вводить их каждый раз заново) ----------

def list_saved_endpoints():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM saved_endpoints ORDER BY label").fetchall()
        return [dict(r) for r in rows]


def create_saved_endpoint(label: str, url: str):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO saved_endpoints (label, url, created_at) VALUES (?, ?, ?)",
            (label, url, datetime.utcnow().isoformat()),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM saved_endpoints WHERE url=?", (url,)).fetchone()
        return row["id"] if row else None


def delete_saved_endpoint(endpoint_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM saved_endpoints WHERE id=?", (endpoint_id,))


# ---------- signatories (сохранённые ФИО подписантов, формат «И.Фамилия») ----------

def list_signatories():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM signatories ORDER BY full_name").fetchall()
        return [dict(r) for r in rows]


def create_signatory(full_name: str):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO signatories (full_name, created_at) VALUES (?, ?)",
            (full_name, datetime.utcnow().isoformat()),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM signatories WHERE full_name=?", (full_name,)).fetchone()
        return row["id"] if row else None


def delete_signatory(signatory_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM signatories WHERE id=?", (signatory_id,))


# ---------- generation_runs (результат потоковой генерации по request_id) ----------
# Генерация теперь выполняется в фоновой asyncio-задаче, не привязанной к
# жизни HTTP-запроса (см. main.py) - поэтому даже если вкладка браузера
# закрылась/обновилась посреди долгого перебора моделей, сама генерация не
# прерывается, а её итог (успех/ошибка/отмена) сохраняется здесь и может быть
# запрошен повторно по request_id, а не теряется безвозвратно вместе с
# уже потраченным лимитом нейросети.

def start_generation_run(request_id: str):
    if not request_id:
        return
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO generation_runs (request_id, status, data_json, message, created_at, updated_at)
               VALUES (?, 'running', NULL, NULL, ?, ?)
               ON CONFLICT(request_id) DO UPDATE SET status='running', data_json=NULL, message=NULL, updated_at=excluded.updated_at""",
            (request_id, now, now),
        )
    _prune_stale_generation_runs()


def finish_generation_run(request_id: str, status: str, data: dict = None, message: str = None):
    if not request_id:
        return
    with get_conn() as conn:
        conn.execute(
            """UPDATE generation_runs SET status=?, data_json=?, message=?, updated_at=?
               WHERE request_id=?""",
            (
                status,
                json.dumps(data, ensure_ascii=False) if data is not None else None,
                message,
                datetime.utcnow().isoformat(),
                request_id,
            ),
        )


def touch_generation_run(request_id: str):
    """Отметка «генерация жива»: обновляется на каждом шаге генерации. Если
    отметок давно нет, а статус всё ещё running - генерация оборвалась
    (например, сервер перезапустили), см. main.api_generate_result."""
    if not request_id:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE generation_runs SET updated_at=? WHERE request_id=? AND status='running'",
            (datetime.utcnow().isoformat(), request_id),
        )


def get_generation_run(request_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM generation_runs WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["data"] = json.loads(d.pop("data_json")) if d.get("data_json") else None
        return d


# ---------- сводная статистика по моделям (идея аудита №5) ----------

def get_stats_summary():
    """Агрегат по журналу model_events на уровень каждой модели - сколько
    генераций/проверок прошло успешно и с ошибкой, какой процент успеха.
    Раньше эти цифры можно было увидеть только вручную, листая журнал одной
    модели за раз в модалке; здесь - сразу сводка по всем, для выбора
    приоритетов и настройки max_tokens без ручного копания в журналах."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.name, m.provider_type, m.enabled, m.priority, m.max_tokens,
                   m.last_check_status,
                   SUM(CASE WHEN e.event_type='generation' AND e.status='ok' THEN 1 ELSE 0 END) AS gen_ok,
                   SUM(CASE WHEN e.event_type='generation' AND e.status='error' THEN 1 ELSE 0 END) AS gen_error,
                   SUM(CASE WHEN e.event_type='check' AND e.status='ok' THEN 1 ELSE 0 END) AS check_ok,
                   SUM(CASE WHEN e.event_type='check' AND e.status='error' THEN 1 ELSE 0 END) AS check_error
            FROM llm_models m
            LEFT JOIN model_events e ON e.model_id = m.id
            GROUP BY m.id
            ORDER BY m.priority ASC, m.id ASC
            """
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            gen_ok = d.pop("gen_ok") or 0
            gen_error = d.pop("gen_error") or 0
            gen_total = gen_ok + gen_error
            d["gen_ok"] = gen_ok
            d["gen_error"] = gen_error
            d["gen_total"] = gen_total
            d["gen_success_rate"] = round(gen_ok / gen_total * 100, 1) if gen_total else None
            d["check_ok"] = d.pop("check_ok") or 0
            d["check_error"] = d.pop("check_error") or 0
            result.append(d)
        return result


def _prune_stale_generation_runs(max_age_seconds: int = 6 * 3600):
    """Как и с флагами отмены - подчищаем старые записи (старше 6 часов),
    чтобы таблица не росла бесконечно, если какие-то request_id никогда не
    запрашиваются повторно (обычный случай - страница просто получила ответ
    по месту, без перезагрузки). Сравнение делаем в Python (а не через SQL
    datetime()), чтобы не зависеть от точного совпадения текстового формата
    ISO-строки (с "T" и микросекундами) с тем, что возвращает datetime()
    в SQLite (с пробелом, без микросекунд) - разные форматы сравнивались бы
    лексикографически неверно."""
    cutoff = datetime.utcnow().timestamp() - max_age_seconds
    with get_conn() as conn:
        rows = conn.execute("SELECT request_id, updated_at FROM generation_runs").fetchall()
        for r in rows:
            try:
                is_stale = datetime.fromisoformat(r["updated_at"]).timestamp() < cutoff
            except (ValueError, TypeError):
                is_stale = True
            if is_stale:
                conn.execute("DELETE FROM generation_runs WHERE request_id=?", (r["request_id"],))
