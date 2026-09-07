#!/bin/sh
# Regenerates /mosquitto/data/passwd from $MOSQUITTO_USERS on every start
# instead of committing hashed credentials to the repo. Format:
# "user1:pass1,user2:pass2,..." - see docker-compose.yml's mosquitto
# service for how it's built from .env.
set -e

passwd_file=/mosquitto/data/passwd
: > "$passwd_file"

old_ifs=$IFS
IFS=,
for pair in $MOSQUITTO_USERS; do
  user=${pair%%:*}
  pass=${pair#*:}
  mosquitto_passwd -b "$passwd_file" "$user" "$pass"
done
IFS=$old_ifs
chmod 600 "$passwd_file"
chown mosquitto:mosquitto "$passwd_file"

exec mosquitto -c /mosquitto/config/mosquitto.conf
