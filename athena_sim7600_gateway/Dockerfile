FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_CONFIG=/data/config.toml

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg v4l-utils alsa-utils git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Bump the add-on version in config.yaml -> this layer is rebuilt.
COPY config.yaml /tmp/addon-config.yaml

# Pin the gateway release instead of building an unpredictable `main`.
# The tag must match the gateway version (config.yaml/manifest.json).
ARG GATEWAY_REF=v1.2.6
RUN pip install --no-cache-dir \
    "git+https://github.com/ruslan200065/AthenaAssistantGSMHA.git@${GATEWAY_REF}"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

VOLUME ["/data"]
EXPOSE 8099
CMD ["/run.sh"]
