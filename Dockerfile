FROM python:3.13-slim

LABEL org.opencontainers.image.title="promoter" \
      org.opencontainers.image.description="YouTube stats -> promo posts -> traffic to your site" \
      org.opencontainers.image.source="https://github.com/toblaron/promo"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/promoter.db \
    OUT_DIR=/data/out

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --chmod=0755 promoter.py /app/promoter.py

USER app
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=3m --retries=2 \
  CMD ["python", "/app/promoter.py", "health"]

ENTRYPOINT ["python", "/app/promoter.py"]
CMD ["watch"]
