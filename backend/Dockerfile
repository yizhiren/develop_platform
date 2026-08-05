FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY backend/pyproject.toml ./pyproject.toml
RUN pip install --upgrade pip && pip install ".[test]"
COPY backend/app ./app
COPY backend/tests ./tests
COPY backend/agent-entrypoint.sh ./agent-entrypoint.sh

RUN chmod 755 /srv/agent-entrypoint.sh \
    && useradd --create-home --uid 10001 forgeflow \
    && chown -R forgeflow:forgeflow /srv
USER forgeflow

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
