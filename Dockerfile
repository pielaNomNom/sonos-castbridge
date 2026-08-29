FROM ghcr.io/tetrisblack/yt-sonos-bridge:main

# yt-dlp needs a JS runtime to decipher YouTube's signature scheme.
# The base image ships Alpine with no JS runtime installed, which causes
# playback to fail with "Failed to extract signature decipher algorithm"
# and HTTP 403 errors from YouTube.
RUN apk add --no-cache deno

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
