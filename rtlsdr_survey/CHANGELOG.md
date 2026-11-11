# Changelog

## 0.6.0
- New **Frequencies** tab: a checklist of every frequency/band the bundled tools support,
  grouped by tool, plus a box for custom frequencies (`<freq> <rtl_433|aprs|noaa>` or
  `<low>:<high>[:<step>] sweep`). Applies immediately. Replaces `survey_plan.yaml`, which is
  no longer read.
- New optional tools, each with an enable switch:
  - Direwolf APRS (stations and weather stations as approvable devices)
  - rtl_fm + multimon-ng NOAA weather radio SAME alerts ("NOAA weather alerts" HA device)
  - readsb ADS-B ("ADS-B receiver" HA device, aircraft on the Devices tab)
  - rtl_ais marine AIS ("AIS receiver" HA device, ships on the Devices tab)
- New catalog entries: 418 MHz and 868.95 MHz for rtl_433.
- **Multiple dongles:** Settings → Receivers lists any number of SDRs (by index or serial, with
  per-dongle gain/ppm and a **Detect dongles** button). *Rotating* dongles share the scan, split
  by listening time; *dedicated* dongles run only the frequencies assigned to them (one = parked
  full time). Each frequency on the Frequencies tab has a dongle picker. Radiosondes can get their
  own dongle. With no receivers listed, the single `device` option works as before.

## 0.5.1
- Fix Settings "Could not save: Extra data: line 1 column 4": the add-on called the Supervisor's
  options/validate endpoint, which add-ons aren't allowed to use (plain-text 403). Validation is
  done by the add-on itself; Supervisor errors are now shown readably.

## 0.5.0
- Build rtl_433 from its development branch instead of the 25.12 release: ~100 more decoders
  (392 vs 291), including many US car remotes, TPMS sensors, garage openers (Chamberlain),
  2GIG/Vivint security, Kidde smoke alarms, Govee sensors and Silver Spring mesh meters.
- New `survey_all_decoders` option (Settings → Survey): survey slots also enable rtl_433's
  ~50 off-by-default decoders. Monitoring always uses the default set.

## 0.4.0
- New **Settings** tab in the web UI with every option, grouped, with validation.
  Saving writes back to the add-on configuration (Supervisor API), so the HA Configuration tab
  stays in sync. Most settings apply immediately; MQTT/topic/log-level changes restart the
  add-on automatically. Standalone Docker: saved to /data/settings_override.json.
- The header mode selector now also saves to the add-on configuration.

## 0.3.2
- UI: right-align numeric column headers so values line up under the right heading
  (Coverage table looked like decoded frames were errors).

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
