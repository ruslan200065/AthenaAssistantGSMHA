FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_CONFIG=/data/config.toml

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg v4l-utils alsa-utils git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Bump the add-on version in config.yaml -> this layer is rebuilt and pip
# installs the matching gateway commit instead of a cached wheel.
COPY config.yaml /tmp/addon-config.yaml

RUN pip install --no-cache-dir \
    "git+https://github.com/ruslan200065/AthenaAssistantGSMHA.git@main"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

VOLUME ["/data"]
EXPOSE 8099
CMD ["/run.sh"]
