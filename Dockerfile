# ---- build stage: install Python deps into a venv (keeps the final image slim) ----
FROM python:3.12-slim AS builder

# Индекс пакетов можно переопределить (например, на зеркало), если pypi.org
# недоступен напрямую из сети, где идёт сборка - см. "Проблемы со сборкой
# образа" в README. Пример: --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir \
    --index-url ${PIP_INDEX_URL} \
    --retries 5 \
    --timeout 30 \
    -r requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim

# непривилегированный пользователь - не запускаем сервис от root
RUN groupadd -r app && useradd -r -g app app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    WEB_CONCURRENCY=2

WORKDIR /app
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# том с данными: SQLite-база и сгенерированные .docx переживают пересборку/рестарт контейнера
RUN mkdir -p /app/data/output && chown -R app:app /app
VOLUME ["/app/data"]

USER app
WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

# gunicorn + uvicorn-воркеры: несколько процессов на одном контейнере
# (не увеличивайте WEB_CONCURRENCY бездумно - SQLite не любит много параллельных записей,
# см. раздел "Масштабирование" в README)
CMD ["sh", "-c", "gunicorn main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY} -b 0.0.0.0:8000 --access-logfile - --error-logfile -"]
