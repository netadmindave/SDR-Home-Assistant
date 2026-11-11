# Changelog

## 0.2.0
- Added radiosonde_auto_rx: weather-balloon tracking during configurable UTC launch windows,
  a "Radiosonde tracker" HA device with GPS device_tracker, optional SondeHub upload.
- Removed rtlamr (most utilities now use mesh AMI meters it can't decode).

## 0.1.0
- First release: time-sliced rtl_433 + rtl_power survey, device approval → MQTT discovery,
  survey/monitor modes, web UI via ingress.
