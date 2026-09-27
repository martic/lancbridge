# Sony CamAPI Companion module

Companion module for the Sony Camera Remote API (ScalarWebAPI v1) — drives the
HXR-MC2500 (or any Sony camera speaking this API) over Wi-Fi from a Streamdeck.

Pair with `camapi.py` in the repo root: run `sudo python3 camapi.py discover`
on the Pi to find and cache the camera's endpoint URL, then enter that URL in
the module config (usually `http://<camera-ip>:10000/`).

## Actions
- **Zoom** — direction (tele/wide) × start/stop/onepush × speed 1–7
- **Record Start/Stop**
- **Record Toggle** (queries state first)
- **Get Available API List** — logs what the camera exposes

## Feedbacks
- **Recording state** — button lights red while recording (via getEvent poll)

## Variables
- `zoom_position`, `recording`, `last_error`

## Presets
(zipped install: copy this folder into Companion's modules dir or build a
companion-module bundle; tested against Companion 3.x with @companion-module/base)
