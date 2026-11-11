"""Runs one Scanner per configured SDR and divides the frequencies between them."""
import asyncio
import collections
import logging
import re
import time

import radiosonde as rs
from catalog import build_slots
from scanner import Scanner

log = logging.getLogger("manager")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def sdr_list(opts):
    """The `sdrs` option, or the legacy single `device` as one rotating dongle."""
    sdrs = [dict(s) for s in (opts.get("sdrs") or []) if s.get("device") not in (None, "")]
    if not sdrs:
        return [{"name": "sdr0", "device": str(opts.get("device", "0")), "role": "rotating"}]
    for i, s in enumerate(sdrs):
        s["name"] = s.get("name") or f"sdr{i}"
        s["role"] = s.get("role") or "rotating"
        s["device"] = str(s["device"])
    return sdrs


def parse_assignments(lines):
    out = {}
    for line in lines or []:
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


class Manager:
    def __init__(self, opts, db, mqtt):
        self.o = opts
        self.db = db
        self.mqtt = mqtt
        self.ring = collections.deque(maxlen=600)
        # The UI mode toggle persists across restarts, unless the option itself was changed.
        if db.get_setting("mode_option") != opts["mode"]:
            db.set_setting("mode_option", opts["mode"])
            db.set_setting("mode", opts["mode"])
        self.mode = db.get_setting("mode", opts["mode"])
        self.rs_windows = rs.parse_windows(opts.get("radiosonde_windows_utc", ""))
        self.survey_windows = rs.parse_windows(opts.get("survey_schedule", ""))
        self.rs_force_until = 0.0
        fs = db.get_setting("first_start")
        if not fs:
            fs = str(time.time())
            db.set_setting("first_start", fs)
        self.first_start = float(fs)
        self.scanners = [Scanner(self, s) for s in sdr_list(opts)]
        self.plan_errors = []
        self.rebuild()

    # ------------------------------------------------------------------ helpers
    def note(self, msg, level=logging.INFO, sdr=None):
        if sdr and len(self.scanners) > 1:
            msg = f"[{sdr}] {msg}"
        log.log(level, msg)
        self.ring.append({"t": time.time(), "msg": msg, "lvl": logging.getLevelName(level)})

    def by_name(self, name):
        return next((s for s in self.scanners if s.name == name), None)

    @property
    def rs_host(self):
        return next((s for s in self.scanners if s.rs_host), None)

    # ------------------------------------------------------------------ dividing frequencies
    def rebuild(self):
        """Split the ticked frequencies between dongles.

        1. A frequency assigned to a dongle (Frequencies tab) goes to that dongle.
        2. Everything else is shared out between the rotating dongles, balancing total dwell.
        3. Radiosondes go to the dongle they're assigned to, else the first rotating dongle.
        """
        plan = build_slots(self.o)
        self.plan_errors = plan.get("custom_errors", [])
        assign = parse_assignments(self.o.get("assignments"))
        per = {s.name: [] for s in self.scanners}
        rotating = [s for s in self.scanners if s.role == "rotating"]
        unassigned = []
        for slot in plan["slots"]:
            tgt = assign.get(slot.get("catalog_id"))
            (per[tgt] if tgt in per else unassigned).append(slot)
        if unassigned and not rotating:
            self.note(f"{len(unassigned)} ticked frequencies aren't assigned to a dongle and there is "
                      "no rotating dongle to take them", logging.WARNING)
        elif unassigned:
            load = {s.name: sum(x["dwell"] for x in per[s.name]) for s in rotating}
            # biggest first, each to the least-loaded rotating dongle; keep decode/sweep mix
            for slot in sorted(unassigned, key=lambda x: -x["dwell"]):
                tgt = min(load, key=lambda n: load[n])
                per[tgt].append(slot)
                load[tgt] += slot["dwell"]
            order = {id(x): i for i, x in enumerate(plan["slots"])}
            for n in per:
                per[n].sort(key=lambda x: order.get(id(x), 0))
        rs_tgt = assign.get("rs_400")
        host = self.by_name(rs_tgt) or (rotating[0] if rotating else None)
        for s in self.scanners:
            s.plan = {"slots": per[s.name], "source": "Frequencies tab"}
            s._pos = {"survey": 0, "monitor": 0}
            s.rs_host = s is host
        summary = ", ".join(f"{s.name}: {len(per[s.name])}" for s in self.scanners)
        self.note(f"Frequencies divided between {len(self.scanners)} dongle(s): {summary}"
                  + (f"; radiosondes on {host.name}" if host and self.o.get("radiosonde_enabled") else ""))

    # ------------------------------------------------------------------ actions used by the web UI
    def set_mode(self, mode):
        if mode not in ("survey", "monitor", "auto"):
            raise ValueError(mode)
        self.mode = mode
        self.db.set_setting("mode", mode)
        self.note(f"Mode set to {mode}")
        self.skip()

    def skip(self, name=None):
        for s in self.scanners:
            if name in (None, s.name):
                s.skip()

    def force_radiosonde(self, minutes):
        host = self.rs_host
        if not host:
            return
        self.rs_force_until = time.time() + 60 * float(minutes)
        self.note(f"Radiosonde tracking forced for {minutes} min on {host.name}")
        host._skip.set()

    def stop_radiosonde(self):
        self.rs_force_until = 0.0
        if self.rs_host:
            self.rs_host.skip()

    def apply_settings(self, changes):
        """Apply saved settings without a restart (everything except MQTT/topic/log/dongle-list changes)."""
        from config import DEFAULTS
        for k, v in changes.items():
            self.o[k] = DEFAULTS.get(k) if v is None else v
        if "survey_schedule" in changes:
            self.survey_windows = rs.parse_windows(self.o.get("survey_schedule") or "")
        if "radiosonde_windows_utc" in changes:
            self.rs_windows = rs.parse_windows(self.o.get("radiosonde_windows_utc") or "")
        plan_keys = {"decode_dwell_s", "sweep_dwell_s", "frequencies", "custom_frequencies",
                     "assignments", "radiosonde_enabled",
                     "enable_aprs", "enable_noaa", "enable_adsb", "enable_ais"}
        if plan_keys & set(changes):
            self.rebuild()
            self.mqtt.sync_fixed()
        if "daytime_survey_min_per_hour" in changes:
            for s in self.scanners:
                s._next_day_survey = s._schedule_day_survey(time.time())
        if "mode" in changes:
            self.db.set_setting("mode_option", self.o["mode"])
            self.mode = self.o["mode"]
            self.db.set_setting("mode", self.mode)
        if {"stale_after_min", "radiosonde_publish_ha", "radiosonde_enabled"} & set(changes):
            self.mqtt._announced = {}
            for d in self.db.approved():
                self.mqtt.announce(d)
            if self.o.get("radiosonde_enabled") and self.o.get("radiosonde_publish_ha"):
                self.mqtt.announce_radiosonde()
        self.note("Settings applied: " + ", ".join(sorted(changes)))
        self.skip()   # restart current slots with the new settings

    # ------------------------------------------------------------------ status
    def status(self):
        per = [s.status() for s in self.scanners]
        st = dict(per[0])
        st["sdrs"] = per
        # whole-system views
        st["order"] = [n for p in per for n in p["order"]]
        st["cycle_seconds"] = max((p["cycle_seconds"] for p in per), default=0)
        mons = [p["monitor"] for p in per if p.get("monitor")]
        st["monitor"] = ({"home": ", ".join(m["home"] for m in mons),
                          "slots": [x for m in mons for x in m["slots"]]} if mons else None)
        st["mode"] = self.mode
        st["plan_errors"] = self.plan_errors
        host = self.rs_host
        if host:
            st["radiosonde"] = host.status()["radiosonde"]
            st["radiosonde"]["host"] = host.name
        return st

    async def run(self):
        await asyncio.gather(*(s.run() for s in self.scanners))

    async def stop(self):
        for s in self.scanners:
            await s._kill_all()
