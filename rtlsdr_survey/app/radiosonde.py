"""radiosonde_auto_rx integration: launch windows, station.cfg generation, UDP telemetry intake."""
import asyncio
import configparser
import datetime as dt
import json
import logging
import math
import os
import socket
from pathlib import Path

log = logging.getLogger("radiosonde")

AUTORX_DIR = Path(os.environ.get("AUTORX_DIR", "/opt/auto_rx"))
AUTORX_PY = os.environ.get("AUTORX_PYTHON", "/opt/autorx-venv/bin/python")
UDP_PORT = 55673


# ---------------------------------------------------------------- launch windows

def parse_windows(spec: str):
    """'10:45-13:30, 22:45-01:30' (UTC) -> [(start_min, end_min), ...]; windows may wrap midnight."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        h1, m1 = map(int, a.strip().split(":"))
        h2, m2 = map(int, b.strip().split(":"))
        out.append((h1 * 60 + m1, h2 * 60 + m2))
    return out


def window_end(windows, now=None):
    """If `now` (UTC datetime) is inside a window, return the window's end as a UTC datetime."""
    now = now or dt.datetime.now(dt.timezone.utc)
    mins = now.hour * 60 + now.minute
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for s, e in windows:
        if s <= e:
            if s <= mins < e:
                return midnight + dt.timedelta(minutes=e)
        else:  # wraps midnight
            if mins >= s:
                return midnight + dt.timedelta(days=1, minutes=e)
            if mins < e:
                return midnight + dt.timedelta(minutes=e)
    return None


def next_window_start(windows, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = []
    for s, _ in windows:
        for day in (0, 1):
            t = midnight + dt.timedelta(days=day, minutes=s)
            if t > now:
                starts.append(t)
    return min(starts) if starts else None


# ---------------------------------------------------------------- station.cfg

def write_station_cfg(opts, path: Path):
    example = AUTORX_DIR / "station.cfg.example"
    cp = configparser.RawConfigParser()
    cp.optionxform = str
    if example.exists():
        cp.read(example)

    def put(sec, key, val):
        if not cp.has_section(sec):
            cp.add_section(sec)
        cp.set(sec, key, str(val))

    dev = str(opts["device"]).strip()
    gain = str(opts["gain"]).strip().lower()
    put("sdr", "sdr_type", "RTLSDR")
    put("sdr", "sdr_quantity", 1)
    put("sdr_1", "device_idx", dev)
    put("sdr_1", "ppm", int(opts.get("ppm") or 0))
    put("sdr_1", "gain", -1 if gain in ("", "auto", "0") else gain)
    put("sdr_1", "bias", False)
    put("search_params", "min_freq", opts["radiosonde_min_mhz"])
    put("search_params", "max_freq", opts["radiosonde_max_mhz"])
    put("location", "station_lat", opts.get("station_lat") or 0.0)
    put("location", "station_lon", opts.get("station_lon") or 0.0)
    put("location", "station_alt", opts.get("station_alt") or 0.0)
    put("location", "gpsd_enabled", False)
    call = (opts.get("sondehub_callsign") or "").strip()
    put("habitat", "uploader_callsign", call or "SONDE_AUTO_RX")
    put("habitat", "upload_listener_position", bool(call))
    put("sondehub", "sondehub_enabled", bool(call))
    put("sondehub", "sondehub_contact_email", opts.get("sondehub_email") or "none@none.com")
    put("aprs", "aprs_enabled", False)
    put("email", "email_enabled", False)
    put("rotator", "rotator_enabled", False)
    put("oziplotter", "payload_summary_enabled", True)
    put("oziplotter", "payload_summary_host", "127.0.0.1")
    put("oziplotter", "payload_summary_port", UDP_PORT)
    put("web", "web_host", "0.0.0.0")
    put("web", "web_port", int(opts.get("radiosonde_web_port") or 5000))
    put("web", "web_control", False)
    put("logging", "per_sonde_log", True)
    put("logging", "save_system_log", False)
    put("advanced", "sdr_fm_path", "rtl_fm")
    put("advanced", "sdr_power_path", "rtl_power")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        cp.write(f)
    return path


# ---------------------------------------------------------------- telemetry

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _clean(v, invalid):
    try:
        return None if v is None or float(v) <= invalid else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def telemetry_state(pkt, opts):
    """PAYLOAD_SUMMARY packet -> flat state for HA."""
    st = {
        "serial": pkt.get("callsign"),
        "sonde_type": pkt.get("model"),
        "subtype": pkt.get("subtype"),
        "frequency_mhz": _clean(pkt.get("freq") and str(pkt["freq"]).split()[0], 0),
        "latitude": pkt.get("latitude"),
        "longitude": pkt.get("longitude"),
        "altitude_m": _clean(pkt.get("altitude"), -1e6),
        "temperature_C": _clean(pkt.get("temp"), -270),
        "humidity": _clean(pkt.get("humidity"), -0.5),
        "pressure_hPa": _clean(pkt.get("pressure"), 0),
        "vertical_speed_m_s": _clean(pkt.get("vel_v"), -1e6),
        "horizontal_speed_km_h": _clean(pkt.get("speed"), -0.5),
        "heading_deg": _clean(pkt.get("heading"), -0.5),
        "battery_V": _clean(pkt.get("batt"), 0),
        "sats": pkt.get("sats"),
        "snr": _clean(pkt.get("snr"), -1e6),
        "frame": pkt.get("frame"),
        "sonde_time": pkt.get("datetime"),
    }
    lat0, lon0 = opts.get("station_lat"), opts.get("station_lon")
    if lat0 and lon0 and st["latitude"] is not None and st["longitude"] is not None:
        st["distance_km"] = round(haversine_km(lat0, lon0, st["latitude"], st["longitude"]), 1)
        st["bearing_deg"] = round(bearing_deg(lat0, lon0, st["latitude"], st["longitude"]), 0)
    return {k: v for k, v in st.items() if v is not None}


class _UDP(asyncio.DatagramProtocol):
    def __init__(self, cb):
        self.cb = cb

    def datagram_received(self, data, addr):
        try:
            pkt = json.loads(data)
        except ValueError:
            return
        if isinstance(pkt, dict) and pkt.get("type") == "PAYLOAD_SUMMARY":
            self.cb(pkt)


async def open_listener(cb):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("0.0.0.0", UDP_PORT))
    transport, _ = await loop.create_datagram_endpoint(lambda: _UDP(cb), sock=sock)
    return transport


