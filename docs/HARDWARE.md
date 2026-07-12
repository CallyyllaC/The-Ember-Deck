# Hardware and I/O

[← Project overview](../README.md) · [Software architecture →](SOFTWARE.md)

This page records the hardware used in the finished V1 Deck and how the physical controls are presented to the software.

Earlier versions of the project used different pins, different analogue plans and several ideas that never made it into the final machine. Those old notes are useful as history, but they should not be treated as wiring instructions. Use this document alongside the live `pawprint.py` configuration, because GPIO mistakes are a particularly tedious way to spend an afternoon.

## Main components

### Computing and displays

- Raspberry Pi 5
- 4.3-inch touchscreen for Plexamp and direct media control
- 5.6-inch secondary display for the Ember Deck status, visualisations and system pages
- Powered USB hub
- Panel-mounted fast-charge/data USB connection
- Panel-mounted Ethernet connection
- ADS1115 analogue-to-digital converter at I²C address `0x48`
- HT16K33 LED driver at I²C address `0x70`
- BlinkStick Pro USB LED controller

The Pi sits at the centre of the system, but quite a lot of the hardware is deliberately kept independent of it. The analogue VU meter, amplifier controls, fan controls and panel power meter all continue to do their jobs without waiting on Linux.

### Audio

- Burr-Brown/PCM2902-class USB DAC
- Class-D 2.1 amplifier
- Two full-range left and right speakers (Visaton FRS 8, 2017, 8 Ω)
- Internal subwoofer in a sealed MDF enclosure (Visaton W 100 S-8, 8 Ω)
- Original analogue meter reused as a hardware VU meter, required a dedicated VU Board.
- Direct hardware volume control
- Direct hardware bass control
- Direct hardware bass threshold control
- Stereo line input
- Monitored headphone output

The Raspberry Pi sends digital audio to the USB DAC. From there, the signal passes through the analogue audio hardware and into the 2.1 amplifier.

The VU meter is also analogue. It does not depend on Ember Engine, shared state or any other software layer. This is useful because it is hardware volume agnostic.

### Power and cooling

- Internal 12 V PSU
- Fused UK mains inlet
- Main power switch
- 10 A inline DC fuse
- Automotive blade-fuse distribution block
- 12 V to 5 V USB-C PD converter for the Raspberry Pi
- Additional DC conversion for the displays and USB hardware
- Panel-mounted voltage and current meter
- Two 12 V fans / control board.

The fans are controlled directly by a temperature control board. They are not software-managed and do not need to be included in the GPIO map.

## Power distribution

The broad power path is:

```text
UK plug, fused at 3 A
        ↓
12 V DC power supply
        ↓
10 A inline DC fuse and main switch
        ↓
fused 12 V distribution block
        ├── Raspberry Pi PD converter
        ├── LED Visualiser
        ├── powered USB hub
        ├── 12v -> 15v -> 2.1 audio amplifier
        ├── secondary display
        └── cooling fans
```
Whilst the AMP board supports 12v, I opted for 15V as it sounded a bit aenemic, it might sound better with more as I think this amp board can go a lot higher input wise but I didn't test it.

All low-voltage returns meet at the negative side of the distribution block rather than being daisy-chained through other devices.

High-current branches use heavier cable than the GPIO and signal wiring. Stranded cable ends are terminated with suitable crimps, and audio or signal runs are kept away from the noisier power and LED wiring where practical. Also use ferrites when necisarry.

The exact fuse fitted to each branch must suit both the installed cable and the expected load.

## External connections and monitoring

The finished enclosure exposes the useful everyday connections without requiring the Deck to be opened again:

| Connection | Direction | Purpose |
|:--|:--:|:--|
| Ethernet | Input/output | Wired networking for Plexamp, administration and media services |
| USB fast charge | Power output | Charges a phone or other accessory |
| USB data | Input/output | Connects peripherals and provides service access through the internal hub |
| Stereo line input | Audio input | Left and right analogue input to the audio system |
| Headphone/monitor | Audio output | Monitored stereo output for headphones or external equipment |
| Voltage/current meter | Measurement | Shows live supply voltage and current draw |

