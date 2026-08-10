FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY glyph/pi_intent_common.py /app/glyph/pi_intent_common.py
COPY glyph/watchdog/watchdog.py /app/glyph/watchdog.py
# Per-host key: build with --build-arg KEY_FILENAME=<name matching this
# host's PI_INTENT_KEY_FILE env var> (defaults to pi_intent.key for Pi2).
ARG KEY_FILENAME=pi_intent.key
COPY ${KEY_FILENAME} /app/${KEY_FILENAME}
WORKDIR /app/glyph
CMD ["python3", "watchdog.py"]
