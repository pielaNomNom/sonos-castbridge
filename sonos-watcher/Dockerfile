FROM python:3.13-alpine

# Needs the `docker` CLI + compose plugin to *recreate* yt-sonos-bridge when
# the Sonos speaker's IP changes (the socket is mounted at runtime, see
# docker-compose.yml). `docker restart` alone doesn't work here: Docker
# bakes env_file values into a container at creation time and never
# re-reads them on restart, so a plain restart would keep running against
# the stale IP forever — only recreating the container (`compose up`)
# picks up the new value written to data/sonos-target.env.
RUN apk add --no-cache docker-cli docker-cli-compose

ENV PYTHONUNBUFFERED=1

COPY watcher.py /app/watcher.py

ENTRYPOINT ["python3", "/app/watcher.py"]
