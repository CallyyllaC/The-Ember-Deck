EMBER DECK UPDATE
=================

What this brings to the Deck
  * Everything since the v2 release (16 October 2026): the code-review fixes,
    the panel layout rework, large dot-matrix lyrics, and the new phosphor
    oscilloscope on the Scope page.
  * The panel UI now runs in the kitty terminal instead of LXTerminal. kitty
    draws the Scope page's trace as pixels. The installer installs kitty.

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

     It asks for your password once, to install kitty with apt (the Pi
     needs internet for that step).
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

Also copied (optional, not used by the Deck)
  ~/EmberDeck-terminal-tests: the terminal probe, demos and benchmark from
  the terminal investigation. See its README.md.

Every run writes a log to ~/EmberDeck-backups/update-<date>.log.
