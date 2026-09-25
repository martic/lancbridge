# CBM APK reverse-engineering — findings (2026-09-25)

## What CBM 3.7.1 speaks
- **PTP/IP + Sony vendor extensions** (`PtpDataWrapper` class, commands like
  `setFocusPositionSetting`, `setIrisPositionSetting`, `setAfTransitionSpeed`,
  `setAFAssist`, `setFocusArea`) plus a `Camera.*` capability-key JSON layer
  (`Camera.Zoom.Value`, `Camera.Iris.FValue`, `Camera.Focus.Velocity`, etc.)
  over `com.sony.linear` sockets (WSS/SSL/TCP).
- This is the **same protocol family as Monitor & Control** — for 2023+ cameras
  (FX6 fw5+, PXW-Z200...). **Not the MC2500's protocol** (MC2500 is 2014, fw ≤4.x,
  speaks the old CBM v1 protocol).

## Version availability
- APKMirror/APKPure/APKCombo: oldest CBM = 3.1.0 (~2020). MC2500-era v1/v2 builds
  are not archived. apk.cafe has an ancient build (file_id 50388) behind a
  JS timer — could be retried with a headless browser.
- CBM 3.5.1 minSdk = Android 6.0; 3.7.1 minSdk = Android 9. Both sideloadable on
  modern Android; "too old to install" errors usually mean an APK with
  targetSdk < 23 (v1.x era) — blocked by Android itself.

## Next steps if we continue
1. Get old CBM v1.x APK (apk.cafe file_id 50388, or ask on forums for the
   2014-era build) → decompile → the legacy protocol is XML-over-TCP-ish
   ("Remocon" era).
2. OR capture the Wi-Fi traffic while a working CBM/phone controls the camera
   (only possible if we find an app that actually talks to the MC2500).
3. OR stick with LANC: hardware proven working; needs the correct command
   dialect. A cheap compatible LANC remote (AODELAN) as reference capture would
   settle it definitively.

## Artifacts
- cbm.apk, apk/ (unzipped), dex_strings.txt, comapi_strings.txt
