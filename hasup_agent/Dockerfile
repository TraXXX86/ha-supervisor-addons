# Home Assistant add-on image (multi-arch aarch64 / amd64).
#
# BUILD_FROM is provided by the Home Assistant add-on builder from build.yaml; the
# default keeps `docker build` usable by hand for the amd64 image.
#
# The build context must contain the agent package *and* the protocol package:
# `agent/scripts/sync-addon-repository.sh` assembles that self-contained directory in
# `addon-repository/hasup_agent/`, which is what Home Assistant builds on the box.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.24-2026.08.0
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/usr/src/app

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY hasup_protocol ./hasup_protocol
COPY hasup_agent ./hasup_agent
COPY rootfs /

LABEL \
    io.hass.name="HA Supervisor Agent" \
    io.hass.description="Fleet supervision agent for Home Assistant" \
    io.hass.type="addon" \
    io.hass.version="0.3.1"

# s6-overlay (provided by the base image) starts the service in
# /etc/s6-overlay/s6-rc.d/hasup-agent and forwards SIGTERM to it on add-on stop.
