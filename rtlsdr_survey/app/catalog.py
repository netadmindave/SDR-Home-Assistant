"""Frequency catalog: every frequency/band the bundled tools support, plus user-added ones.

The Frequencies tab shows this as a checklist. The selected ids live in the `frequencies`
option, extra lines in `custom_frequencies`, and each optional tool has an `enable_*` switch.
build_slots() turns all of that into the scanner's slot list.
"""
import logging
import re

log = logging.getLogger("catalog")

# Tools that can be switched on/off. rtl_433 and rtl_power are always available.
TOOLS = {
    "rtl_433":   {"label": "rtl_433 (ISM sensors)", "option": None},
    "rtl_power": {"label": "rtl_power (energy sweeps)", "option": None},
    "auto_rx":   {"label": "radiosonde_auto_rx (weather balloons)", "option": "radiosonde_enabled"},
    "aprs":      {"label": "Direwolf (APRS)", "option": "enable_aprs"},
    "noaa":      {"label": "rtl_fm + multimon-ng (NOAA weather radio SAME alerts)", "option": "enable_noaa"},
    "adsb":      {"label": "readsb (ADS-B aircraft)", "option": "enable_adsb"},
    "ais":       {"label": "rtl_ais (marine AIS)", "option": "enable_ais"},
}


def _d(id_, group, name, decoder, freq=None, rng=None, weight=1.0, default=False, note="",
       sample_rate=None):
    return {"id": id_, "group": group, "name": name, "decoder": decoder, "freq": freq,
            "range": rng, "weight": weight, "default": default, "note": note,
            "sample_rate": sample_rate}


