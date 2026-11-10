#!/usr/bin/env bash
# ============================================================
#  Печатает nginx-конфиг сайта в stdout.
#
#    bash deploy/render-nginx.sh               # выделенный VPS: default_server, server_name _
#    bash deploy/render-nginx.sh titan.ex.com  # общий сервер: именованный server-блок
#
#  С доменом убирается default_server — на сервере с другими сайтами наш блок
#  не перехватывает чужие запросы и не конфликтует с чужим default_server.
# ============================================================
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nginx.conf"
DOMAIN="${1:-}"

if [[ -z "$DOMAIN" || "$DOMAIN" == "_" ]]; then
    cat "$SRC"
    exit 0
fi

if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    echo "render-nginx: недопустимое имя домена: $DOMAIN" >&2
    exit 2
fi

sed -e 's/ default_server;/;/' \
    -e "s/server_name _;/server_name ${DOMAIN};/" \
    "$SRC"
