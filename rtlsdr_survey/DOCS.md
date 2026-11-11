# RTL-SDR Survey (Home Assistant add-on / Docker)

One RTL-SDR dongle, time-sliced between these tools (the optional ones are off until you enable them on the **Frequencies** tab):

- **rtl_433** (development branch, ~390 decoders) decodes 300–928 MHz sensors: weather, temp/humidity, TPMS, door/window, remotes.
- **rtl_power** runs peak-hold sweeps of the unlicensed bands to find transmitters nothing decoded.
- **radiosonde_auto_rx** tracks NWS weather balloons during the twice-daily launch windows.
- *Optional:* **Direwolf** (APRS), **rtl_fm + multimon-ng** (NOAA weather radio SAME alerts), **readsb** (ADS-B aircraft), **rtl_ais** (marine AIS).

**Survey mode** cycles through every frequency ticked on the **Frequencies** tab indefinitely, for days if you like. It builds an inventory of every device it hears: what type it is, which slot heard it, how often, SNR, and the latest raw and parsed output. Nothing goes to Home Assistant until you tick **In HA** for a device. Then it publishes MQTT discovery, and the device shows up with proper sensor entities.

**Monitor mode** watches only the devices you approved. The dongle parks on a **home frequency**: the one with the most event-driven devices (door/window sensors, motion, remotes, doorbells), since those only transmit when something happens. It leaves home only when devices on another frequency haven't reported for `monitor_refresh_min`. That visit ends as soon as they have all reported, or after `monitor_dwell_s` at most.

**Auto mode** (default) surveys inside `survey_schedule` (default 01:00–05:00 local) and monitors the rest of the day. While it monitors, it spends `daytime_survey_min_per_hour` (5) minutes each hour surveying one randomly chosen frequency, at a random time. That catches devices that are only active in the daytime, such as garage remotes and car TPMS. New devices join the Devices list as they're heard; you choose which to approve. Auto keeps surveying until at least one device is approved. Optionally, `initial_survey: true` surveys around the clock for the first `initial_survey_days` to build the list faster.

**Monitor mode** is monitoring only: no scheduled, random or fallback surveying. With nothing approved, it idles.

**Stale devices:** timer-reporting devices (temperature, weather, TPMS...) get HA's `expire_after`, so their entities go *unavailable* after `stale_after_min` (120) without a report. Event devices are exempt, because silence is normal for them.

Event-driven devices on a frequency other than home are only heard while the dongle visits it. If you have door sensors on two different frequencies, a second dongle is the real fix.

## Several dongles

With one dongle you don't need to configure anything. For two or more, open **Settings → Receivers (SDRs)** and add a row per dongle. **Detect dongles** lists what's plugged in. Each row has:

- **Name:** used in the UI, the log and the MQTT topic `rtlsdr/scanner/<name>`.
- **Device:** index (`0`, `1`…) or, better, serial number. Set unique serials with `rtl_eeprom -d <index> -s <serial>` so dongles keep their jobs after a reboot or replug.
- **Role:**
  - **rotating:** shares the scan with the other rotating dongles. Survey, monitor and auto modes apply. Frequencies nobody else claims are split between rotating dongles, balanced by listening time.
  - **dedicated:** runs only the frequencies assigned to it, ignoring modes. One frequency means it stays parked there full time; several means it rotates among just those.
- **Gain / PPM:** optional per-dongle overrides.

Then on the **Frequencies** tab each frequency (and each custom line) has a dongle dropdown. Leave it on *any rotating* to share it, or pick a dongle. Radiosondes run during launch windows on the first rotating dongle, unless you assign them to one. Assigned to a dedicated dongle, auto_rx runs full time and the other dongles never pause.

Example with three dongles:

| Dongle | Role | Assigned | Result |
|---|---|---|---|
| `main` | rotating | (nothing) | Surveys/monitors everything else and handles the radiosonde windows |
| `door` | dedicated | 433.92 MHz | Door sensors and remotes heard instantly, never missed |
| `wx` | dedicated | NOAA 162.475 | Every weather alert, 24/7 |

Changing the receiver list restarts the add-on. Changing assignments applies immediately.

## Frequencies tab

A checklist of every frequency and band the bundled tools support, grouped by tool. Each optional tool has an **enabled** switch, and its frequencies are skipped while it's off. Under the list is a box for your own frequencies, one per line:

