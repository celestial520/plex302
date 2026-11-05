FROM ubuntu:24.04

ARG APT_MIRROR=mirrors.aliyun.com
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    CONFIG_DIR=/config \
    NGINX_PORT=32500 \
    WEBUI_PORT=999 \
    PATH="/opt/venv/bin:$PATH"

# 国内 NAS 构建加速；不需要可 --build-arg APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|archive.ubuntu.com|${APT_MIRROR}|g; s|security.ubuntu.com|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        nginx libnginx-mod-http-js supervisor \
        tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/plex302.conf.template /etc/nginx/templates/plex302.conf.template
COPY nginx/plex302.js /etc/nginx/conf.d/plex302.js
COPY scripts/entrypoint.sh /entrypoint.sh
COPY scripts/supervisord.conf /etc/supervisor/conf.d/plex302.conf
RUN chmod +x /entrypoint.sh \
    && mkdir -p /config/logs /var/log/nginx \
    && rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

VOLUME ["/config"]
EXPOSE 32500 999

ENTRYPOINT ["/entrypoint.sh"]
