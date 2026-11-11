"""Settings tab backend: option metadata, validation, saving to the add-on config, live apply."""
import json
import logging
import os
import re

import aiohttp

from config import DEFAULTS, data_dir

log = logging.getLogger("settings")

SUPERVISOR = os.environ.get("SUPERVISOR_URL", "http://supervisor")
OVERRIDE_FILE = "settings_override.json"   # standalone (no Supervisor) persistence

# key: (group, label, type, extra, help, live)
#   type: choice | bool | int | float | str | password | windows
#   extra: choices list for choice, (min, max) for int/float
#   live: True = applied immediately; False = needs a restart
SPEC = [
    ("Scheduling", "mode", "Mode", "choice", ["auto", "survey", "monitor"],
     "auto: survey on schedule + random daytime slots, monitor otherwise. survey: always survey. "
     "monitor: monitoring only, never surveys.", True),
    ("Scheduling", "survey_schedule", "Survey schedule (local time)", "windows", None,
     "Auto mode surveys inside these windows, e.g. 01:00-05:00. Comma-separate several.", True),
    ("Scheduling", "daytime_survey_min_per_hour", "Daytime survey (min per hour)", "int", (0, 30),
     "Auto mode: minutes per monitored hour spent surveying a random frequency. 0 = off.", True),
    ("Scheduling", "initial_survey", "Initial round-the-clock survey", "bool", None,
     "Auto mode: survey continuously for the first days after first start.", True),
    ("Scheduling", "initial_survey_days", "Initial survey length (days)", "int", (1, 60), "", True),

    ("Monitor", "monitor_dwell_s", "Monitor visit length (s)", "int", (20, 1800),
     "Longest visit to a non-home frequency; ends early once its devices report.", True),
    ("Monitor", "monitor_refresh_min", "Monitor refresh (min)", "int", (1, 240),
     "Revisit a frequency when its devices haven't been heard for this long.", True),
    ("Monitor", "stale_after_min", "Mark stale after (min)", "int", (0, 1440),
     "Timer-reporting devices go unavailable in HA after this long without a report. 0 = off.", True),

    ("Survey", "decode_dwell_s", "Decode dwell (s)", "int", (30, 7200),
     "Seconds per rtl_433 frequency during a survey (× the slot's weight).", True),
    ("Survey", "sweep_dwell_s", "Sweep dwell (s)", "int", (10, 1800),
     "Seconds per rtl_power band.", True),
    ("Survey", "sweep_integration_s", "Sweep integration (s)", "int", (1, 60), "", True),
    ("Survey", "peak_threshold_db", "Peak threshold (dB)", "float", (3, 40),
     "dB above the noise floor for a sweep bin to count as a signal.", True),

    ("Radio", "device", "Dongle", "str", None, "Device index (0) or serial number.", True),
    ("Radio", "gain", "Gain", "str", None, "auto, or tuner gain in dB (e.g. 40.2).", True),
    ("Radio", "ppm", "Frequency correction (ppm)", "int", (-200, 200), "", True),
    ("Radio", "rtl_433_extra_args", "Extra rtl_433 arguments", "str", None,
     "e.g. -R -59 to disable a noisy decoder.", True),
    ("Survey", "survey_all_decoders", "Enable all decoders while surveying", "bool", None,
     "Also turn on rtl_433's ~50 off-by-default decoders during survey slots (not monitoring). "
     "Finds more devices, but those decoders are off by default because they produce more "
     "false matches.", True),

    ("Radiosondes", "radiosonde_enabled", "Track radiosondes", "bool", None,
     "Hand the dongle to radiosonde_auto_rx during launch windows.", True),
    ("Radiosondes", "radiosonde_windows_utc", "Launch windows (UTC)", "windows", None,
     "Comma-separated HH:MM-HH:MM in UTC.", True),
    ("Radiosondes", "radiosonde_min_mhz", "Search start (MHz)", "float", (400, 406), "", True),
    ("Radiosondes", "radiosonde_max_mhz", "Search end (MHz)", "float", (400, 406), "", True),
    ("Radiosondes", "radiosonde_publish_ha", "Publish Radiosonde tracker to HA", "bool", None, "", True),
    ("Radiosondes", "sondehub_callsign", "SondeHub callsign", "str", None,
     "Set to upload reception to sondehub.org. Empty = off.", True),
    ("Radiosondes", "sondehub_email", "SondeHub contact email", "str", None, "", True),
    ("Radiosondes", "station_lat", "Station latitude", "float", (-90, 90),
     "Empty = Home Assistant's home location.", True),
    ("Radiosondes", "station_lon", "Station longitude", "float", (-180, 180), "", True),
    ("Radiosondes", "station_alt", "Station altitude (m)", "float", (-500, 9000), "", True),

    ("MQTT", "mqtt_host", "Broker host", "str", None, "Empty = Mosquitto add-on (automatic).", False),
    ("MQTT", "mqtt_port", "Broker port", "int", (1, 65535), "", False),
    ("MQTT", "mqtt_user", "User", "str", None, "", False),
    ("MQTT", "mqtt_password", "Password", "password", None, "", False),
    ("MQTT", "base_topic", "Base topic", "str", None, "", False),
    ("MQTT", "discovery_prefix", "HA discovery prefix", "str", None, "", False),
    ("MQTT", "publish_raw", "Publish raw survey events", "bool", None,
     "Every decode (approved or not) to <base>/survey/<uid>.", True),

    ("Advanced", "log_level", "Log level", "choice", ["debug", "info", "warning", "error"], "", False),
]
SPEC_BY_KEY = {s[1]: s for s in SPEC}
# Optional in the add-on schema: an empty value removes the key instead of failing validation.
OPTIONAL = {"survey_schedule", "mqtt_host", "mqtt_port", "mqtt_user", "mqtt_password",
            "rtl_433_extra_args", "sondehub_callsign", "sondehub_email",
            "station_lat", "station_lon", "station_alt"}
