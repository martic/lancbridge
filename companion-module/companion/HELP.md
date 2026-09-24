# v1sdi-remote Companion module

Controls the Roland V-1SDI switcher via the `v1sdi_midi.py` daemon
(https://github.com/martic/v1sdi-remote) running on the same Pi as Companion.

## Config
- **Base URL** — default `http://127.0.0.1:8789` (daemon on the same Pi).

## Actions
- PGM / PST input select (1-4)
- AUTO (timed take), CUT (instant take)
- Transition type: Wipe / Mix / Cut
- PinP, SPLIT, DSK, FREEZE toggles
- Memory select (1-8)

## Feedbacks
- Input is on PGM / PST (light the matching button)
- Transition type is wipe/mix/cut
- DSK / PinP / SPLIT active

Feedback polls the daemon's `/status` (1s), which itself mirrors the
switcher via SysEx RQ1 polling — so buttons follow manual changes on the unit.
