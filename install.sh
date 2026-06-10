#!/usr/bin/env bash
# install.sh — install tofu-search and its (optional) extras.
#
# Usage:
#   ./install.sh                core dependencies only
#   ./install.sh --playwright   core + Playwright, then install the chromium browser
#   ./install.sh --pdf          core + PDF extraction (pymupdf / pymupdf4llm)
#   ./install.sh --all          everything above
#
# Honors $PIP (default: "python3 -m pip"). Installs the package itself in
# editable mode so local changes are picked up.
set -euo pipefail

cd "$(dirname "$0")"

PIP="${PIP:-python3 -m pip}"
WANT_PLAYWRIGHT=0
WANT_PDF=0

for arg in "$@"; do
  case "$arg" in
    --all)        WANT_PLAYWRIGHT=1; WANT_PDF=1 ;;
    --playwright) WANT_PLAYWRIGHT=1 ;;
    --pdf)        WANT_PDF=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 2 ;;
  esac
done

EXTRAS=""
if [ "$WANT_PLAYWRIGHT" -eq 1 ] && [ "$WANT_PDF" -eq 1 ]; then
  EXTRAS="[all]"
elif [ "$WANT_PLAYWRIGHT" -eq 1 ]; then
  EXTRAS="[playwright]"
elif [ "$WANT_PDF" -eq 1 ]; then
  EXTRAS="[pdf]"
fi

echo "==> Installing tofu-search${EXTRAS} (editable)"
$PIP install -e ".${EXTRAS}"

if [ "$WANT_PLAYWRIGHT" -eq 1 ]; then
  echo "==> Installing Chromium for Playwright"
  python3 -m playwright install chromium || {
    echo "WARNING: 'playwright install chromium' failed. SPA/bot-protection" >&2
    echo "         fallback will be disabled until Chromium is installed." >&2
  }
fi

echo "==> Verifying import"
python3 -c "import tofu_search; print('tofu-search', tofu_search.__version__, 'OK')"

echo "Done."
