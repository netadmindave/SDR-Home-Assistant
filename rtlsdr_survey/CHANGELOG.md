# Changelog

## 0.2.2
- Fix devices that alternate message types (e.g. LaCrosse-TX141W sends temp/humidity and wind
  in separate packets): state is now merged, so fields missing from one packet keep their last
  value, and HA entities are added when a new field first appears after approval.
- Drop rtl_433's "test" flag as an HA entity (and remove it if already created).

## 0.2.1
- Fix build on Home Assistant: runtime image is now pinned to debian:bookworm-slim instead of
  Supervisor's default Alpine base ("apt-get: not found").

## 0.2.0
- Added radiosonde_auto_rx: weather-balloon tracking during configurable UTC launch windows,
  a "Radiosonde tracker" HA device with GPS device_tracker, optional SondeHub upload.
- Removed rtlamr (most utilities now use mesh AMI meters it can't decode).

## 0.1.0
- First release: time-sliced rtl_433 + rtl_power survey, device approval → MQTT discovery,
  survey/monitor modes, web UI via ingress.
