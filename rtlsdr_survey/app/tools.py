"""Parsers for the optional tools: multimon-ng (NOAA EAS/SAME), Direwolf (APRS),
readsb (ADS-B aircraft.json) and rtl_ais (AIVDM NMEA). Pure functions, no I/O."""
import datetime as dt
import math
import re

# ---------------------------------------------------------------- NOAA SAME (multimon-ng EAS)
SAME_RE = re.compile(
    r"ZCZC-(?P<org>[A-Z]{3})-(?P<evt>[A-Z0-9]{3})-(?P<locs>[\d\-]+)\+(?P<dur>\d{4})-"
    r"(?P<issued>\d{7})-(?P<sender>[^-]{1,8})-?")
SAME_EVENTS = {
    "TOR": "Tornado Warning", "TOA": "Tornado Watch", "SVR": "Severe Thunderstorm Warning",
    "SVA": "Severe Thunderstorm Watch", "SVS": "Severe Weather Statement",
    "FFW": "Flash Flood Warning", "FFA": "Flash Flood Watch", "FFS": "Flash Flood Statement",
    "FLW": "Flood Warning", "FLA": "Flood Watch", "FLS": "Flood Statement",
    "HUW": "Hurricane Warning", "HUA": "Hurricane Watch", "HLS": "Hurricane Statement",
    "TRW": "Tropical Storm Warning", "TRA": "Tropical Storm Watch", "SSW": "Storm Surge Warning",
    "SSA": "Storm Surge Watch", "EWW": "Extreme Wind Warning", "SMW": "Special Marine Warning",
    "SPS": "Special Weather Statement", "WSW": "Winter Storm Warning", "WSA": "Winter Storm Watch",
    "BZW": "Blizzard Warning", "HWW": "High Wind Warning", "HWA": "High Wind Watch",
    "FRW": "Fire Warning", "CEM": "Civil Emergency Message", "EVI": "Evacuation Immediate",
    "SPW": "Shelter in Place Warning", "LAE": "Local Area Emergency", "CAE": "Child Abduction Emergency",
    "HMW": "Hazardous Materials Warning", "NUW": "Nuclear Power Plant Warning",
    "TSW": "Tsunami Warning", "TSA": "Tsunami Watch", "EQW": "Earthquake Warning",
    "DSW": "Dust Storm Warning", "BHW": "Biological Hazard Warning", "LEW": "Law Enforcement Warning",
    "RWT": "Required Weekly Test", "RMT": "Required Monthly Test", "DMO": "Demonstration Message",
    "ADR": "Administrative Message", "NPT": "National Periodic Test", "EAN": "Emergency Action Notification",
}
SAME_ORIG = {"WXR": "National Weather Service", "EAS": "EAS participant", "CIV": "Civil authorities",
             "PEP": "Primary Entry Point"}