```
418.5M rtl_433 Gate remote
162.475M noaa
144.39M aprs
418M:420M:10k sweep
```

Saving applies immediately, with no restart. The selection is stored in the `frequencies` / `custom_frequencies` options.

What each optional tool adds:

| Tool | Frequencies | In Home Assistant |
|---|---|---|
| **Direwolf APRS** | 144.390 (NA), 144.800 (EU), 145.825 (ISS) | Each station is a device you can approve; weather stations give temperature, humidity, wind, rain and pressure |
| **NOAA SAME alerts** | 162.400–162.550 (pick your local transmitter) | A fixed **NOAA weather alerts** device: *Alert active*, last alert, event code, areas, issued/expires. Alerts only arrive while the dongle is on that channel, so use a second dongle if you rely on it. |
| **readsb ADS-B** | 1090 MHz | A fixed **ADS-B receiver** device: aircraft seen, with position, max range, nearest. Aircraft are listed on the Devices tab. |
| **rtl_ais AIS** | 161.975 + 162.025 MHz | A fixed **AIS receiver** device: ships seen, max range, nearest |

## Radiosondes

The NWS launches weather balloons twice a day, about an hour before 00Z and 12Z. Each flight lasts about 2 hours. During the windows in `radiosonde_windows_utc` (default `10:45-13:30,22:45-01:30` UTC), the add-on hands the dongle to radiosonde_auto_rx. That works in survey **and** monitor mode. auto_rx scans 400–406 MHz itself, decodes the sonde, and sends telemetry back here.

- **In HA**, a fixed **Radiosonde tracker** device appears automatically, with no approval needed because every flight has a new serial. It has altitude, temperature, humidity, pressure, vertical/horizontal speed, heading, distance and bearing from your station, plus a GPS `device_tracker` for the map card.
- **Station location** comes from Home Assistant's home location (add-on), or set `station_lat/lon`.
- **Track sondes now** in the header runs auto_rx for 2 hours immediately, which is handy for testing.
- auto_rx's own map is on port 5000 while it's running. In the add-on, enable the port under Network.
- **SondeHub upload** is off by default. Set `sondehub_callsign` (and optionally `sondehub_email`) to contribute your reception to sondehub.org.
- A sonde at altitude is line-of-sight from a long way off. Even with a poor antenna, expect to hear launches from sites a couple of hundred km away near the top of the flight.

## How long a survey takes

With the default plan, one full cycle is about **73 minutes**. 433.92 MHz gets about 21% of the time, and 315 MHz and 915 MHz about 14% each. A device that transmits every 30–60 s shows up on the first pass through its slot. One that transmits rarely can take hours to days. Leave survey mode running a few days before judging what's around you. The **Coverage** tab shows how long each slot has listened in total, plus rtl_433's *frames seen vs decoded* ratio. A large gap means there are transmitters on that frequency that rtl_433 doesn't understand.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
   `https://github.com/netadmindave/SDR-Home-Assistant`
2. Install **RTL-SDR Survey**. The first build compiles librtlsdr, rtl_433 and radiosonde_auto_rx's decoders, which takes about 5–10 min on x86 and longer on a Pi.
3. If the Mosquitto add-on is installed, the MQTT broker is picked up automatically. Otherwise set `mqtt_host` etc.
4. Start it, then open **SDR Survey** in the sidebar.

Stop the rtl_433 and rtlamr2mqtt add-ons first. Only one process can own the dongle.


