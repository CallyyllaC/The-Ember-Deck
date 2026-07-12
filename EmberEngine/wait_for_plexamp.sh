#!/usr/bin/env bash

# Systemd readiness gate: wait for Plexamp's real Node process, then allow a
# short settling period before Foundry starts workers that consume its state.

# Wait up to 30 seconds for the actual Plexamp headless process.
for _ in $(seq 1 30); do
    if /usr/bin/pgrep -f '[p]lexamp/js/index.js' >/dev/null; then
        sleep 3
        exit 0
    fi
    sleep 1
done

echo "Plexamp process did not appear within 30 seconds." >&2
exit 1
