#!/usr/bin/env bash
# Follow the supervised runtime's user-journal output in compact form.
exec /usr/bin/journalctl --user -u emberdeck.service -n 250 -f -o cat
