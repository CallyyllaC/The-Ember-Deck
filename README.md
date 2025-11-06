# The Ember Deck
A buildlog of the design and creation of my plexamp player, please note that these are not fullproof instructions, I have next to no idea what I am doing and this entire process is a learning experience I am deciding to document.
Things will vary between hardware used, I will not even try to pretend otherwise, but for all intent here is the hardware list:
Pi 5
USB DAC
Old TV Radio (Not working, please don't rip apart functioning ones, they're rare enough)
4.5" Touchscreen

## Pi Software Setup

### Update the Pi and Packages
```bash
sudo apt update && sudo apt full-upgrade -y
```
```bash
sudo reboot
```

### Add Bluetooth capability
#### Install Bluetooth speaker package
```bash
sudo apt install -y bluez pulseaudio-module-bluetooth
```

#### Pair Phone to Pi and configure as trusted
```bash
bluetoothctl
```
```bash
power on
agent on
scan on
pair <DEVICE_MAC>
trust <DEVICE_MAC>
connect <DEVICE_MAC>
exit
```

### Install plexamp headless
#### Install the packages and configure
```bash
sudo apt install -y wget nodejs
```
```bash
wget -O plexamp.tar.bz2 https://plexamp.plex.tv/headless/Plexamp-Linux-headless-v<version>.tar.bz2
```
```bash
tar -xvf plexamp.tar.bz2
```
```bash
node plexamp/js/index.js
```

#### Create a plexamp headless service
```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/plexamp.service
```

```ini
[Unit]
Description=Plexamp Headless
After=pipewire-pulse.service wireplumber.service network-online.target
Wants=pipewire-pulse.service wireplumber.service network-online.target

[Service]
Type=simple
WorkingDirectory=/home/<USERNAME>/plexamp
ExecStart=/usr/bin/node /home/<USERNAME>/plexamp/js/index.js --device pulse
Restart=on-failure
Environment=NODE_ENV=production

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now plexamp
```
```bash
sudo loginctl enable-linger <USERNAME>
```

### Embrace the lack of playback due to no valid output device
#### Set default audio device to USB DAC
```bash
pactl list short sinks
``` -> alsa_output.usb-Audio_CODEC-00.analog-stereo
```bash
pactl set-default-sink alsa_output.usb-Audio_CODEC-00.analog-stereo
```
```bash
pactl info | grep "Default Sink"
```
```bash
wpctl status
``` -> 57
```bash
wpctl set-default 57
```

#### Create default configs because my plexamp client is a drama queen
To test if you need this ensure that you can use the following commands and play music through Plexamp
```bash
pw-play /usr/share/sounds/alsa/Front_Center.wav
```
```bash
speaker-test -c2 -twav -D default
```

```bash
nano ~/.config/pulse/client.conf
```
```bash
default-sink = alsa_output.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo-output
```
```bash
sudo nano /etc/asound.conf
```
```ini                                                         
pcm.pulse {
    type pulse
}

ctl.pulse {
    type pulse
}

pcm.!default {
    type pulse
}

ctl.!default {
    type pulse
}
```

![Prototype Board](images/Prototype.jpg)

### Backup all your hard work (I will be saving it to my local plex media server)
#### Mount local plex server pc
```bash
sudo mkdir -p /mnt/plexserver
sudo mount -t cifs "//<SERVERIP>/<SERVERFOLDER>" /mnt/plexserver \
  -o username=<USERNAME>,password='<PASSWORD>',iocharset=utf8,file_mode=0777,dir_mode=0777,vers=3.0
```
  
#### Steam image to server
```bash
sudo dd if=/dev/mmcblk0 bs=4M status=progress | gzip -1 - > /mnt/plexserver/pi_backup_$(date +%F).img.gz
```

#### Unmount Server
```bash
sudo umount /mnt/plexserver
```
## Setting up the hardware
![TV Radio](images/TVRadio.jpg)