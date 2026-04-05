#!/bin/bash
# =============================================================================
# Daily Drive — Secondary Scheduled Refresh
# =============================================================================
# Runs a second scheduled refresh (for example, your evening refresh).
# - Fetches fresh music and podcasts every run
# - Lets daily_drive.py choose the morning/evening profile from config.schedule
#
# Cron example:  0 16 * * * /opt/dailydrive/scripts/hourly-refresh.sh
# =============================================================================

set -euo pipefail

DAILYDRIVE_DIR="/opt/dailydrive"
LOG_DIR="${DAILYDRIVE_DIR}/logs"
LOG_FILE="${LOG_DIR}/dailydrive-$(date +%Y%m%d).log"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "${PYTHON_BIN}" ] && [ -x "/opt/homebrew/bin/python3" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3"
fi
if [ -z "${PYTHON_BIN}" ] && [ -x "/usr/local/bin/python3" ]; then
  PYTHON_BIN="/usr/local/bin/python3"
fi

# Make sure log directory exists
mkdir -p "${LOG_DIR}"

# --- Run the scheduled playlist refresh ---
echo "=== Secondary scheduled refresh started at $(date) ===" >> "${LOG_FILE}"
cd "${DAILYDRIVE_DIR}"
"${PYTHON_BIN}" daily_drive.py >> "${LOG_FILE}" 2>&1
EXIT_CODE=$?
echo "=== Secondary scheduled refresh finished at $(date) (exit code: ${EXIT_CODE}) ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"

exit ${EXIT_CODE}
