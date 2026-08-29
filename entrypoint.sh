#!/bin/sh
set -e

# YouTube changes its anti-bot / signature scheme often enough that yt-dlp
# goes stale within weeks. Self-update on every container start so the
# bridge doesn't silently start failing with HTTP 403 after a while.
/app/yt-dlp -U || echo "[entrypoint] yt-dlp self-update failed, continuing with existing version"

exec node bundle.mjs
