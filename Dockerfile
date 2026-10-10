FROM python:alpine AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

FROM base AS runtime
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="torrent-name-normalizer" \
      org.opencontainers.image.description="Safely normalize qBittorrent display names after Arr verification" \
      org.opencontainers.image.version="2.3.0" \
      org.opencontainers.image.revision="$VCS_REF"

ENV NORMALIZER_CONFIG=/app/config/config.toml \
    HEALTH_PATH=/tmp/normalizer-health.json \
    HEALTH_MAX_AGE_SECONDS=180

WORKDIR /app
RUN addgroup -S normalizer && adduser -S -D -H -G normalizer normalizer
COPY --chown=normalizer:normalizer torrent_normalizer/ /app/torrent_normalizer/
USER normalizer

ENTRYPOINT ["python", "-m", "torrent_normalizer.main"]
