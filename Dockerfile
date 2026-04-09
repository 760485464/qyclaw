FROM python:3.12-slim

WORKDIR /app

LABEL org.opencontainers.image.title="qyclaw" \
      org.opencontainers.image.version="v0.1.0" \
      org.opencontainers.image.description="Qyclaw full-stack image with backend and frontend in one container"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QYCLAW_CONFIG=/app/config.yaml

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY requirements-models.txt /app/requirements-models.txt

RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/requirements-models.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY images /app/images
COPY skills-builtin /app/skills-builtin
COPY scripts /app/scripts
COPY config.yaml /app/config.yaml
COPY config-docker.yaml /app/config-docker.yaml
COPY docker/start.sh /app/docker/start.sh

RUN mkdir -p /app/uploads /app/skills /app/workspaces /app/certs /app/data \
    && chmod +x /app/docker/start.sh

EXPOSE 8000 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/docker/start.sh"]
