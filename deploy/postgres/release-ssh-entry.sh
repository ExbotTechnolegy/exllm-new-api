#!/usr/bin/env bash
set -euo pipefail

request=$SSH_ORIGINAL_COMMAND
receiver=/usr/local/libexec/exai-release/receive-release.py
if [[ "$request" == rollback ]]; then
    exec sudo -n -- "$receiver" rollback
fi
if [[ "$request" =~ ^deploy\ ([a-f0-9]{40})$ ]]; then
    exec sudo -n -- "$receiver" deploy "${BASH_REMATCH[1]}"
fi
echo "Unsupported EXAI release command" >&2
exit 1