WIN_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*$")


def has_supervisor():
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def describe(opts):
    """Metadata + current values for the UI. Values come from what the user configured;
    auto-derived values (MQTT from the Mosquitto add-on, location from HA) are shown as hints."""
    user = opts.get("_user") or {}
    out = []
    for group, key, label, typ, extra, help_, live in SPEC:
        default = DEFAULTS.get(key)
        if key in OPTIONAL and default in (0, 0.0, "", None):
            default = ""          # unset optional -> empty field (auto/disabled)
        val = user.get(key, default)
        hint = None
        if key not in user and opts.get(key) not in (None, "", 0, 0.0) and opts.get(key) != val:
            hint = f"currently using {opts[key]} (automatic)" if typ != "password" else "automatic"
        if typ == "password":
            val = "********" if user.get(key) else ""
        out.append({"group": group, "key": key, "label": label, "type": typ,
                    "choices": extra if typ == "choice" else None,
                    "min": extra[0] if typ in ("int", "float") and extra else None,
                    "max": extra[1] if typ in ("int", "float") and extra else None,
                    "help": help_, "live": live, "value": val, "hint": hint})
    return {"settings": out, "backend": "supervisor" if has_supervisor() else "file"}


def validate(changes):
    """Coerce UI values to the right types. Returns (clean, errors). None = remove key."""
    clean, errors = {}, {}
    for key, raw in changes.items():
        spec = SPEC_BY_KEY.get(key)
        if not spec:
            errors[key] = "unknown option"
            continue
        _, _, label, typ, extra, _, _ = spec
        try:
            if typ == "password":
                if raw == "********":
                    continue          # unchanged
                clean[key] = None if raw in ("", None) else str(raw)
                continue
            if raw in ("", None):
                if key in OPTIONAL:
                    clean[key] = None
                    continue
                raise ValueError("required")
            if typ == "bool":
                v = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "on", "yes")
            elif typ == "int":
                v = int(float(raw))
            elif typ == "float":
                v = float(raw)
            elif typ == "choice":
                v = str(raw)
                if v not in extra:
                    raise ValueError(f"must be one of {', '.join(extra)}")
            elif typ == "windows":
                v = ",".join(p.strip() for p in str(raw).split(",") if p.strip())
                for part in v.split(","):
                    if not WIN_RE.match(part):
                        raise ValueError(f"'{part}' is not HH:MM-HH:MM")
                    for hm in part.split("-"):
                        h, m = map(int, hm.strip().split(":"))
                        if h > 23 or m > 59:
                            raise ValueError(f"bad time '{hm.strip()}'")
            else:
                v = str(raw).strip()
            if typ in ("int", "float") and extra and not (extra[0] <= v <= extra[1]):
                raise ValueError(f"must be between {extra[0]} and {extra[1]}")
            clean[key] = v
        except (TypeError, ValueError) as e:
            errors[key] = f"{label}: {e}"
    lo = clean.get("radiosonde_min_mhz")
    hi = clean.get("radiosonde_max_mhz")
    if lo is not None and hi is not None and lo >= hi:
        errors["radiosonde_max_mhz"] = "Search end must be above search start"
    return clean, errors


async def _sv(session, method, path, body=None):
    headers = {"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}
    async with session.request(method, SUPERVISOR + path, json=body, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json(content_type=None)
        if r.status >= 400 or (data or {}).get("result") == "error":
            raise RuntimeError((data or {}).get("message") or f"HTTP {r.status}")
        return (data or {}).get("data")


async def save(clean):
    """Persist to the add-on configuration (Supervisor) or the standalone override file.
    Returns the resulting user options dict."""
    if has_supervisor():
        async with aiohttp.ClientSession() as s:
            info = await _sv(s, "GET", "/addons/self/info")
            options = dict(info.get("options") or {})
            for k, v in clean.items():
                if v is None:
                    options.pop(k, None)
                else:
                    options[k] = v
            options.pop("monitor_hourly_survey_min", None)
            await _sv(s, "POST", "/addons/self/options/validate", {"options": options})
            await _sv(s, "POST", "/addons/self/options", {"options": options})
            return options
    f = data_dir() / OVERRIDE_FILE
    cur = json.loads(f.read_text()) if f.exists() else {}
    for k, v in clean.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    f.write_text(json.dumps(cur, indent=2))
    return cur


async def restart():
    if has_supervisor():
        async with aiohttp.ClientSession() as s:
            await _sv(s, "POST", "/addons/self/restart")
    else:
        # standalone: exit; Docker's restart policy brings us back with the new settings
        os._exit(0)
