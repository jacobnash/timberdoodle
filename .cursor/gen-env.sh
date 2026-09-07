#!/usr/bin/env bash
# Idempotently ensure a local .env exists with the secrets the stack needs.
# Sourced by both install.sh and start.sh: .env is gitignored and, with
# environment builds, install does not run on every boot, so start must also
# be able to (re)create it. Both HMAC secrets are just long random keys (not
# real credentials), so generating them locally is safe and deterministic per
# environment; the file persists once created.
ensure_timberdoodle_env() {
  local dir="$1"
  ( cd "$dir" || return 1
    if [ ! -f .env ]; then
      cp .env.example .env
    fi
    if ! grep -qE '^TIMBERDOODLE_JWT_SECRET=.+' .env; then
      sed -i "s|^TIMBERDOODLE_JWT_SECRET=.*|TIMBERDOODLE_JWT_SECRET=$(openssl rand -hex 32)|" .env
    fi
    if ! grep -qE '^TIMBERDOODLE_GATEWAY_SECRET=.+' .env; then
      sed -i "s|^TIMBERDOODLE_GATEWAY_SECRET=.*|TIMBERDOODLE_GATEWAY_SECRET=$(openssl rand -hex 32)|" .env
    fi
    # Allow webhook registration/delivery to local receivers (dev/test only) -
    # see CLAUDE.md; the integration suite's webhook tests need this. The
    # example line carries an inline comment, so replace the whole line.
    if ! grep -qE '^TIMBERDOODLE_ALLOW_PRIVATE_WEBHOOKS=1$' .env; then
      sed -i 's|^TIMBERDOODLE_ALLOW_PRIVATE_WEBHOOKS=.*|TIMBERDOODLE_ALLOW_PRIVATE_WEBHOOKS=1|' .env
    fi
  )
}
