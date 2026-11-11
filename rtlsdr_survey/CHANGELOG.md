# Changelog

## 0.3.1
- Initial round-the-clock survey is now opt-in (`initial_survey`, default off).
- Auto mode adds short random daytime survey slots (`daytime_survey_min_per_hour`, default 5),
  replacing `monitor_hourly_survey_min`.
- `monitor` mode is now strictly monitoring-only: no random or fallback surveying; it idles if
  nothing is approved.

## 0.3.0
- New `auto` mode (default): survey during `survey_schedule` (default 01:00-05:00 local) and
  continuously for the first `initial_survey_days`; monitor the rest of the time.
- Smarter monitor mode: the dongle parks on a "home" frequency (the one with the most
  event-driven devices such as door sensors and remotes) and only visits other frequencies when
  their devices haven't reported for `monitor_refresh_min`. Visits end as soon as every device
  there has reported, capped at `monitor_dwell_s`.
- Stale-device detection: timer-reporting devices get HA `expire_after` (`stale_after_min`),
  so they show unavailable when they stop reporting.
- Slots are cut short so radiosonde windows and schedule changes start on time.
- Time zone taken from Home Assistant when TZ isn't set.

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