CATALOG = [
    # ---- rtl_433: Part 15.231 remotes, TPMS, security, ISM sensors
    _d("r433_43392", "rtl_433", "433.92 MHz sensors", "rtl_433", "433.92M", weight=3, default=True,
       note="Most weather/temperature sensors, doorbells, remotes"),
    _d("r433_315", "rtl_433", "315 MHz TPMS / key fobs", "rtl_433", "315M", weight=2, default=True,
       note="US tire pressure sensors, car remotes"),
    _d("r433_345", "rtl_433", "345 MHz security", "rtl_433", "345M", default=True,
       note="Honeywell/Resideo 5800, 2GIG"),
    _d("r433_310", "rtl_433", "310 MHz remotes", "rtl_433", "310M", default=True),
    _d("r433_318", "rtl_433", "318 MHz remotes", "rtl_433", "318M", default=True),
    _d("r433_303", "rtl_433", "303.875 MHz fans / remotes", "rtl_433", "303.875M", default=True),
    _d("r433_390", "rtl_433", "390 MHz garage remotes", "rtl_433", "390M", default=True,
       note="Chamberlain/LiftMaster"),
    _d("r433_418", "rtl_433", "418 MHz", "rtl_433", "418M",
       note="Some UK/older remotes and sensors"),
    _d("r433_915", "rtl_433", "915 MHz sensors", "rtl_433", "915M", weight=2, default=True,
       sample_rate="1024k", note="Acurite/Ambient, ERT meters, many US sensors"),
    _d("r433_27", "rtl_433", "27 MHz ISM", "rtl_433", "27.145M", weight=0.5, default=True),
    _d("r433_8683", "rtl_433", "868.3 MHz (EU SRD)", "rtl_433", "868.3M", sample_rate="1024k",
       note="EU band; only imported devices in the US"),
    _d("r433_86895", "rtl_433", "868.95 MHz (EU, wM-Bus)", "rtl_433", "868.95M",
       sample_rate="1024k"),
    # ---- rtl_power energy sweeps (find what nothing decodes)
    _d("sw_27", "rtl_power sweeps", "27 MHz ISM / CB", "rtl_power", rng="26.95M:27.41M:5k", default=True),
    _d("sw_40", "rtl_power sweeps", "40.68 MHz ISM", "rtl_power", rng="40.6M:40.76M:5k", default=True),
    _d("sw_49", "rtl_power sweeps", "49.8 MHz Part 15", "rtl_power", rng="49.78M:49.92M:5k", default=True),
    _d("sw_72", "rtl_power sweeps", "72-76 MHz RC", "rtl_power", rng="72M:76M:10k", default=True),
    _d("sw_mursl", "rtl_power sweeps", "MURS low", "rtl_power", rng="151.78M:151.98M:5k", default=True),
    _d("sw_mursh", "rtl_power sweeps", "MURS high", "rtl_power", rng="154.54M:154.62M:5k", default=True),
    _d("sw_300", "rtl_power sweeps", "300-320 MHz remotes / TPMS", "rtl_power", rng="300M:320M:10k", default=True),
    _d("sw_340", "rtl_power sweeps", "340-350 MHz", "rtl_power", rng="340M:350M:10k", default=True),
    _d("sw_385", "rtl_power sweeps", "385-395 MHz", "rtl_power", rng="385M:395M:10k", default=True),
    _d("sw_425", "rtl_power sweeps", "425-440 MHz (433 ISM / 70cm)", "rtl_power", rng="425M:440M:10k", default=True),
    _d("sw_frs", "rtl_power sweeps", "FRS/GMRS", "rtl_power", rng="462.5M:467.75M:12.5k", default=True),
    _d("sw_902", "rtl_power sweeps", "902-928 MHz ISM", "rtl_power", rng="902M:928M:25k", weight=2.7, default=True),
    # ---- radiosondes (scheduled by launch windows, not the survey rotation)
    _d("rs_400", "Radiosondes", "Weather balloons 400-406 MHz", "auto_rx", rng="400.05-405.95 MHz",
       note="Runs during the radiosonde launch windows; on/off is the tool switch"),
    # ---- APRS
    _d("aprs_14439", "APRS", "APRS 144.390 MHz (North America)", "aprs", "144.39M", default=True,
       note="Amateur positions, weather stations, telemetry"),
    _d("aprs_144800", "APRS", "APRS 144.800 MHz (Europe)", "aprs", "144.8M"),
    _d("aprs_145825", "APRS", "APRS 145.825 MHz (ISS / satellites)", "aprs", "145.825M"),
    # ---- NOAA weather radio SAME
    *[_d(f"noaa_{f.replace('.', '')}", "NOAA weather radio", f"NOAA WX {f} MHz", "noaa", f"{f}M",
         note="Pick your local transmitter (see weather.gov/nwr)")
      for f in ("162.400", "162.425", "162.450", "162.475", "162.500", "162.525", "162.550")],
    # ---- ADS-B
    _d("adsb_1090", "ADS-B", "ADS-B 1090 MHz aircraft", "adsb", "1090M", default=True,
       note="Needs a 1090 MHz antenna for decent range"),
    # ---- AIS
    _d("ais_162", "AIS", "Marine AIS 161.975 + 162.025 MHz", "ais", "162M", default=True,
       note="Both channels at once; useful near the coast or navigable water"),
]
BY_ID = {c["id"]: c for c in CATALOG}
DEFAULT_IDS = [c["id"] for c in CATALOG if c["default"]]

CUSTOM_DECODERS = {"rtl_433", "aprs", "noaa", "sweep", "rtl_power"}
_FREQ_RE = r"\d+(?:\.\d+)?[kKmMgG]?"
CUSTOM_RE = re.compile(
    rf"^\s*(?P<f>{_FREQ_RE}(?::{_FREQ_RE}(?::{_FREQ_RE})?)?)\s+(?P<dec>[a-z_0-9]+)(?:\s+(?P<name>.+?))?\s*$")


