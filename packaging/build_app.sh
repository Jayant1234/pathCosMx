#!/usr/bin/env bash
# Build the PathQC desktop app (macOS, Linux; Windows in Git Bash). See packaging/build_app.py.
set -euo pipefail
exec python "$(dirname "$0")/build_app.py" "$@"