# HA entities for the single "Radiosonde tracker" device
# field -> (component, device_class, unit, state_class, entity_category, name)
TRACKER_FIELDS = {
    "serial": ("sensor", None, None, None, None, "Serial"),
    "sonde_type": ("sensor", None, None, None, None, "Type"),
    "altitude_m": ("sensor", "distance", "m", "measurement", None, "Altitude"),
    "temperature_C": ("sensor", "temperature", "°C", "measurement", None, "Temperature"),
    "humidity": ("sensor", "humidity", "%", "measurement", None, "Humidity"),
    "pressure_hPa": ("sensor", "atmospheric_pressure", "hPa", "measurement", None, "Pressure"),
    "vertical_speed_m_s": ("sensor", "speed", "m/s", "measurement", None, "Vertical speed"),
    "horizontal_speed_km_h": ("sensor", "wind_speed", "km/h", "measurement", None, "Horizontal speed"),
    "heading_deg": ("sensor", None, "°", "measurement", None, "Heading"),
    "distance_km": ("sensor", "distance", "km", "measurement", None, "Distance"),
    "bearing_deg": ("sensor", None, "°", "measurement", None, "Bearing"),
    "frequency_mhz": ("sensor", "frequency", "MHz", None, "diagnostic", "Frequency"),
    "snr": ("sensor", None, "dB", "measurement", "diagnostic", "SNR"),
    "battery_V": ("sensor", "voltage", "V", "measurement", "diagnostic", "Battery"),
    "last_seen": ("sensor", "timestamp", None, None, "diagnostic", "Last seen"),
}
