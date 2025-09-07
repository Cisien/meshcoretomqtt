# Basic container. Example usage: docker run -d --name mctomqtt-yagi -v ./config.ini:/opt/config.ini --device=/dev/ttyACM0  meshcoretomqtt:150326

FROM debian:bookworm

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /opt

RUN apt-get update && apt-get install -y python3-pip

RUN pip install pyserial paho-mqtt --break-system-packages

COPY ./*.py /opt
COPY ./config.ini /opt


CMD ["/usr/bin/python3", "/opt/mctomqtt.py"]
