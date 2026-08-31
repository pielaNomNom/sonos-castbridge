FROM python:3.13-alpine

# Needs the `docker` CLI to restart yt-sonos-bridge when the Sonos speaker's
# IP changes (the socket is mounted at runtime, see docker-compose.yml).
RUN apk add --no-cache docker-cli

ENV PYTHONUNBUFFERED=1

COPY watcher.py /app/watcher.py

ENTRYPOINT ["python3", "/app/watcher.py"]
