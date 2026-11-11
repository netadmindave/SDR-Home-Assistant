"""Options loading: HA add-on (/data/options.json + Supervisor MQTT service) or env vars."""
import json
import logging
import os
import shutil
import time
import urllib.request
from pathlib import Path

import yaml

log = logging.getLogger("config")

DEFAULTS = {
    "mode": "auto",              # survey | monitor | auto
    "survey_schedule": "01:00-05:00",   # local time; auto mode surveys inside, monitors outside
    "initial_survey": False,            # auto mode: survey around the clock at first...
    "initial_survey_days": 7,           # ...for this many days
    "daytime_survey_min_per_hour": 5,   # auto mode: random survey minutes each monitored hour
    "monitor_dwell_s": 90,              # max visit to a non-home slot in monitor mode
    "monitor_refresh_min": 10,          # revisit a slot when its devices are this stale
    "stale_after_min": 120,             # HA expire_after for periodic devices (0 = off)
    "device": "0",               # dongle index or serial
    "gain": "auto",              # "auto" or dB (e.g. 40.2)
    "ppm": 0,
    "decode_dwell_s": 300,
    "sweep_dwell_s": 45,
    "sweep_integration_s": 5,
    "peak_threshold_db": 10.0,
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_password": "",
    "base_topic": "rtlsdr",
    "discovery_prefix": "homeassistant",
    "publish_raw": True,
    "rtl_433_extra_args": "",
    "survey_all_decoders": False,       # survey slots also enable rtl_433's off-by-default decoders
    # frequency checklist (Frequencies tab); None = catalog defaults
    "frequencies": None,
    "custom_frequencies": [],
    # receivers: [] = the single `device` above; else [{name, device, role, gain?, ppm?}, ...]
    "sdrs": [],
    "assignments": [],                  # "<frequency id>=<sdr name>" (Frequencies tab)
    # optional tools
    "enable_aprs": False,
    "enable_noaa": False,
    "enable_adsb": False,
    "enable_ais": False,
    "log_level": "info",
    "web_port": 8099,
    # radiosonde_auto_rx
    "radiosonde_enabled": True,
    "radiosonde_windows_utc": "10:45-13:30,22:45-01:30",
    "radiosonde_min_mhz": 400.05,
    "radiosonde_max_mhz": 405.95,
    "radiosonde_publish_ha": True,
    "radiosonde_web_port": 5000,
    "station_lat": 0.0,
    "station_lon": 0.0,
    "station_alt": 0.0,
    "sondehub_callsign": "",
    "sondehub_email": "",
}

ENV_PREFIX = "RTLSDR_"


def _coerce(default, value):
    if isinstance(default, list) or default is None:
        try:
            return json.loads(value)
        except ValueError:
            return [v.strip() for v in str(value).split(";") if v.strip()]
    if isinstance(default, bool):
        return str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    return str(value)


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def config_dir() -> Path:
    """Where the user-editable plan lives. /config is the add-on's addon_config map."""
    p = Path(os.environ.get("CONFIG_DIR", "/config"))
    return p if p.is_dir() else data_dir()


def _supervisor_mqtt():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r).get("data") or {}
        if data.get("host"):
            log.info("Using MQTT broker from Supervisor: %s:%s", data["host"], data.get("port"))
            return {
                "mqtt_host": data["host"],
                "mqtt_port": int(data.get("port", 1883)),
                "mqtt_user": data.get("username", ""),
                "mqtt_password": data.get("password", ""),
            }
    except Exception as e:  # noqa: BLE001
        log.warning("Supervisor MQTT lookup failed: %s", e)
    return None


def _supervisor_location():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "http://supervisor/core/api/config",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r)
        if data.get("latitude") is not None:
            return {"station_lat": float(data["latitude"]), "station_lon": float(data["longitude"]),
                    "station_alt": float(data.get("elevation") or 0),
                    "_time_zone": data.get("time_zone")}
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read Home Assistant location: %s", e)
    return None


def load_options() -> dict:
    opts = dict(DEFAULTS)
    user = {}   # what the user explicitly configured (shown/edited in the Settings tab)
    f = data_dir() / "options.json"
    if f.exists():
        try:
            user.update({k: v for k, v in json.loads(f.read_text()).items() if v is not None})
        except Exception as e:  # noqa: BLE001
            log.error("Bad options.json: %s", e)
    for k, d in DEFAULTS.items():
        ev = os.environ.get(ENV_PREFIX + k.upper())
        if ev is not None:
            user[k] = _coerce(d, ev)
    # standalone: settings saved from the web UI win over env vars
    ov = data_dir() / "settings_override.json"
    if ov.exists() and not os.environ.get("SUPERVISOR_TOKEN"):
        try:
            user.update(json.loads(ov.read_text()))
        except Exception as e:  # noqa: BLE001
            log.error("Bad %s: %s", ov, e)
    user.pop("monitor_hourly_survey_min", None)   # replaced by daytime_survey_min_per_hour
    opts.update(user)
    opts["_user"] = user
    if not opts["mqtt_host"]:
        sup = _supervisor_mqtt()
        if sup:
            opts.update(sup)
    loc = _supervisor_location()
    if loc:
        tz = loc.pop("_time_zone", None)
        if tz and not os.environ.get("TZ"):
            os.environ["TZ"] = tz
            time.tzset()
            log.info("Time zone taken from Home Assistant: %s", tz)
        if not opts["station_lat"] and not opts["station_lon"]:
            opts.update(loc)
            log.info("Station location taken from Home Assistant")
    return opts


# ---------------------------------------------------------------- plan

def parse_freq(v) -> int:
    """'433.92M' / '250k' / 912600155 -> Hz (int)."""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().upper()
    mult = 1
    if s.endswith("G"):
        mult, s = 1_000_000_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("K"):
        mult, s = 1_000, s[:-1]
    return int(round(float(s) * mult))


def load_plan(opts: dict) -> dict:
    """Survey slots now come from the Frequencies tab (catalog.py)."""
    from catalog import build_slots
    plan = build_slots(opts)
    log.info("Plan: %d slots from the frequency checklist", len(plan["slots"]))
    return plan
