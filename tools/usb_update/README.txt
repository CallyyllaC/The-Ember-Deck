EMBER DECK UPDATE
=================

This folder updates an existing Ember Deck (the program in ~/EmberDeck) to
the version in VERSION. CHANGELOG.md, in this folder, lists what each
version changes and anything to know before updating. It works from any
earlier version: the Deck simply gets this version's program files.

The panel UI runs in the kitty terminal; if the Deck does not have kitty
yet, the installer installs it first.

What it does not touch
  * No existing config file is changed: ~/EmberDeck/configs (settings,
    *.local.yaml, secrets), Labwc's rc.xml, the systemd units and the
    LXTerminal profile all stay exactly as they are. The panel reuses the
    LXTerminal profile's font and window rules by reading them.
  * ~/EmberDeck/.venv and ~/EmberDeck/logs are left alone.

How to run it
  1. Copy this whole folder to a USB stick and plug it into the Deck.
  2. On the Deck, as the user that runs Ember Deck (a terminal on the
     desktop, or SSH), run:

         bash /media/$USER/<usb-name>/<this-folder>/install.sh

     If kitty is not installed yet, it asks for your password once to
     install it with apt (the Pi needs internet for that step).
  3. It checks the update files and your install, validates your config
     files against the new code, stops the Deck, backs up ~/EmberDeck to
     ~/EmberDeck-backups, copies the new code in, starts the Deck again and
     watches it for about 25 seconds.
  4. If the runtime or the kitty panel does not come up healthy, it restores
     the backup and restarts the old version by itself.

To undo it later
         bash /media/$USER/<usb-name>/<this-folder>/rollback.sh
  restores the newest backup (program files; configs were never changed).
  kitty stays installed either way.

Optional afterwards: your supporter code
  1. Support Awoo on Ko-fi while signed in to Ko-fi, then claim your code at
     https://awoo.ltd/support/activate (it is emailed to you).
  2. On the Deck, after the update:

         ~/EmberDeck/.venv/bin/python ~/EmberDeck/supporter_check.py --install

     Paste the code when asked (it is hidden and checked before saving), then
     restart the panel:  systemctl --user restart ember-ui.service
  3. Check it any time:

         ~/EmberDeck/.venv/bin/python ~/EmberDeck/supporter_check.py

     The Console page also shows SUPPORTER and a tick once it is accepted.
  Help with error messages: AWOO-SUPPORTER-SYSTEM.md in this folder.

Optional afterwards: the boot set
  The Awoo boot screen and the boot running light change system settings,
  so they are a separate step that lists every change and asks first:

         sudo bash /media/$USER/<usb-name>/<this-folder>/boot-set/install-boot-set.sh

  Undo it with the same command plus --uninstall. Reboot to see it.
  Running it again keeps any boot theme file you changed on your Deck
  (flipped images, an edited script); add --force to replace them.

Every run writes a log to ~/EmberDeck-backups/update-<date>.log.
