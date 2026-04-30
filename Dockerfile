FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    REMEDY_DRUPAL_RUNS_DB=/data/runs.sqlite3

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[prod,llm]" \
    && python -m playwright install --with-deps chromium \
    && useradd --create-home --shell /usr/sbin/nologin remedy \
    && mkdir -p /data \
    && chown -R remedy:remedy /app /data /ms-playwright

USER remedy

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=5)"

CMD ["sh", "-c", "remedy-drupal serve-http --config ${REMEDY_DRUPAL_CONFIG:-/app/config.yaml} --env ${REMEDY_DRUPAL_ENV:-/app/.env} --host 0.0.0.0 --port ${PORT:-8787} --runs-db ${REMEDY_DRUPAL_RUNS_DB:-/data/runs.sqlite3}"]
