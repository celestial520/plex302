#!/bin/sh
set -e

export NGINX_PORT="${NGINX_PORT:-32500}"
export WEBUI_PORT="${WEBUI_PORT:-999}"
export CONFIG_DIR="${CONFIG_DIR:-/config}"
export TZ="${TZ:-Asia/Shanghai}"

mkdir -p "$CONFIG_DIR/logs" /var/log/nginx /run

echo "[entrypoint] Plex302 启动：Plex 代理端口 ${NGINX_PORT}，WebUI 端口 ${WEBUI_PORT}"

sed "s/__PORT__/${NGINX_PORT}/g" /etc/nginx/templates/plex302.conf.template > /etc/nginx/conf.d/plex302.conf

# 配置错误时不要静默失败，日志里能直接看到原因
nginx -t

exec supervisord -c /etc/supervisor/conf.d/plex302.conf
