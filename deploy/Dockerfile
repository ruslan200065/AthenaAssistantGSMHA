FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_CONFIG=/data/config.toml \
    STATE_DIR=/data \
    CAMERA_SNAPSHOT_DIR=/data/snapshots

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg v4l-utils alsa-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY sim7600_gateway ./sim7600_gateway
RUN pip install .

COPY config.example.toml ./config.example.toml

VOLUME ["/data"]
EXPOSE 8099

CMD ["python", "-m", "sim7600_gateway", "-c", "/data/config.toml"]
