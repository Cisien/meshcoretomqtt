# Basic container
# Example usage:
#   docker run -d --name mctomqtt \
#     -v ./config.toml:/etc/mctomqtt/config.toml \
#     --device=/dev/ttyACM0 \
#     meshcoretomqtt:latest

# Final stage
FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/mctomqtt/

# Install dependencies
# We add build-base and python3-dev to compile ed25519-orlp, then remove them
RUN apk add --no-cache --virtual .build-deps \
    build-base \
    python3-dev \
    && apk add --no-cache \
    curl \
    libstdc++ \
    libgcc \
    && pip3 install pyserial paho-mqtt ed25519-orlp --no-cache-dir \
    && apk del .build-deps

# Copy application files
COPY ./mctomqtt.py ./auth_token.py ./config_loader.py /opt/mctomqtt/
COPY ./bridge/ /opt/mctomqtt/bridge/

# Note: Mount your config directory as a volume:
#   -v /path/to/mctomqtt-config:/etc/mctomqtt:ro

RUN adduser -D mctomqtt
RUN addgroup mctomqtt dialout

USER mctomqtt
CMD ["python3", "/opt/mctomqtt/mctomqtt.py"]
