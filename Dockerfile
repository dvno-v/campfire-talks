FROM python:3.15.0rc1-alpine3.24@sha256:4b4340819382ffdbc0d87233b441daf617eec784e43458f8f5cb4d5e3b7d1838

LABEL org.opencontainers.image.title="Campfire" \
      org.opencontainers.image.description="Private self-hosted friend-group chat" \
      org.opencontainers.image.source="https://github.com/dvno-v/campfire-talks"

RUN addgroup -S -g 10001 campfire \
    && adduser -S -D -H -u 10001 -G campfire campfire \
    && mkdir -p /app /data /backups \
    && chown campfire:campfire /data /backups

WORKDIR /app
COPY --chown=root:root campfire/ campfire/
COPY --chown=root:root static/ static/
COPY --chown=root:root server.py README.md ROADMAP.md ./

RUN python -m compileall -q /app \
    && find /app -type d -exec chmod 0555 {} + \
    && find /app -type f -exec chmod 0444 {} +

USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CAMPFIRE_DB=/data/campfire.db \
    CAMPFIRE_UPLOAD_DIR=/data/uploads

EXPOSE 8000
VOLUME ["/data", "/backups"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3).read()"]

ENTRYPOINT ["python", "-m", "campfire"]
CMD ["serve"]