The line input and monitor output are analogue connections and are direct to the USB DAC not to the Pi.

The voltage/current display is also entirely hardware-based, measuring the internal supply directly.

## I²C bus

The ADS1115 and HT16K33 share Raspberry Pi I²C bus 1.

| Raspberry Pi pin | Function |
|:--|:--|
| GPIO 2 | SDA |
| GPIO 3 | SCL |

The connected devices are:

| Device | Address | Purpose |
|:--|:--:|:--|
| ADS1115 | `0x48` | Reads the front-panel analogue controls |
| HT16K33 | `0x70` | Drives the graph and status LEDs |

Both devices can be checked with:

```bash
i2cdetect -y 1
```

A working bus should show devices at `48` and `70`.

## Analogue inputs

The current Pawprint setup uses two ADS1115 channels:

| Channel | Function | Behaviour |
|:--:|:--|:--|
| A0 | Colour control | Changes colour/contrast normally and saturation while the REC control bank is active |
| A1 | Gain control | Changes visualiser gain normally and brightness while the REC control bank is active |
| A2 | Unused | Unused |
| A3 | Unused | Not opened by Pawprint, but physically connected to a spare POT I have fitted. |

Values are calibrated and normalised through `configs/pawprint.yaml`.

The controls use soft pickup. When a stored software value and the physical knob position do not match, the value does not jump immediately. The knob must first cross the stored value before it takes control again. This prevents sudden changes after boot or when switching control banks.

## Tape keys

The original tape-deck buttons are active-low GPIO inputs with software debouncing.

| Physical key | Action | GPIO |
|:--|:--|:--:|
| Play | Play/pause | 6 |
| Stop/Pause | Stop playback | 16 |
| Rewind | Previous track | 12 |
| Fast-forward | Next track | 8 |
| Eject | Hold for orderly shutdown | 21 |
| Record | Change control bank; hold to cycle HDMI-2 source | 5 |

Pawprint sends transport commands to whichever source is currently active: Plexamp, Bluetooth or a local MPRIS player.

Eject has no short-press action. Holding it for roughly five seconds flashes the front red LED and calls the shutdown helper:

```text
/usr/local/sbin/emberdeck-poweroff
```

## Selectors

### Four-position selector

The four-position selector uses GPIO 22 and GPIO 23. It selects the visualiser mode.

```python
SEL4_LOOKUP = {
    0b11: 1,
    0b10: 2,
    0b01: 3,
    0b00: 4,
}
```

### Three-position selector

The three-position selector uses GPIO 24 and GPIO 25. It publishes the current source or control context.

```python
SEL3_LOOKUP = {
    0b11: 3,
    0b10: 1,
    0b01: 2,
}
```

## Graph and status LEDs

The HT16K33 drives sixteen discrete LEDs in total:

- ten graph segments
- five top status indicators
- one front red control/status indicator

The matrix mapping is:

| Output | Matrix coordinate | Behaviour |
|:--|:--:|:--|
| Graph 1–8 | `(0, 0)` through `(0, 7)` | Track-progress segments 1–8 |
| Graph 9 | `(8, 0)` | Track-progress segment 9 |
| Graph 10 | `(8, 1)` | Track-progress segment 10 |
| Front red | `(8, 2)` | REC control bank and shutdown feedback |
| Top green 1 | `(8, 3)` | Process heartbeat |
| Top green 2 | `(8, 4)` | Network activity |
| Top amber | `(8, 5)` | Disk activity |
| Top red 1 | `(8, 6)` | Low-storage warning |
| Top red 2 | `(8, 7)` | Low-memory warning |

Pawprint normally owns the ten-segment graph and displays media progress from left to right.

Other workers can request a temporary override through shared output state. This is used for short-lived feedback such as source selection without permanently taking the graph away from track progress.

## RGBW visualiser

The main audio visualiser is a separate 24-pixel RGBW strip controlled through a BlinkStick Pro.

Echo analyses the captured audio and publishes the visualiser data. Foxfire turns that data into LED output.

The strip has independent controls for:

- gain
- brightness
- colour
- saturation

It is separate from the HT16K33 graph and status indicators.
