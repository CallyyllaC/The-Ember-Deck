#!/usr/bin/env bash
# Prepare the Burr-Brown USB DAC before the Ember Deck runtime starts.
# Recovery escalates from device discovery, through ALSA, to the PipeWire sink.
set -Eeuo pipefail

DAC_ID="08bb:2902"
SINK_MATCH="Burr-Brown_from_TI_USB_Audio_CODEC"

log() {
    echo "[dac-ready] $*"
}

wait_for() {
    local timeout="$1"
    shift

    for _ in $(seq 1 "$timeout"); do
        "$@" && return 0
        sleep 1
    done

    return 1
}

usb_present() {
    lsusb -d "$DAC_ID" >/dev/null 2>&1
}

alsa_present() {
    aplay -l 2>/dev/null | grep -q "USB Audio"
}

find_sink() {
    pactl list short sinks 2>/dev/null \
      | awk -v pattern="$SINK_MATCH" '$2 ~ pattern { print $2; exit }'
}

sink_present() {
    [[ -n "$(find_sink)" ]]
}

if ! wait_for 6 usb_present; then
    log "DAC absent. Resetting its USB controller."
    sudo -n /usr/local/sbin/emberdeck-dac-root reset-xhci

    if ! wait_for 15 usb_present; then
        log "DAC did not enumerate after controller reset."
        exit 1
    fi
fi

if ! wait_for 5 alsa_present; then
    log "DAC exists but ALSA card is missing. Reloading snd_usb_audio."
    sudo -n /usr/local/sbin/emberdeck-dac-root reload-usb-audio

    if ! wait_for 10 alsa_present; then
        log "ALSA still cannot see the DAC."
        exit 1
    fi
fi

if ! wait_for 5 sink_present; then
    log "Restarting PipeWire and WirePlumber to create DAC sink."
    systemctl --user restart wireplumber.service pipewire.service pipewire-pulse.service

    if ! wait_for 10 sink_present; then
        log "PipeWire did not create a Burr-Brown sink."
        exit 1
    fi
fi

sink="$(find_sink)"
pactl set-default-sink "$sink" || true

log "DAC ready: $sink"
