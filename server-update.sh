#!/bin/sh
# Запускается на сервере из deploy.ps1 сразу после git pull.
# Образ пересобирается, только если поменялись зависимости или Dockerfile, иначе хватает рестарта.
set -e
cd "$(dirname "$0")"

if ! git diff --quiet ORIG_HEAD HEAD -- requirements.txt Dockerfile 2>/dev/null; then
  echo "Зависимости поменялись: пересобираю образ"
  docker build -q -t moneybot . >/dev/null
  docker rm -f moneybot >/dev/null
  docker run -d --name moneybot --restart always --memory 256m --cpus 0.5 \
    --log-opt max-size=10m --log-opt max-file=3 -v "$PWD":/app moneybot >/dev/null
else
  docker restart moneybot >/dev/null
fi

sleep 8
docker ps --filter name=moneybot --format "moneybot: {{.Status}}"
echo "Версия: $(git log --oneline -1)"
docker logs --since 15s moneybot 2>&1 | grep -E "Run polling|ERROR|Error" | tail -5
