"""Turn decoder output into a device key, a device type, and a flat state dict."""
import hashlib
import re

TPMS_HINTS = ("tpms", "schrader", "toyota", "ford", "citroen", "renault", "hyundai", "elantra",
              "jansite", "abarth", "pmv", "truck", "kia", "porsche", "bmw", "gm-", "steelmate",
              "tyre", "tire", "huf", "ave-")
SECURITY_HINTS = ("security", "door", "window", "contact", "dsc", "interlogix", "2gig", "visonic",
                  "simplisafe", "ge-", "honeywell", "kerui", "ademco", "x10-security")
REMOTE_HINTS = ("remote", "keyfob", "key-fob", "button", "switch", "garage", "princeton", "ev1527",
                "pt2262", "nexa", "proove", "kaku", "intertechno", "somfy", "came", "nice-",
                "chamberlain", "secplus", "fan", "ceiling", "lightwave", "brennenstuhl")


EVENT_CATEGORIES = ("Security", "Motion", "Doorbell", "Remote", "Smoke", "Water leak")


def is_event_device(category) -> bool:
    """Devices that transmit on events (door opened, button pressed) rather than on a timer."""
    return any(c in (category or "") for c in EVENT_CATEGORIES)


def uid_for(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _has(d, *names):
    return any(n in d for n in names)


def classify_rtl433(ev: dict) -> str:
    model = str(ev.get("model", "")).lower()
    keys = set(ev)
    if "tpms" in model or (_has(keys, "pressure_kPa", "pressure_PSI", "pressure_bar")
                           and any(h in model for h in TPMS_HINTS)):
        return "Tire pressure sensor (TPMS)"
    if "ert" in model or "consumption" in keys or "consumption_data" in keys:
        return "Utility meter"
    if "smoke" in model or "co_" in model or "smoke" in keys:
        return "Smoke / CO alarm"
    if "motion" in model or "pir" in model or "motion" in keys:
        return "Motion sensor"
    if "doorbell" in model or "chime" in model or "bell" in model:
        return "Doorbell"
    if _has(keys, "contact_open", "reed_open") or any(h in model for h in SECURITY_HINTS):
        return "Security / door-window sensor"
    if "leak" in model or "water" in keys or "leak" in keys:
        return "Water leak sensor"
    if _has(keys, "strike_count", "storm_dist"):
        return "Lightning detector"
    if _has(keys, "wind_avg_km_h", "wind_avg_m_s", "wind_avg_mi_h", "wind_dir_deg", "wind_max_m_s"):
        return "Weather station"
    if _has(keys, "rain_mm", "rain_in", "rain_rate_mm_h", "rain_rate_in_h"):
        return "Rain gauge / weather station"
    if _has(keys, "power_W", "energy_kWh", "current_A", "power0_W", "power0"):
        return "Energy monitor"
    if _has(keys, "moisture"):
        return "Soil moisture sensor"
    if _has(keys, "depth_cm", "level", "tank"):
        return "Tank / level sensor"
    if _has(keys, "pressure_kPa", "pressure_PSI"):
        return "Pressure sensor (possibly TPMS)"
    t = _has(keys, "temperature_C", "temperature_F", "temperature_1_C")
    h = "humidity" in keys
    if t and h:
        return "Temperature / humidity sensor"
    if t:
        return "Temperature sensor"
    if _has(keys, "cmd", "button", "code", "data") or any(h in model for h in REMOTE_HINTS):
        return "Remote / keyfob / button"
    return "Other (decoded)"


def rtl433_identity(ev: dict):
    model = str(ev.get("model", "unknown"))
    dev_id = ev.get("id", ev.get("sensor_id", ev.get("code", "")))
    channel = ev.get("channel", "")
    parts = [model, str(dev_id)]
    if channel not in ("", None):
        parts.append(f"ch{channel}")
    key = "rtl_433:" + ":".join(parts)
    return key, model, str(dev_id), str(channel or "")


DROP_FIELDS = {"time", "mic", "mod", "protocol", "raw_msg"}


def rtl433_state(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in DROP_FIELDS and not isinstance(v, (list, dict))}


# ---------------------------------------------------------------- HA discovery field map

# field -> (component, device_class, unit, state_class, entity_category)
FIELD_MAP = {
    "temperature_C": ("sensor", "temperature", "°C", "measurement", None),
    "temperature_F": ("sensor", "temperature", "°F", "measurement", None),
    "temperature_1_C": ("sensor", "temperature", "°C", "measurement", None),
    "temperature_2_C": ("sensor", "temperature", "°C", "measurement", None),
    "humidity": ("sensor", "humidity", "%", "measurement", None),
    "moisture": ("sensor", "moisture", "%", "measurement", None),
    "pressure_kPa": ("sensor", "pressure", "kPa", "measurement", None),
    "pressure_PSI": ("sensor", "pressure", "psi", "measurement", None),
    "pressure_hPa": ("sensor", "atmospheric_pressure", "hPa", "measurement", None),
    "wind_avg_km_h": ("sensor", "wind_speed", "km/h", "measurement", None),
    "wind_max_km_h": ("sensor", "wind_speed", "km/h", "measurement", None),
    "wind_avg_m_s": ("sensor", "wind_speed", "m/s", "measurement", None),
    "wind_max_m_s": ("sensor", "wind_speed", "m/s", "measurement", None),
    "wind_avg_mi_h": ("sensor", "wind_speed", "mph", "measurement", None),
    "wind_max_mi_h": ("sensor", "wind_speed", "mph", "measurement", None),
    "wind_dir_deg": ("sensor", None, "°", "measurement", None),
    "rain_mm": ("sensor", "precipitation", "mm", "total_increasing", None),
    "rain_in": ("sensor", "precipitation", "in", "total_increasing", None),
    "rain_rate_mm_h": ("sensor", "precipitation_intensity", "mm/h", "measurement", None),
    "rain_rate_in_h": ("sensor", "precipitation_intensity", "in/h", "measurement", None),
    "light_lux": ("sensor", "illuminance", "lx", "measurement", None),
    "uv": ("sensor", None, "UV index", "measurement", None),
    "uvi": ("sensor", None, "UV index", "measurement", None),
    "power_W": ("sensor", "power", "W", "measurement", None),
    "energy_kWh": ("sensor", "energy", "kWh", "total_increasing", None),
    "current_A": ("sensor", "current", "A", "measurement", None),
    "voltage_V": ("sensor", "voltage", "V", "measurement", None),
    "battery_V": ("sensor", "voltage", "V", "measurement", "diagnostic"),
    "consumption": ("sensor", None, None, "total_increasing", None),
    "strike_count": ("sensor", None, None, "total_increasing", None),
    "storm_dist": ("sensor", None, None, "measurement", None),
    "rssi": ("sensor", "signal_strength", "dB", "measurement", "diagnostic"),
    "snr": ("sensor", None, "dB", "measurement", "diagnostic"),
    "battery_ok": ("binary_sensor", "battery", None, None, "diagnostic"),
    "contact_open": ("binary_sensor", "opening", None, None, None),
    "tamper": ("binary_sensor", "tamper", None, None, "diagnostic"),
    "alarm": ("binary_sensor", "safety", None, None, None),
    "motion": ("binary_sensor", "motion", None, None, None),
    "leak_now": ("sensor", None, None, None, None),
}
SKIP_ENTITY = {"test", "model", "id", "channel", "subtype", "freq", "freq1", "freq2", "noise", "protocol_id", "last_seen", "slot", "type", "event", "heartbeat", "reed_open"}


NICE_NAMES = {"rssi": "RSSI", "snr": "SNR", "battery_ok": "Battery", "contact_open": "Contact",
              "uv": "UV index", "uvi": "UV index", "wind_dir_deg": "Wind direction",
              "battery_V": "Battery voltage", "temperature_1_C": "Temperature 1",
              "temperature_2_C": "Temperature 2"}


def entity_name(field: str) -> str:
    if field in NICE_NAMES:
        return NICE_NAMES[field]
    n = re.sub(r"_(C|F|km_h|m_s|mi_h|mm|in|kPa|PSI|hPa|W|kWh|A|V|deg|lux|mm_h|in_h)$", "", field)
    return n.replace("_", " ").capitalize()
