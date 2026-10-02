FROM python:3.12-bookworm

LABEL org.opencontainers.image.title="WeChat Vault" \
      org.opencontainers.image.description="微信聊天记录长期归档与随时查看（微备份开源对标）" \
      org.opencontainers.image.version="1.1"

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    WV_STORE=/data/vault-store \
    WV_INBOX=/data/inbox \
    WV_LOGS=/data/logs \
    WV_COLD=/data/cold

WORKDIR /app

# 依赖（仅两个，用清华源加速；NAS 已缓存 python:3.12-bookworm 基础镜像）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple fastapi uvicorn

COPY parser/    /app/parser/
COPY archiver/  /app/archiver/
COPY viewer/    /app/viewer/

# 数据挂载点
RUN mkdir -p /data/vault-store /data/inbox /data/logs /data/cold

EXPOSE 8790

COPY deploy/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ============================================================
#  关键：容器以宿主 UID/GID 运行（默认 1000:1001 = 群晖/fnOS 的 AKI）
#  这样宿主上看到的数据文件属主就是 AKI，而不是 root，
#  SMB / 文件管理器 / 冷备目录都不会出现"权限不足"。
#  可在 compose 里用 user: "1000:1001" 覆盖。
# ============================================================
ARG APP_UID=1000
ARG APP_GID=1001
RUN groupadd -g ${APP_GID} app 2>/dev/null || true \
 && useradd -u ${APP_UID} -g ${APP_GID} -M -s /bin/sh app 2>/dev/null || true \
 && chown -R ${APP_UID}:${APP_GID} /app /data \
 # 显式放开读/执行位：COPY 会保留源文件权限，若源侧是 600/700，
 # 非 root 用户读不了静态资源（表现为首页 500 / PermissionError: index.html）
 && find /app -type d -exec chmod 755 {} + \
 && find /app -type f -exec chmod 644 {} + \
 && chmod +x /app/entrypoint.sh \
 && find /app/deploy -name '*.sh' -exec chmod +x {} + 2>/dev/null || true

USER ${APP_UID}:${APP_GID}

ENTRYPOINT ["/app/entrypoint.sh"]
