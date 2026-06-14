#!/usr/bin/env bash
set -euo pipefail

export UPDATE_DST=1
exec "$(dirname "$(readlink -f "$0")")/install.sh"
