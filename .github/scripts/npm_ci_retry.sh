#!/usr/bin/env bash
# Install the lockfile-exact Node dependencies without install scripts. A
# transient registry or network error gets two more tries 15 s apart, then the
# step fails clearly (#3439). NPM, NPM_CI_ATTEMPTS and NPM_CI_DELAY_S exist for
# the contract test; workflows leave them unset.
set -u
attempts="${NPM_CI_ATTEMPTS:-3}"
delay="${NPM_CI_DELAY_S:-15}"
for ((attempt = 1; attempt <= attempts; attempt++)); do
  if "${NPM:-npm}" ci --ignore-scripts; then
    exit 0
  fi
  if ((attempt < attempts)); then
    echo "npm ci failed (attempt ${attempt} of ${attempts}); retrying in ${delay} s"
    sleep "$delay"
  fi
done
echo "::error::npm ci --ignore-scripts failed after ${attempts} attempts"
exit 1
