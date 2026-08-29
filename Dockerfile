FROM ghcr.io/tetrisblack/yt-sonos-bridge:main

# DIAL server port is hardcoded upstream (dial:{port:8099}) and can't be set
# via env var. Needed as a build arg so multiple rooms/speakers can each run
# their own bridge instance on the same host without port clashes — the
# audio-serving port doesn't need this, it already derives from
# SERVER_ENDPOINT. See "Multiple rooms" in the README.
ARG DIAL_PORT=8099
RUN sed -i "s/dial:{port:8099}/dial:{port:${DIAL_PORT}}/" /app/bundle.mjs

# yt-dlp needs a JS runtime to decipher YouTube's signature scheme.
# The base image ships Alpine with no JS runtime installed, which causes
# playback to fail with "Failed to extract signature decipher algorithm"
# and HTTP 403 errors from YouTube.
RUN apk add --no-cache deno

# Sonos playback-position sync defaults to a 3s interval, which shows up as
# the phone's UI (seek bar / now-playing state) lagging behind actual Sonos
# state by up to 3 seconds. This is a cheap local UPnP call, so tighten it.
# Fragile by nature (matches a minified string) — if this stops matching
# after an upstream bump, grep bundle.mjs for "syncSeek" and re-pin it.
RUN sed -i 's/this.syncSeek()},3e3)/this.syncSeek()},250)/' /app/bundle.mjs

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
