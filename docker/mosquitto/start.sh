#!/bin/sh
# Start script of the optional `mqtt` service in docker-compose.yml.
# Writes Mosquitto's password file from BROKER_USERNAME and BROKER_PASSWORD
# (.env), then starts the broker. Without a password it does not start: a
# broker on the plant network must not be open to everyone.
set -eu

if [ -z "${BROKER_PASSWORD:-}" ]; then
    echo "BROKER_PASSWORD is empty: set it in .env. The broker does not start without a login." >&2
    exit 1
fi

# One login, written again on every start: a user added to this file by hand is
# gone after a restart. Readable by the broker only.
umask 077
mosquitto_passwd -b -c /mosquitto/data/passwd "${BROKER_USERNAME:-vision}" "$BROKER_PASSWORD"

# The broker gives up root for the image's `mosquitto` user once it has
# started; that user has to own its data and the password file.
chown -R mosquitto:mosquitto /mosquitto/data 2>/dev/null || true

exec mosquitto -c /mosquitto/config/mosquitto.conf
