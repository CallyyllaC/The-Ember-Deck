# Awoo supporter features in The Ember Deck

The Ember Deck is the same for everyone. Holders of an Awoo supporter licence get one extra, cosmetic touch as a thank-you. Nothing else depends on it, and the code is open: this is an honour system, not copy protection.

## What supporters get

| Feature | Supporters | Everyone else |
|:--|:--|:--|
| Panel page changes | A CRT switch-off and switch-on: the picture collapses to a bright line, then a dot, the page changes behind the black screen, and the new page switches back on | Pages change instantly, as they always have |

The boot screen, the boot running light, the LED strip's ignition animation, the phosphor scope and every other feature are the same for everyone.

## Getting a supporter code

1. Support Awoo on Ko-fi through the [Awoo support page](https://awoo.ltd/support), **signed in to your Ko-fi account** (a guest donation cannot be linked to a supporter code).
2. Open [awoo.ltd/support/activate](https://awoo.ltd/support/activate) and enter the email address of that Ko-fi account. Your permanent supporter code is emailed to you; check your spam folder if it does not arrive. Requests are rate-limited, so wait before asking again.

The code is a single line starting with `AWOO1.`. Keep it to yourself: it is yours, and there is no need to share it with anyone to use it.

## Installing it on the Deck

On the Deck, as the user that runs Ember Deck:

```bash
~/EmberDeck/.venv/bin/python ~/EmberDeck/supporter_check.py --install
```

Paste the code when asked and press Enter. It is not shown on screen and does not go into your shell history. The code is checked first and saved only if it is valid, to `~/.config/awoo/supporter.licence`, readable by you alone. Then restart the panel so it picks the code up:

```bash
systemctl --user restart ember-ui.service
```

Installing by hand also works: put the code on one line in `~/.config/awoo/supporter.licence` and run `chmod 600` on it. That file does not exist until you create it.

## Checking it

```bash
~/EmberDeck/.venv/bin/python ~/EmberDeck/supporter_check.py
```

It says whether the installed code is accepted, or why not, without printing the code. On the panel itself, the Console page's status band shows `SUPPORTER ✓` once a valid code is in place, or a short reason it was not accepted (shown only when a licence file exists).

| Message | What to do |
|:--|:--|
| No supporter licence at … | Install your code as above. |
| the Python 'cryptography' package is not installed | `~/EmberDeck/.venv/bin/pip install cryptography`, then check again. |
| the supporter licence is not in AWOO1 format | The file holds something other than the code: paste the whole `AWOO1.` line from the email, nothing else. |
| the supporter licence signature is invalid | The code was changed or cut short while copying; paste it again from the email. |
| the supporter licence was signed with an unknown key | The code was issued for a newer signing key than this version of Ember Deck knows; update the Deck. |

Adding, replacing or removing the code takes effect the next time the panel UI starts.

## How it works

- Format and verification: `EmberEngine/awoo_supporter.py` (Awoo licence v1, Ed25519, production key `awoo-2026-01`). It is checked offline, once, when the panel UI starts. There is no network check, no expiry and no device binding.
- `supporter_licence_path` in `ember_ui.yaml` moves the licence file; `supporter_check.py` follows the same setting. A licence file inside a repository checkout is gitignored.
- Verification needs the Python `cryptography` package in the Deck's environment. Without it, or with a missing, unreadable or invalid licence, the panel behaves exactly as it does for everyone else.
- The transition is drawn by kitty above Textual's text (`crt_transition.py`); its frames are rendered once and uploaded when the UI starts, so a transition only sends small "show frame" commands. It needs the panel to run in kitty, as it does since the kitty migration.
- `page_transitions: false` in `ember_ui.yaml` turns it off for a supporter who prefers instant changes.
- No other worker reads the licence. Discord Rich Presence in particular stays independent of supporter status (enforced by `tests/test_rich_presence.py`).
- The feature has no Synapse keys of its own: the panel UI checks the licence for itself and nothing else needs to know.
