"""Options loading: HA add-on (/data/options.json + Supervisor MQTT service) or env vars."""
import json
import logging
import os
import shutil
import urllib.request
from pathlib import Path

import yaml

log = logging.getLogger("config")

DEFAULTS = {
    "mode": "survey",            # survey | monitor
    "device": "0",               # dongle index or serial
    "gain": "auto",              # "auto" or dB (e.g. 40.2)
    "ppm": 0,
    "decode_dwell_s": 300,
    "sweep_dwell_s": 45,
    "sweep_integration_s": 5,
    "peak_threshold_db": 10.0,
    "monitor_hourly_survey_min": 0,  # in monitor mode, spend N min/hour surveying
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_password": "",
    "base_topic": "rtlsdr",
    "discovery_prefix": "homeassistant",
    "publish_raw": True,
    "rtl_433_extra_args": "",
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
                    "station_alt": float(data.get("elevation") or 0)}
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read Home Assistant location: %s", e)
    return None


def load_options() -> dict:
    opts = dict(DEFAULTS)
    f = data_dir() / "options.json"
    if f.exists():
        try:
            opts.update({k: v for k, v in json.loads(f.read_text()).items() if v is not None})
        except Exception as e:  # noqa: BLE001
            log.error("Bad options.json: %s", e)
    for k, d in DEFAULTS.items():
        ev = os.environ.get(ENV_PREFIX + k.upper())
        if ev is not None:
            opts[k] = _coerce(d, ev)
    if not opts["mqtt_host"]:
        sup = _supervisor_mqtt()
        if sup:
            opts.update(sup)
    if not opts["station_lat"] and not opts["station_lon"]:
        loc = _supervisor_location()
        if loc:
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
    target = config_dir() / "survey_plan.yaml"
    default = Path(__file__).with_name("default_plan.yaml")
    if not target.exists():
        try:
            shutil.copy(default, target)
            log.info("Wrote default survey plan to %s", target)
        except OSError as e:
            log.warning("Could not write %s: %s", target, e)
    src = target if target.exists() else default
    raw = yaml.safe_load(src.read_text()) or {}

    slots = []
    for d in raw.get("decode") or []:
        if not d.get("enabled", True):
            continue
        if d.get("decoder", "rtl_433") != "rtl_433":
            log.warning("Skipping slot %r: unsupported decoder %r", d.get("name"), d.get("decoder"))
            continue
        w = float(d.get("weight", 1))
        slots.append({
            "name": d["name"],
            "kind": "decode",
            "decoder": d.get("decoder", "rtl_433"),
            "freq": parse_freq(d["freq"]),
            "sample_rate": parse_freq(d.get("sample_rate", "250k")),
            "msgtype": d.get("msgtype", "all"),
            "dwell": int(d.get("dwell", opts["decode_dwell_s"] * w)),
        })
    for s in raw.get("sweep") or []:
        if not s.get("enabled", True):
            continue
        lo, hi, step = s["range"].split(":")
        slots.append({
            "name": s["name"],
            "kind": "sweep",
            "decoder": "rtl_power",
            "low": parse_freq(lo),
            "high": parse_freq(hi),
            "step": parse_freq(step),
            "range": s["range"],
            "dwell": int(s.get("dwell", opts["sweep_dwell_s"])),
        })
    log.info("Plan loaded from %s: %d slots", src, len(slots))
    return {"slots": slots, "source": str(src)}
