#!/usr/bin/env bash
# Автообновление бота в Docker. Cron запускает каждый час (deploy/docker/arena.cron):
# скачать :latest из GitHub Container Registry → если образ новый (или контейнер не запущен) —
# пересоздать контейнер. База и bot.log — в $D/data, ключи — в $D/secrets (только чтение).
set -uo pipefail
IMAGE=ghcr.io/johnsmith-sg/model-arena-bot:latest
NAME=model-arena-bot
D=/opt/model-arena-bot
LOG=$D/docker/update.log
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

if ! docker pull -q "$IMAGE" >/dev/null 2>&1; then
  say "ошибка: не удалось скачать $IMAGE"; exit 1
fi
new=$(docker image inspect -f '{{.Id}}' "$IMAGE")
old=$(docker inspect -f '{{.Image}}' "$NAME" 2>/dev/null || echo none)
running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || echo false)

if [ "$new" = "$old" ] && [ "$running" = true ]; then
  say "без изменений: ${new:7:12}"; exit 0
fi

docker rm -f "$NAME" >/dev/null 2>&1
# --user: тот же системный пользователь, что владеет data/ и secrets/ на сервере
docker run -d --name "$NAME" --restart unless-stopped \
  --user "$(id -u arenabot):$(id -g arenabot)" \
  --memory 300m \
  --log-opt max-size=5m --log-opt max-file=3 \
  -v "$D/data:/data" -v "$D/secrets:/run/secrets:ro" \
  --env-file "$D/docker/arena.env" \
  "$IMAGE" >/dev/null
sleep 5
say "обновлено: ${old:7:12} → ${new:7:12}, контейнер $(docker inspect -f '{{.State.Status}}' "$NAME")"
docker image prune -f >/dev/null 2>&1
