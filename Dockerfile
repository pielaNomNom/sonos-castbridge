FROM ghcr.io/tetrisblack/yt-sonos-bridge:main

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