## Run as a plain container

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/rtlsdr-blacklist.conf
sudo modprobe -r dvb_usb_rtl28xxu
git clone https://github.com/netadmindave/SDR-Home-Assistant.git && cd SDR-Home-Assistant
# edit RTLSDR_MQTT_HOST in docker-compose.yml, then:
docker compose up -d --build
# UI: http://<host>:8099   data + plan: ./data/
```

Every option can be set as an env var: `RTLSDR_<OPTION_NAME_UPPERCASE>`.

If HA runs as a Proxmox VM, pass the dongle (or its USB controller) through to the VM.

## Settings tab

Every option below is also in the web UI's **Settings** tab. Saving there writes to the add-on configuration through the Supervisor API, so it matches the Configuration tab in HA. Most settings apply immediately. Settings marked *restart* (MQTT broker, topics, log level) restart the add-on automatically. In standalone Docker, settings are saved to `/data/settings_override.json` and take priority over environment variables.

## Options

| Option | Default | Notes |
|---|---|---|
| `mode` | auto | `auto`, `survey` or `monitor`. The UI selector overrides it until you change this option. |
| `survey_schedule` | 01:00-05:00 | Auto mode: local-time survey windows (comma-separated, may wrap midnight) |
| `initial_survey` | false | Auto mode: survey around the clock at first (optional) |
| `initial_survey_days` | 7 | Length of that initial survey |
| `daytime_survey_min_per_hour` | 5 | Auto mode: random daytime survey minutes per hour (0 = off) |
| `monitor_dwell_s` | 90 | Longest visit to a non-home frequency in monitor mode |
| `monitor_refresh_min` | 10 | Revisit a frequency when its devices are this stale |
| `stale_after_min` | 120 | HA `expire_after` for timer-reporting devices (0 = off) |
| `device` | "0" | Dongle index or serial number (set unique serials with `rtl_eeprom -s`) |
| `gain` | auto | or dB, e.g. `40.2` |
| `ppm` | 0 | Frequency correction |
| `decode_dwell_s` | 300 | Seconds per decode slot (× the slot's `weight`) |
| `sweep_dwell_s` | 45 | Seconds per rtl_power band |
| `sweep_integration_s` | 5 | rtl_power integration interval (peak hold is on) |
| `peak_threshold_db` | 10 | dB above the per-hop median to count as a signal |
| `publish_raw` | true | Publish every decode (approved or not) to `rtlsdr/survey/<uid>` |
| `frequencies` / `custom_frequencies` | defaults | Edited on the Frequencies tab |
| `sdrs` | (empty = one dongle, `device`) | Receivers list: name, device, role (rotating/dedicated), optional gain/ppm. Edited on Settings → Receivers |
| `assignments` | (empty) | `<frequency id>=<receiver name>`. Edited on the Frequencies tab |
| `enable_aprs` / `enable_noaa` / `enable_adsb` / `enable_ais` | false | Optional tools |
| `survey_all_decoders` | false | Survey slots also enable rtl_433's ~50 off-by-default decoders |
| `rtl_433_extra_args` | | e.g. `-R -59` to disable a noisy decoder, or `-Y minmax` |
| `radiosonde_enabled` | true | Hand the dongle to auto_rx during launch windows |
| `radiosonde_windows_utc` | 10:45-13:30,22:45-01:30 | Comma-separated UTC windows, may wrap midnight |
| `radiosonde_min_mhz` / `max_mhz` | 400.05 / 405.95 | auto_rx search range |
| `radiosonde_publish_ha` | true | Publish the Radiosonde tracker device |
| `station_lat/lon/alt` | from HA | Used for distance/bearing and SondeHub |
| `sondehub_callsign` | (empty) | Set to enable SondeHub upload |

## MQTT topics

| Topic | Content |
|---|---|
| `rtlsdr/status` | `online` / `offline` (LWT, also used as entity availability) |
| `rtlsdr/scanner` (one dongle) or `rtlsdr/scanner/<name>` (several) | Current slot per dongle (retained) |
| `rtlsdr/survey/<uid>` | Every decoded event while `publish_raw` is on |
| `rtlsdr/devices/<uid>/state` | Flattened state of **approved** devices (retained) |
| `rtlsdr/radiosonde/state`, `.../position` | Latest sonde telemetry and GPS position (retained) |
| `rtlsdr/noaa/state`, `rtlsdr/adsb/state`, `rtlsdr/ais/state` | Fixed receiver devices for the optional tools (retained) |
| `homeassistant/<component>/rtlsdr_<uid>/<field>/config` | Discovery for approved devices. Un-ticking a device removes it. |

## Limits

- **Sonde windows pause the survey** for about 5½ h a day. Shorten `radiosonde_windows_utc`, or disable it, if you want more survey time.
- **Each dongle hears about 2.4 MHz at a time.** Anything transmitting while the dongle is elsewhere is missed. Monitor mode with a single slot avoids that. A second dongle is the real fix (e.g. one parked on 433.92, one on 915).
- **Device IDs.** Many cheap sensors pick a new random ID after a battery change, so it shows up as a new device. Approve the new one and forget the old one.
- **Undecoded signals** are energy only. The tool can tell you *something* bursts at 418.000 MHz 3% of the time, but not what it is. Use `rtl_433 -A` or Universal Radio Hacker on that frequency to dig in.
