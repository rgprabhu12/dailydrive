#!/bin/bash
# =============================================================================
# Daily Drive — Scheduled Refresh
# =============================================================================
# Runs on a schedule (for example, your morning refresh).
# - Fetches fresh music and podcasts every run
# - Lets index.js choose the morning/evening profile from config.schedule
# - On macOS, schedules the next day's wake times with pmset
# - On macOS, can put the machine back to sleep after the run completes
# - Cleans up log files older than 7 days
#
# Cron example:  0 4 * * * /opt/dailydrive/scripts/daily-refresh.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DAILYDRIVE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${DAILYDRIVE_DIR}/logs"
LOG_FILE="${LOG_DIR}/dailydrive-$(date +%Y%m%d).log"

# launchd often provides a very minimal PATH, so include common Homebrew/macOS locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
if [ -z "${NODE_BIN}" ] && [ -x "/opt/homebrew/bin/node" ]; then
  NODE_BIN="/opt/homebrew/bin/node"
fi
if [ -z "${NODE_BIN}" ] && [ -x "/usr/local/bin/node" ]; then
  NODE_BIN="/usr/local/bin/node"
fi

# Make sure log directory exists
mkdir -p "${LOG_DIR}"

if [ -z "${NODE_BIN}" ]; then
  echo "❌ node not found in PATH" >> "${LOG_FILE}"
  exit 1
fi

# --- Log cleanup: delete log files older than 7 days ---
find "${LOG_DIR}" -name "dailydrive-*.log" -type f -mtime +7 -delete

# --- Optional auto-deploy: pull from a separate git checkout and sync here ---
GIT_REPO="${GIT_REPO:-}"
if [ -n "${GIT_REPO}" ] && [ "${GIT_REPO}" != "${DAILYDRIVE_DIR}" ] && [ -d "${GIT_REPO}/.git" ]; then
  echo "=== Auto-deploy: pulling latest code ===" >> "${LOG_FILE}"
  git -C "${GIT_REPO}" pull --ff-only >> "${LOG_FILE}" 2>&1 || echo "⚠️  Git pull failed (non-fatal, continuing with current code)" >> "${LOG_FILE}"
  # Sync code + config into the runtime directory (skip tokens, state, node_modules)
  cp "${GIT_REPO}/index.js" "${DAILYDRIVE_DIR}/index.js"
  cp "${GIT_REPO}/setup.js" "${DAILYDRIVE_DIR}/setup.js"
  cp "${GIT_REPO}/taste-profile.js" "${DAILYDRIVE_DIR}/taste-profile.js"
  [ -f "${GIT_REPO}/taste-profile-google.js" ] && cp "${GIT_REPO}/taste-profile-google.js" "${DAILYDRIVE_DIR}/taste-profile-google.js"
  cp "${GIT_REPO}/package.json" "${DAILYDRIVE_DIR}/package.json"
  cp -r "${GIT_REPO}/scripts/"* "${DAILYDRIVE_DIR}/scripts/"
  # Config lives in the repo (gitignored) — sync it if present
  [ -f "${GIT_REPO}/config.yaml" ] && cp "${GIT_REPO}/config.yaml" "${DAILYDRIVE_DIR}/config.yaml"
  [ -f "${GIT_REPO}/.env" ] && cp "${GIT_REPO}/.env" "${DAILYDRIVE_DIR}/.env"
  echo "=== Auto-deploy complete ===" >> "${LOG_FILE}"
fi

# --- Run the scheduled playlist refresh ---
echo "=== Scheduled refresh started at $(date) ===" >> "${LOG_FILE}"
cd "${DAILYDRIVE_DIR}"
"${NODE_BIN}" index.js >> "${LOG_FILE}" 2>&1
EXIT_CODE=$?
echo "=== Scheduled refresh finished at $(date) (exit code: ${EXIT_CODE}) ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"

RUN_POWER_ACTIONS=false
if [ "${EXIT_CODE}" -eq 0 ] && [ "$(uname -s)" = "Darwin" ] && command -v pmset >/dev/null 2>&1; then
  MORNING_TIME="$("${NODE_BIN}" -e "const fs=require('fs'); const yaml=require('js-yaml'); const config=yaml.load(fs.readFileSync('config.yaml','utf8')); const times=(config.schedule && Array.isArray(config.schedule.times)) ? config.schedule.times : []; if (!times[0]) process.exit(1); process.stdout.write(times[0]);" 2>>"${LOG_FILE}" || true)"
  EVENING_TIME="$("${NODE_BIN}" -e "const fs=require('fs'); const yaml=require('js-yaml'); const config=yaml.load(fs.readFileSync('config.yaml','utf8')); const times=(config.schedule && Array.isArray(config.schedule.times)) ? config.schedule.times : []; if (!times[1]) process.exit(1); process.stdout.write(times[1]);" 2>>"${LOG_FILE}" || true)"
  TIMEZONE="$("${NODE_BIN}" -e "const fs=require('fs'); const yaml=require('js-yaml'); const config=yaml.load(fs.readFileSync('config.yaml','utf8')); const tz=config.schedule && config.schedule.timezone; if (!tz) process.exit(1); process.stdout.write(tz);" 2>>"${LOG_FILE}" || true)"
  CURRENT_TIME="$(TZ="${TIMEZONE:-$(date +%Z)}" date '+%H:%M')"

  if [ -n "${MORNING_TIME}" ] && [ "${CURRENT_TIME}" = "${MORNING_TIME}" ]; then
    RUN_POWER_ACTIONS=true
  else
    echo "=== Skipping macOS wake/sleep actions for non-morning run (${CURRENT_TIME}, morning slot ${MORNING_TIME:-unknown}) ===" >> "${LOG_FILE}"
  fi
fi

if [ "${RUN_POWER_ACTIONS}" = true ]; then
  MORNING_HOUR="${MORNING_TIME%:*}"
  MORNING_MINUTE="${MORNING_TIME#*:}"
  EVENING_HOUR="${EVENING_TIME%:*}"
  EVENING_MINUTE="${EVENING_TIME#*:}"

  NEXT_MORNING_WAKE="$(date -v+1d -v"${MORNING_HOUR}"H -v"${MORNING_MINUTE}"M -v-2M '+%m/%d/%y %H:%M:00')"
  NEXT_EVENING_WAKE="$(date -v+1d -v"${EVENING_HOUR}"H -v"${EVENING_MINUTE}"M -v-2M '+%m/%d/%y %H:%M:00')"

  echo "=== Scheduling next wake times: ${NEXT_MORNING_WAKE} and ${NEXT_EVENING_WAKE} ===" >> "${LOG_FILE}"
  sudo /usr/bin/pmset schedule wakeorpoweron "${NEXT_MORNING_WAKE}" >> "${LOG_FILE}" 2>&1 || echo "⚠️  Failed to schedule next morning wake" >> "${LOG_FILE}"
  sudo /usr/bin/pmset schedule wakeorpoweron "${NEXT_EVENING_WAKE}" >> "${LOG_FILE}" 2>&1 || echo "⚠️  Failed to schedule next evening wake" >> "${LOG_FILE}"
fi

# On macOS, try to return the machine to sleep after the 4am run.
# This is intentionally non-fatal so the job result still reflects the refresh.
if [ "${RUN_POWER_ACTIONS}" = true ]; then
  echo "=== Requesting macOS sleep after morning refresh ===" >> "${LOG_FILE}"
  sudo /usr/bin/pmset sleepnow >> "${LOG_FILE}" 2>&1 || echo "⚠️  Failed to put Mac back to sleep" >> "${LOG_FILE}"
fi

exit ${EXIT_CODE}
