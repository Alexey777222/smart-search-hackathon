#!/usr/bin/env bash
# deploy/update_stand.sh — обновление публичного стенда с откатом.
#
# Что делает:
#   1. фиксирует текущую серверную версию (SHA и копия кода в backup);
#   2. подтягивает указанный коммит из GitHub и обновляет код стенда;
#   3. перезапускает службу и проверяет /api/health (SHA и готовность);
#   4. при любой неудаче — возвращает копию и снова проверяет.
#
# Запуск на VDS от root:
#   bash deploy/update_stand.sh [SHA]
# Без аргумента берётся HEAD ветки main из GitHub.
#
# Код синхронизируется вместе с .git: /api/health должен показывать тот же
# коммит, что опубликован в GitHub, — иначе стенд и репозиторий нельзя
# сверить.

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/smart-search-hackathon}
REPO_URL=${REPO_URL:-https://github.com/Alexey777222/smart-search-hackathon.git}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8000/api/health}
SERVICE=${SERVICE:-smart-search}
BACKUP_ROOT=${BACKUP_ROOT:-/opt/smart-search-backups}
WANT_SHA=${1:-}

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="$BACKUP_ROOT/$STAMP-before"
WORK_DIR=$(mktemp -d /tmp/smart-search-XXXXXX)
trap 'rm -rf "$WORK_DIR"' EXIT

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

rollback() {
  log "ОТКАТ к копии $BACKUP_DIR"
  rsync -a --delete "$BACKUP_DIR"/ "$APP_DIR"/
  systemctl restart "$SERVICE"
  sleep 4
  curl -fsS "$HEALTH_URL" >/dev/null && log "откат применён, служба отвечает" \
    || log "ВНИМАНИЕ: после отката служба не отвечает"
}

# 1. Текущая серверная версия и копия для отката.
mkdir -p "$BACKUP_ROOT"
CURRENT_SHA=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo "нет-git")
log "текущая серверная версия: $CURRENT_SHA"
rsync -a --exclude data --exclude .venv "$APP_DIR"/ "$BACKUP_DIR"/
log "копия кода: $BACKUP_DIR"

# 2. Публикуемый коммит.
git clone --quiet "$REPO_URL" "$WORK_DIR/repo"
if [ -n "$WANT_SHA" ]; then
  git -C "$WORK_DIR/repo" checkout --quiet "$WANT_SHA"
fi
WANT_SHA=$(git -C "$WORK_DIR/repo" rev-parse HEAD)
log "публикуемый коммит: $WANT_SHA"

# 3. Обновление кода (данные и окружение не трогаем).
rsync -a --exclude data --exclude .venv --exclude .env \
  "$WORK_DIR/repo"/ "$APP_DIR"/
systemctl restart "$SERVICE"
sleep 5

# 4. Проверка: служба отвечает, индекс на месте, SHA совпадает.
if ! curl -fsS "$HEALTH_URL" >/dev/null; then
  log "health не отвечает"
  rollback
  exit 1
fi
SERVED_SHA=$(curl -fsS "$HEALTH_URL" | python3 -c \
  'import json,sys; print(json.load(sys.stdin).get("commit") or "")')
OFFERS=$(curl -fsS "$HEALTH_URL" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["index"]["offers"])')
log "стенд отдаёт commit=$SERVED_SHA, предложений в индексе: $OFFERS"

if [ "$SERVED_SHA" != "${WANT_SHA:0:7}" ]; then
  log "несовпадение: ожидался ${WANT_SHA:0:7}"
  rollback
  exit 1
fi
if [ "${OFFERS:-0}" -lt 100000 ]; then
  log "индекс неполный ($OFFERS)"
  rollback
  exit 1
fi
log "готово: стенд на $SERVED_SHA"