def parse_custom(lines):
    """'418.5M rtl_433 Gate remote' / '162.55M noaa' / '144.39M aprs' /
    '418M:420M:10k sweep'. Returns (entries, errors)."""
    from config import parse_freq
    out, errors = [], []
    for i, raw in enumerate(lines or []):
        line = (raw or "").split("#", 1)[0].strip()
        if not line:
            continue
        m = CUSTOM_RE.match(line)
        if not m or m.group("dec") not in CUSTOM_DECODERS:
            errors.append(f"line {i + 1}: '{raw}': expected '<freq> <rtl_433|aprs|noaa>' "
                          "or '<low>:<high>:<step> sweep'")
            continue
        dec = "rtl_power" if m.group("dec") in ("sweep", "rtl_power") else m.group("dec")
        f = m.group("f")
        try:
            if dec == "rtl_power":
                parts = f.split(":")
                if len(parts) == 2:
                    parts.append("10k")
                if len(parts) != 3:
                    raise ValueError("sweep needs low:high[:step]")
                lo, hi, step = (parse_freq(p) for p in parts)
                if not (24e6 <= lo < hi <= 1766e6):
                    raise ValueError("sweep must be within 24-1766 MHz, low < high")
                rng = ":".join(parts)
                name = m.group("name") or f"Custom sweep {lo / 1e6:g}-{hi / 1e6:g} MHz"
                out.append(_d(f"custom:{f} sweep", "Custom", name, dec, rng=rng))
            else:
                if ":" in f:
                    raise ValueError("ranges are only for 'sweep'")
                hz = parse_freq(f)
                if not 24e6 <= hz <= 1766e6:
                    raise ValueError("frequency must be within 24-1766 MHz")
                name = m.group("name") or f"Custom {dec} {hz / 1e6:g} MHz"
                out.append(_d(f"custom:{f} {dec}", "Custom", name, dec, f,
                              sample_rate="1024k" if dec == "rtl_433" and hz > 800e6 else None))
        except ValueError as e:
            errors.append(f"line {i + 1}: '{raw}': {e}")
    return out, errors


def tool_enabled(opts, decoder):
    opt = TOOLS.get(decoder, {}).get("option")
    return True if opt is None else bool(opts.get(opt))


def build_slots(opts):
    """Selected catalog entries + custom lines -> scanner slots (survey rotation)."""
    from config import parse_freq
    selected = opts.get("frequencies")
    if selected is None:
        selected = DEFAULT_IDS
    entries = [BY_ID[i] for i in selected if i in BY_ID]
    custom, errors = parse_custom(opts.get("custom_frequencies") or [])
    for e in errors:
        log.warning("custom frequency ignored: %s", e)
    entries += custom
    slots = []
    for c in entries:
        dec = c["decoder"]
        if dec == "auto_rx" or not tool_enabled(opts, dec):
            continue   # radiosondes run on their own schedule
        w = float(c["weight"])
        if dec == "rtl_power":
            lo, hi, step = c["range"].split(":")
            slots.append({"name": c["name"], "kind": "sweep", "decoder": "rtl_power",
                          "low": parse_freq(lo), "high": parse_freq(hi), "step": parse_freq(step),
                          "range": c["range"], "dwell": int(opts["sweep_dwell_s"] * w),
                          "catalog_id": c["id"]})
        else:
            sr = c.get("sample_rate") or "250k"
            slots.append({"name": c["name"], "kind": "decode", "decoder": dec,
                          "freq": parse_freq(c["freq"]), "sample_rate": parse_freq(sr),
                          "dwell": int(opts["decode_dwell_s"] * w), "catalog_id": c["id"]})
    return {"slots": slots, "source": "Frequencies tab", "custom_errors": errors}


def describe(opts):
    selected = set(opts.get("frequencies") if opts.get("frequencies") is not None else DEFAULT_IDS)
    _, errors = parse_custom(opts.get("custom_frequencies") or [])
    return {
        "tools": [{"id": k, "label": v["label"], "option": v["option"],
                   "enabled": tool_enabled(opts, k)} for k, v in TOOLS.items()],
        "catalog": [dict(c, selected=c["id"] in selected) for c in CATALOG],
        "custom": opts.get("custom_frequencies") or [],
        "custom_entries": [{"id": c["id"], "name": c["name"]}
                           for c in parse_custom(opts.get("custom_frequencies") or [])[0]],
        "custom_errors": errors,
    }
