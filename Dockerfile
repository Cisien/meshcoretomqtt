# Basic container
# Example usage:
#   docker run -d --name mctomqtt \
#     -v ./config.toml:/etc/mctomqtt/config.toml \
#     --device=/dev/ttyACM0 \
#     meshcoretomqtt:latest


# Builder stage for Node.js and meshcore-decoder
FROM alpine:latest AS builder

WORKDIR /build

# Install Node.js and npm
RUN apk add --no-cache nodejs npm

# Install meshcore-decoder
RUN npm install -g @michaelhart/meshcore-decoder

# Final stage
FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/mctomqtt/

# Install dependencies including Node.js runtime
RUN apk add --no-cache \
    curl \
    libstdc++ \
    libgcc \
    nodejs \
    && pip3 install pyserial paho-mqtt --no-cache-dir

# Copy the entire Node structure from builder to ensure symlinks and paths remain valid
COPY --from=builder /usr/local /usr/local
# Copy application files
COPY ./mctomqtt.py ./auth_token.py ./config_loader.py /opt/mctomqtt/
COPY ./bridge/ /opt/mctomqtt/bridge/

# Note: Mount your config as a volume:
#   -v /path/to/config.toml:/etc/mctomqtt/config.toml
# Or mount a drop-in override:
#   -v /path/to/00-user.toml:/etc/mctomqtt/config.d/00-user.toml
USER mctomqtt
CMD ["python3", "/opt/mctomqtt/mctomqtt.py"]
