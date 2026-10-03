#!/usr/bin/env bash
# Makes InfluxDB's buckets and v1 users match the influxdb2-secret, then idles.
#
# Runs as a sidecar of InfluxDB, so it applies on every pod start; reloader
# restarts the pod when the secret changes, so a new password applies too.
# v1 users are used, not 2.x tokens, because InfluxDB generates every token's
# value itself while a v1 user's password can come from the vault.
#
# Environment: INFLUX_TOKEN (the admin token), INFLUX_ORG, BUCKET,
# INFLUXDB_HA_PASSWORD, INFLUXDB_GRAFANA_PASSWORD.
set -euo pipefail

export INFLUX_HOST=http://localhost:8086
# The CLI reads and writes a config file; keep it off the read-only image.
export INFLUX_CONFIGS_PATH=/tmp/influx-configs

log() { echo "bootstrap: $*"; }

# Waits out first-run setup too: the server listens on 8086, and the admin
# token works, only once setup has finished.
until influx ping >/dev/null 2>&1 && influx org list --name "${INFLUX_ORG}" >/dev/null 2>&1; do
    log "waiting for InfluxDB"
    sleep 5
done

bucket_id() {
    influx bucket list --name "$1" --hide-headers 2>/dev/null | cut -f 1
}

# ensure_bucket NAME: create it with infinite retention if it is missing.
# First-run setup creates the main bucket, so this matters only if it is lost.
ensure_bucket() {
    local name=$1
    if [ -z "$(bucket_id "${name}")" ]; then
        influx bucket create --name "${name}" --retention 0 >/dev/null
        log "created bucket ${name}"
    fi
    bucket_id "${name}"
}

# ensure_v1_user USERNAME PASSWORD read|write BUCKET_ID: one permission on one
# bucket. A user with any other permission is recreated, so a wider grant
# made in the UI doesn't survive a restart.
ensure_v1_user() {
    local username=$1 password=$2 action=$3 bucket=$4
    local existing
    # A username that doesn't exist is a 404, not an empty list. Any other
    # failure shows up when create fails below.
    existing=$(influx v1 auth list --username "${username}" --json 2>/dev/null || true)
    if grep -q '"token"' <<<"${existing}"; then
        local permissions
        permissions=$(grep -o "\"[a-z]*:orgs/[^\"]*\"" <<<"${existing}" || true)
        if [ "${permissions}" = "\"${action}:orgs/$(org_id)/buckets/${bucket}\"" ]; then
            influx v1 auth set-password --username "${username}" --password "${password}" >/dev/null
            log "${username}: in place, password applied"
            return
        fi
        influx v1 auth delete --username "${username}" >/dev/null
        log "${username}: permissions differed, recreating"
    fi
    influx v1 auth create --username "${username}" --password "${password}" \
        "--${action}-bucket" "${bucket}" \
        --description "${username} (managed by influxdb2-bootstrap)" >/dev/null
    log "${username}: created with ${action} on bucket ${bucket}"
}

org_id() {
    influx org list --name "${INFLUX_ORG}" --hide-headers | cut -f 1
}

bucket=$(ensure_bucket "${BUCKET}")
ensure_v1_user homeassistant "${INFLUXDB_HA_PASSWORD}" write "${bucket}"
ensure_v1_user grafana "${INFLUXDB_GRAFANA_PASSWORD}" read "${bucket}"
log "done"
# The sidecar's readiness probe. A restarted container gets a fresh /tmp, so
# it is never stale.
touch /tmp/bootstrap-done

# Idle until the pod stops, exiting promptly when it does.
trap 'exit 0' TERM INT
sleep infinity &
wait