def parse_same(line, now=None):
    m = SAME_RE.search(line)
    if not m:
        return None
    g = m.groupdict()
    now = now or dt.datetime.now(dt.timezone.utc)
    # issued JJJHHMM (UTC, day of year)
    jjj, hh, mm = int(g["issued"][:3]), int(g["issued"][3:5]), int(g["issued"][5:7])
    year = now.year if jjj <= now.timetuple().tm_yday + 1 else now.year - 1
    issued = dt.datetime(year, 1, 1, hh, mm, tzinfo=dt.timezone.utc) + dt.timedelta(days=jjj - 1)
    dur = dt.timedelta(hours=int(g["dur"][:2]), minutes=int(g["dur"][2:]))
    locs = [l for l in g["locs"].split("-") if l]
    return {
        "event_code": g["evt"], "event": SAME_EVENTS.get(g["evt"], g["evt"]),
        "originator": SAME_ORIG.get(g["org"], g["org"]), "locations": ",".join(locs),
        "duration_min": int(dur.total_seconds() // 60), "issued": issued.isoformat(),
        "expires": (issued + dur).isoformat(), "sender": g["sender"].strip(),
        "is_test": g["evt"] in ("RWT", "RMT", "DMO", "NPT"), "raw": m.group(0),
    }


# ---------------------------------------------------------------- APRS (Direwolf)
APRS_LINE_RE = re.compile(r"^\[[^\]]*\]\s*(?P<src>[A-Z0-9]{1,6}(?:-\d{1,2})?)>(?P<dst>[^,:]+)(?P<path>[^:]*):(?P<info>.*)$")
POS_RE = re.compile(r"(\d{2})(\d{2}\.\d{2})([NS]).(\d{3})(\d{2}\.\d{2})([EW])(.)")
WX_FIELDS = [("c", "wind_dir_deg", 3, 1), ("s", "wind_avg_mi_h", 3, 1), ("g", "wind_max_mi_h", 3, 1),
             ("t", "temperature_F", 3, 1), ("r", "rain_1h_in", 3, 0.01), ("p", "rain_24h_in", 3, 0.01),
             ("P", "rain_since_midnight_in", 3, 0.01), ("h", "humidity", 2, 1),
             ("b", "pressure_hPa", 5, 0.1), ("L", "solar_w_m2", 3, 1)]


def parse_aprs(line):
    line = line.replace("<0x0a>", "").replace("<0x0d>", "").rstrip()
    m = APRS_LINE_RE.match(line)
    if not m:
        return None
    src, info = m.group("src"), m.group("info")
    st = {"callsign": src, "path": (m.group("dst") + m.group("path")).strip(",")}
    p = POS_RE.search(info)
    wx_part = None
    if p:
        lat = int(p.group(1)) + float(p.group(2)) / 60
        lon = int(p.group(4)) + float(p.group(5)) / 60
        st["latitude"] = round(-lat if p.group(3) == "S" else lat, 5)
        st["longitude"] = round(-lon if p.group(6) == "W" else lon, 5)
        rest = info[p.end():]
        if p.group(7) == "_":   # weather station symbol: ddd/sss then wx fields
            mm = re.match(r"(\d{3}|\.{3})/(\d{3}|\.{3})(.*)", rest)
            if mm:
                if mm.group(1).isdigit():
                    st["wind_dir_deg"] = int(mm.group(1))
                if mm.group(2).isdigit():
                    st["wind_avg_mi_h"] = int(mm.group(2))
                wx_part = mm.group(3)
            else:
                wx_part = rest
        else:
            st["comment"] = rest.strip()[:80]
    elif info.startswith("_"):   # positionless weather
        wx_part = info[9:]
    if wx_part:
        for key, field, n, scale in WX_FIELDS:
            mm = re.search(re.escape(key) + r"(-?\d{%d})" % n, wx_part)
            if mm and not (key == "c" and "wind_dir_deg" in st) and not (key == "s" and "wind_avg_mi_h" in st):
                v = int(mm.group(1)) * scale
                if field == "humidity" and v == 0:
                    v = 100
                st[field] = round(v, 2)
    category = "APRS weather station" if any(f[1] in st for f in WX_FIELDS) else "APRS station"
    return f"aprs:{src}", "APRS", src, category, st


# ---------------------------------------------------------------- AIS (rtl_ais NMEA)
def _sixbit(payload):
    bits = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        bits.extend((v >> i) & 1 for i in range(5, -1, -1))
    return bits


def _u(bits, start, n):
    v = 0
    for b in bits[start:start + n]:
        v = (v << 1) | b
    return v


def _s(bits, start, n):
    v = _u(bits, start, n)
    return v - (1 << n) if bits[start] else v


def _text(bits, start, n):
    chars = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
    s = "".join(chars[_u(bits, i, 6)] for i in range(start, start + n, 6) if i + 6 <= len(bits))
    return s.replace("@", " ").strip()


class AISAssembler:
    """Reassembles multi-part !AIVDM sentences and decodes position / static reports."""
    def __init__(self):
        self.parts = {}

    def feed(self, line):
        m = re.search(r"!AIVD[MO],(\d),(\d),(\d?),([AB12]?),([^,]*),(\d)\*", line)
        if not m:
            return None
        total, num, seq, ch, payload, _fill = m.groups()
        total, num = int(total), int(num)
        if total > 1:
            key = (seq, ch)
            self.parts.setdefault(key, {})[num] = payload
            if len(self.parts[key]) < total:
                return None
            got = self.parts.pop(key)
            payload = "".join(got[i] for i in range(1, total + 1))
        return decode_ais(payload)


def decode_ais(payload):
    b = _sixbit(payload)
    if len(b) < 38:
        return None
    t, mmsi = _u(b, 0, 6), _u(b, 8, 30)
    st = {"mmsi": mmsi, "msg_type": t}
    if t in (1, 2, 3) and len(b) >= 168:
        sog, lon, lat, cog, hdg = _u(b, 50, 10), _s(b, 61, 28), _s(b, 89, 27), _u(b, 116, 12), _u(b, 128, 9)
        st["nav_status"] = _u(b, 38, 4)
    elif t in (18, 19) and len(b) >= 168:
        sog, lon, lat, cog, hdg = _u(b, 46, 10), _s(b, 57, 28), _s(b, 85, 27), _u(b, 112, 12), _u(b, 124, 9)
        if t == 19 and len(b) >= 263:
            st["ship_name"] = _text(b, 143, 120)
    elif t == 5 and len(b) >= 424:
        st["callsign"] = _text(b, 70, 42)
        st["ship_name"] = _text(b, 112, 120)
        st["ship_type"] = _u(b, 232, 8)
        st["destination"] = _text(b, 302, 120)
        return st
    elif t == 24 and len(b) >= 160:
        if _u(b, 38, 2) == 0:
            st["ship_name"] = _text(b, 40, 120)
        else:
            st["ship_type"] = _u(b, 40, 8)
            st["callsign"] = _text(b, 90, 42)
        return st
    else:
        return st
    if lon != 0x6791AC0 and lat != 0x3412140:
        st["longitude"] = round(lon / 600000.0, 5)
        st["latitude"] = round(lat / 600000.0, 5)
    if sog != 1023:
        st["speed_kn"] = sog / 10.0
    if cog != 3600:
        st["course_deg"] = cog / 10.0
    if hdg != 511:
        st["heading_deg"] = hdg
    return st


# ---------------------------------------------------------------- ADS-B (readsb aircraft.json)
def parse_aircraft(ac):
    hexid = (ac.get("hex") or "").strip().lower()
    if not hexid or hexid.startswith("~"):
        return None
    st = {"icao": hexid}
    for src, dst in (("flight", "flight"), ("r", "registration"), ("t", "aircraft_type"),
                     ("alt_baro", "altitude_ft"), ("gs", "ground_speed_kn"), ("track", "track_deg"),
                     ("lat", "latitude"), ("lon", "longitude"), ("squawk", "squawk"),
                     ("category", "category"), ("rssi", "rssi")):
        v = ac.get(src)
        if v not in (None, ""):
            st[dst] = v.strip() if isinstance(v, str) else v
    if st.get("altitude_ft") == "ground":
        st["altitude_ft"] = 0
    return st


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))
