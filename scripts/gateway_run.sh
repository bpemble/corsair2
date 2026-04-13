#!/bin/bash
# Wrapper around the stock gnzsnz/ib-gateway entrypoint that also launches
# the TOTP helper in the background. The helper watches for the 2FA dialog
# and auto-enters the code via xdotool.

# The stock run.sh sets DISPLAY=:1 internally, but our helper forks before
# that happens. Export it here so xdotool can find the gateway's X server.
export DISPLAY=:1

# Launch TOTP helper if secret is configured
if [ -f /mnt/scripts/totp_helper.sh ] && [ -n "$TOTP_SECRET" ]; then
    echo ".> Launching TOTP helper in background"
    bash /mnt/scripts/totp_helper.sh &
fi

# Hand off to the stock entrypoint
exec /home/ibgateway/scripts/run.sh
