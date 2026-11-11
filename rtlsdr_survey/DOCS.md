# RTL-SDR Survey (Home Assistant add-on / Docker)

One RTL-SDR dongle, time-sliced between three tools:

| Tool | What it does here |
|---|---|
| **rtl_433** | Decodes ISM/Part 15 sensors: weather, temp/humidity, TPMS, door/window, remotes, 915 MHz sensors |
| **rtl_power** | Peak-hold energy sweeps of the unlicensed bands, to find transmitters nothing decoded |
| **radiosonde_auto_rx** | Tracks NWS weather balloons (400–406 MHz) during the twice-daily launch windows |

**Survey mode** cycles through every slot in `survey_plan.yaml` indefinitely, for days if you like. It builds an inventory of every device it hears: what type it is, which slot heard it, how often, SNR, and the latest raw and parsed output. Nothing goes to Home Assistant until you tick **In HA** for a device. Then it publishes MQTT discovery, and the device shows up with proper sensor entities.

**Monitor mode** only visits the slots where your approved devices live. With one slot, the dongle stays there.
`monitor_hourly_survey_min` can keep a few minutes per hour for continued surveying.

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

The plan file is written to the add-on's config folder (`/addon_configs/<repo-id>_rtlsdr_survey/survey_plan.yaml`) on first start. Edit it there and restart.

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

## Options

| Option | Default | Notes |
|---|---|---|
| `mode` | survey | `survey` or `monitor`. The UI button overrides it until you change this option. |
| `device` | "0" | Dongle index or serial number (set unique serials with `rtl_eeprom -s`) |
| `gain` | auto | or dB, e.g. `40.2` |
| `ppm` | 0 | Frequency correction |
| `decode_dwell_s` | 300 | Seconds per decode slot (× the slot's `weight`) |
| `sweep_dwell_s` | 45 | Seconds per rtl_power band |
| `sweep_integration_s` | 5 | rtl_power integration interval (peak hold is on) |
| `peak_threshold_db` | 10 | dB above the per-hop median to count as a signal |
| `publish_raw` | true | Publish every decode (approved or not) to `rtlsdr/survey/<uid>` |
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
| `rtlsdr/scanner` | Current slot (retained) |
| `rtlsdr/survey/<uid>` | Every decoded event while `publish_raw` is on |
| `rtlsdr/devices/<uid>/state` | Flattened state of **approved** devices (retained) |
| `rtlsdr/radiosonde/state`, `.../position` | Latest sonde telemetry and GPS position (retained) |
| `homeassistant/<component>/rtlsdr_<uid>/<field>/config` | Discovery for approved devices. Un-ticking a device removes it. |

## Limits

- **Sonde windows pause the survey** for about 5½ h a day. Shorten `radiosonde_windows_utc`, or disable it, if you want more survey time.
- **One dongle hears about 2.4 MHz at a time.** Anything transmitting while the dongle is elsewhere is missed. Monitor mode with a single slot avoids that. A second dongle is the real fix (e.g. one parked on 433.92, one on 915).
- **Device IDs.** Many cheap sensors pick a new random ID after a battery change, so it shows up as a new device. Approve the new one and forget the old one.
- **Undecoded signals** are energy only. The tool can tell you *something* bursts at 418.000 MHz 3% of the time, but not what it is. Use `rtl_433 -A` or Universal Radio Hacker on that frequency to dig in.
