#!/bin/sh
set -eu
exec "${PYTHON:-python3}" "$(dirname "$0")/verify_peer_cache.py" "$@"
