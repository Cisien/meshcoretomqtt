# Basic container
# Example usage: 
#   docker run -d --name mctomqtt \
#     -v ./config/.env.local:/opt/.env.local \
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

WORKDIR /opt

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
COPY ./mctomqtt.py ./auth_token.py ./.env /opt/

# Note: .env.local should be mounted as a volume with your configuration
# The .env file contains defaults, .env.local contains your overrides
# Example: -v /path/to/.env.local:/opt/.env.local

CMD ["python3", "/opt/mctomqtt.py"]
