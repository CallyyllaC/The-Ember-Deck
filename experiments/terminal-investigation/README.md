# Terminal investigation: proofs of concept and Pi tests

Investigation material for choosing the panel UI's terminal. Nothing here is
used by the Ember Deck runtime, and nothing here changes the Pi's
configuration. The decision report is published separately; this folder holds
the code behind it and the tests that can only be run on the Deck.

| File | What it is | Runs on |
|:--|:--|:--|
| `textsize_filter.py` | Placeholder → kitty OSC 66 translation for a Textual driver | anywhere |
| `verify_textsize_model.py` | Replays Textual's real output through the filter into a model of kitty's multicell rules | any PC with Textual |
| `visualisers.py` | Prototype renderers (pixel and character) and a benchmark | anywhere, benchmark on the Pi |
| `pi_probe.py` | Asks a terminal what it supports, then draws samples | the Pi, in each terminal |
| `kitty_textsize_demo.py` | Textual app with large text through the filter | the Pi, in kitty |
| `kitty_graphics_demo.py` | Textual app with a pixel visualiser from Echo's live data | the Pi, in kitty, Deck running |

## Tests to run on the Pi

None of these change the Deck. Installing kitty (`sudo apt install kitty`,
Debian Trixie ships 0.41.1) is the only system change, and it is yours to make.

1. **Environment facts.** Record the output of:
   ```bash
   cat /etc/os-release | head -3; uname -m
   wlr-randr
   glxinfo -B | grep -E "OpenGL (core profile )?version|renderer"
   pactl get-default-source; pactl list short sources
   dpkg -l lxterminal libvte-2.91-0 kitty foot 2>/dev/null | grep ^ii
   ```
   `wlr-randr` confirms the panel output and its 640 × 480 mode. `glxinfo`
   should show OpenGL 3.1 (kitty needs 3.1 on Linux). The default source
   tells us what Echo hears (a `.monitor` source hears every player).
2. **Current terminal.** In the `emberdeck` LXTerminal profile, full screen on
   the panel: `python3 pi_probe.py | tee probe-lxterminal.txt`, then photograph
   the screen. Expected: no OSC 66, no kitty graphics, no Sixel.
3. **kitty capabilities.** On the panel output:
   ```bash
   kitty --start-as=fullscreen -o font_size=9 -o hide_window_decorations=yes \
       -o window_padding_width=0 python3 pi_probe.py | tee probe-kitty.txt
   ```
   Expected: `osc66_width True`, `osc66_scale True`, `kitty_graphics True`,
   `cell_px` (record it: it fixes the grid). Photograph lines 4 to 7.
   Repeat with `font_size` 8, 10 and 11 to see which grid you like.
4. **Large text in Textual.**
   `kitty ... python3 kitty_textsize_demo.py` (same options). Leave it 10
   minutes. Press `r` (full repaint) and `h` (hide/show) a few times.
   Pass: large text never leaves fragments, the clock beside it never shifts
   sideways, nothing flickers.
5. **Pixel visualiser in Textual.** With the Deck runtime running and music
   playing:
   ```bash
   TEXTUAL_COLOR_SYSTEM=truecolor kitty ... python3 kitty_graphics_demo.py --style bars --update edit --medium shm
   ```
   Then `--update retransmit`, then `--medium direct`, then each `--style`.
   Record the fps and CPU it prints on quit, and whether the image updates,
   flickers or disappears. Check `top` for kitty's own CPU while it runs.
6. **Renderer cost.** `python3 visualisers.py`: milliseconds per frame on the
   Pi for every renderer.
7. **Coexistence.** With kitty on the panel and Firefox on HDMI-2 both running
   for an hour: audio has no dropouts (Echo and playback), kitty's memory
   (`ps -o rss,cmd -C kitty`) stays flat, `vcgencmd measure_temp` is sane.
8. **Recovery.** `pkill kitty`, then confirm `ember-ui.service` would restart it
   (only once a kitty launcher exists; not part of this investigation).
