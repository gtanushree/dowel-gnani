#!/usr/bin/env bash
# The "inventory" repository on its tidy branch: a refactor with no change in behaviour and no planted
# problems. Whatever a review reports as an issue here is a false alarm.
set -euo pipefail
"$(dirname "$0")/inventory.sh" "${1:?target directory}"
git -C "$1" switch -q tidy
