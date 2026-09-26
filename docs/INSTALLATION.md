# Installation

[← Software architecture](SOFTWARE.md) · [Media gallery →](MEDIA.md)

These steps describe the repository’s current software shape, but hardware device names and the final graphical service must be checked on the live Raspberry Pi.

## Base packages

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y \
  python3 python3-venv python3-pip python3-dev python3-lgpio \
  build-essential git curl wget dos2unix \
  libportaudio2 libportaudiocpp0 portaudio19-dev \
  libusb-1.0-0 i2c-tools alsa-utils \
  pipewire pipewire-pulse pipewire-bin wireplumber \
  bluez libspa-0.2-bluetooth cifs-utils playerctl
```

Enable I²C and grant the runtime user access to the relevant hardware groups:

```bash
sudo raspi-config nonint do_i2c 0
sudo usermod -aG gpio,i2c,plugdev,audio "$USER"
```

Log out completely or reboot before relying on new group membership.

## Deployment layout and Python environment

The systemd units deliberately expect the runnable `EmberEngine` directory at `~/EmberDeck`. From a repository checkout, copy that directory (or make an equivalent symlink) before creating the environment:

```bash
git clone <repository-url> ~/The-Ember-Deck
cp -a ~/The-Ember-Deck/EmberEngine ~/EmberDeck
cd ~/EmberDeck
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install \
  numpy sounddevice pyyaml plexapi blinkstick pyusb \
  adafruit-circuitpython-ads1x15 adafruit-blinka gpiozero \
  pillow psutil rich textual
```

`adafruit-blinka` provides the `board` module. Do not install the unrelated PyPI package named `board`.

`docs/requirements.txt` is a historical full-machine `pip freeze`, not a minimal lock file. It includes unrelated Raspberry Pi desktop packages and is useful for provenance, but the command above is the supported application dependency set.

Before starting services, edit `~/EmberDeck/configs/minstrel.yaml` and set the Deck's exact Plexamp client name plus one complete login method. Protect a file containing credentials:

```bash
chmod 600 ~/EmberDeck/configs/minstrel.yaml
```

Credentials can instead be supplied through the `EMBERDECK_PLEX_*` environment values in the [configuration reference](CONFIGURATION.md).

## Plexamp Headless

Install Plexamp Headless using the current package and Node.js requirements published for the chosen Plexamp release. A typical user service is:

```ini
[Unit]
Description=Plexamp Headless
After=pipewire-pulse.service wireplumber.service network-online.target
Wants=pipewire-pulse.service wireplumber.service network-online.target

[Service]
Type=simple
WorkingDirectory=%h/plexamp
ExecStart=/usr/bin/node %h/plexamp/js/index.js --device pulse
Restart=on-failure
Environment=NODE_ENV=production

[Install]
WantedBy=default.target
```

Enable user services after creating them:

```bash
systemctl --user daemon-reload
systemctl --user enable --now plexamp.service
sudo loginctl enable-linger "$USER"
```

## USB DAC and PipeWire

Inspect the available sinks rather than copying a temporary numeric ID:

```bash
pactl list short sinks
pactl info | grep 'Default Sink'
wpctl status
```

`ensure_dac.sh` checks USB enumeration, ALSA and PipeWire in stages and can select the Burr-Brown sink. Its privileged recovery helper must be installed and restricted separately if automatic USB-controller or driver recovery is required.

Basic output tests:

```bash
pw-play /usr/share/sounds/alsa/Front_Center.wav
speaker-test -c2 -twav -D default
```

Plexamp on linux can be very picky about audio devices, this should solve that, if not this can be usually be solved by selecting default audio device again in settings and it will update its internal state.

## BlinkStick permissions

Create `/etc/udev/rules.d/99-blinkstick.rules`:

```udev
SUBSYSTEM=="usb", ATTR{idVendor}=="20a0", ATTR{idProduct}=="41e5", MODE="0666"
SUBSYSTEM=="hidraw", KERNEL=="hidraw*", ATTRS{idVendor}=="20a0", ATTRS{idProduct}=="41e5", MODE="0666"
```

Reload and test:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
blinkstick --info
```

Older BlinkStick releases may need `Callable` imported from `collections.abc` rather than `collections`.

## Ember Engine service

The checked-in launchers derive the project directory from their own location, while the systemd units use `%h` for the current user’s home directory. The project can therefore live under any user account, provided the service files and launchers remain together in the expected layout.

Install the appropriate repository files under the user service directory:

```bash
mkdir -p ~/.config/systemd/user
cp ~/EmberDeck/emberdeck.service \
   ~/EmberDeck/ember-ui.service \
   ~/EmberDeck/ember-browser.service \
   ~/.config/systemd/user/
```

The runtime service calls `run_foundry.sh`. Plexamp is ordered first but is a soft dependency, so temporarily unavailable Plexamp does not disable physical I/O, Bluetooth, MPRIS, or LEDs:

```bash
chmod +x ~/EmberDeck/*.sh
systemctl --user daemon-reload
systemctl --user enable --now emberdeck.service ember-ui.service ember-browser.service
```

The repository currently includes a Textual `ember-ui.service` and a Firefox kiosk launcher.

- Textual panel: `ember-ui.service` opens the diagnostic UI in LXTerminal; the `emberdeck` LXTerminal profile and Labwc graphical session must exist.
- Firefox kiosk: `ember-browser.service` waits up to 90 seconds for Wayland and `http://127.0.0.1:32500` before systemd retries it.

## Shutdown helper and uinput

Pawprint needs `/dev/uinput` access for virtual media keys. Install the supplied udev and module configuration from `EmberEngine/system` using root-owned files and reload the relevant rules.

Before installing `99-emberdeck-uinput.rules`, replace `[USERNAME]` with the Linux account that runs Ember Deck. Udev does not expand shell variables or systemd `%` specifiers in a `GROUP=` assignment, so this value must name a real local user or an intentionally configured device-access group.

```bash
sudo install -m 0644 ~/EmberDeck/system/emberdeck-uinput.conf /etc/modules-load.d/emberdeck-uinput.conf
sudo sed 's/\[USERNAME\]/your-user/' ~/EmberDeck/system/99-emberdeck-uinput.rules \
  | sudo tee /etc/udev/rules.d/99-emberdeck-uinput.rules >/dev/null
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=uinput
```

The Eject hold calls `/usr/local/sbin/emberdeck-poweroff`. Install the supplied helper as a root-owned, non-writable executable:

```bash
sudo install -o root -g root -m 0755 \
  ~/EmberDeck/system/emberdeck-poweroff /usr/local/sbin/emberdeck-poweroff
```

Then create a root-owned `/etc/sudoers.d/emberdeck-poweroff` using `visudo -f` with only this rule (replace `your-user`):

```sudoers
your-user ALL=(root) NOPASSWD: /usr/local/sbin/emberdeck-poweroff
```

Do not grant the service account unrestricted passwordless `sudo`.

## EasyEffects

EasyEffects is optional and provides the output equaliser, limiter and loudness chain:

```bash
sudo apt install -y easyeffects
easyeffects
```

## Verification

```bash
i2cdetect -y 1
python -m pip check
python -m compileall -q ~/EmberDeck
python -m unittest discover -s ~/The-Ember-Deck/tests -p 'test*.py' -v
python -m sounddevice
systemctl --user status plexamp.service emberdeck.service ember-ui.service ember-browser.service
journalctl --user -u emberdeck.service -b
```

The I²C scan should show devices at `0x48` and `0x70`. Confirm the DAC, capture device, BlinkStick, both selectors, every tape key and the safe-shutdown hold before closing the enclosure.
