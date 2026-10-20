# Образ бота «Арена моделей». Собирается GitHub Actions и лежит в GitHub Container Registry:
# ghcr.io/johnsmith-sg/model-arena-bot:latest. Секретов в образе нет — они монтируются на сервере.
FROM python:3.12-slim

WORKDIR /app
COPY deploy/requirements-server.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py providers.py storage.py catalog.py ./
COPY certs/ certs/

# Бот работает не от root; база и bot.log — в томе /data, чтобы переживать обновления
RUN useradd --system --uid 10001 arena && mkdir /data && chown arena /data
USER arena
# Версия = коммит, из которого собран образ (передаёт GitHub Actions) — видна в журнале при старте
ARG GIT_SHA=local
ENV ARENA_DATA_DIR=/data \
    LOG_TO_STDOUT=1 \
    PYTHONUNBUFFERED=1 \
    ARENA_VERSION=$GIT_SHA
VOLUME /data

CMD ["python", "bot.py"]
