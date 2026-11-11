"""Drives one RTL-SDR: time-slices it between rtl_433, rtl_power, rtl_fm pipelines, readsb,
rtl_ais and radiosonde_auto_rx. manager.py runs one of these per dongle."""
import asyncio
import collections
import re
import random
import json
import logging
import os
import shlex
import signal
import statistics
import time
from pathlib import Path

import datetime as dt

import radiosonde as rs
import tools
from classify import classify_rtl433, is_event_device, rtl433_identity, rtl433_state, uid_for
from config import data_dir

log = logging.getLogger("scanner")

BIN = os.environ.get("BIN_DIR", "")


def _bin(name):
    return os.path.join(BIN, name) if BIN else name


class Scanner:
    """Drives ONE dongle. A Manager (manager.py) owns one Scanner per configured SDR and shares
    the options, database, MQTT client, mode and log between them."""

    def __init__(self, mgr, sdr):
        self.mgr = mgr
        self.sdr = sdr
        self.name = sdr["name"]
        self.role = sdr.get("role", "rotating")     # rotating | dedicated
        over = {k: sdr[k] for k in ("device", "gain", "ppm") if sdr.get(k) not in (None, "")}
        self.o = collections.ChainMap(over, mgr.o)  # per-dongle radio settings over shared options
        self.plan = {"slots": [], "source": "Frequencies tab"}
        self.db = mgr.db
        self.mqtt = mgr.mqtt
        self.ring = mgr.ring
        self.rs_host = False        # handles radiosonde windows (rotating) / runs auto_rx (dedicated)
        self.current = None
        self.started = None
        self.ends = None
        self.cur_events = 0
        self._skip = asyncio.Event()
        self._procs = []
        self._pos = {"survey": 0, "monitor": 0}
        self._next_day_survey = self._schedule_day_survey(time.time())
        self._pending = None

    # shared state lives on the manager
    mode = property(lambda self: self.mgr.mode)
    rs_windows = property(lambda self: self.mgr.rs_windows)
    survey_windows = property(lambda self: self.mgr.survey_windows)
    first_start = property(lambda self: self.mgr.first_start)

    @property
    def rs_force_until(self):
        return self.mgr.rs_force_until

    @rs_force_until.setter
    def rs_force_until(self, v):
        self.mgr.rs_force_until = v

    # ------------------------------------------------------------- helpers
    def note(self, msg, level=logging.INFO):
        self.mgr.note(msg, level, sdr=self.name)

    def skip(self):
        if self.current and self.current.get("decoder") == "auto_rx":
            self.rs_force_until = 0.0
        self._skip.set()

    def radiosonde_due(self):
        """Return the UTC timestamp the radiosonde slot should run until, or None."""
        if not self.o.get("radiosonde_enabled") or not self.rs_host:
            return None
        if self.rs_force_until > time.time():
            return self.rs_force_until
        end = rs.window_end(self.rs_windows)
        return end.timestamp() if end else None

    def radiosonde_slot(self, until):
        lo, hi = self.o["radiosonde_min_mhz"], self.o["radiosonde_max_mhz"]
        return {"name": "Radiosondes", "kind": "decode", "decoder": "auto_rx",
                "freq": int((lo + hi) / 2 * 1e6), "range": f"{lo}-{hi} MHz",
                "dwell": max(30, int(until - time.time()))}

    def _dev_args(self, style):
        dev = str(self.o["device"]).strip()
        if style == "rtl_433" and not dev.isdigit():
            dev = ":" + dev
        return ["-d", dev]

    def _gain(self):
        g = str(self.o["gain"]).strip().lower()
        return None if g in ("", "auto", "0") else g

    def survey_order(self):
        dec = [s for s in self.plan["slots"] if s["kind"] == "decode"]
        swp = [s for s in self.plan["slots"] if s["kind"] == "sweep"]
        out = []
        # interleave: every decode slot is followed by one sweep slot (sweeps wrap around)
        for i, d in enumerate(dec):
            out.append(d)
            if swp:
                out.append(swp[i % len(swp)])
        # make sure every sweep band appears at least once per cycle
        for s in swp[len(dec):]:
            out.append(s)
        return out or swp

    # ------------------------------------------------------------- scheduling
    def _local_window_end(self):
        """End (epoch) of the survey_schedule window we're in, local time, or None."""
        end = rs.window_end(self.survey_windows, dt.datetime.now())
        return end.timestamp() if end else None

    def _initial_survey_until(self):
        if not self.o.get("initial_survey"):
            return 0
        days = float(self.o.get("initial_survey_days") or 0)
        return self.first_start + days * 86400 if days > 0 else 0

    @staticmethod
    def _schedule_day_survey(after):
        """A random moment within the hour after `after`, so daytime survey slots land at
        unpredictable times rather than always on the hour."""
        return after + random.uniform(0, 3600)

    def effective_mode(self):
        """survey: always survey. monitor: monitoring only, never surveys.
        auto: survey inside survey_schedule (and during the optional initial survey),
        monitor otherwise with short random daytime survey slots. Auto keeps surveying until
        at least one device is approved, since there's nothing to monitor yet."""
        if self.mode in ("survey", "monitor"):
            return self.mode
        if not self.db.approved():
            return "survey"
        if time.time() < self._initial_survey_until() or self._local_window_end():
            return "survey"
        return "monitor"

    def monitor_plan(self):
        """Group approved devices by slot and pick the 'home' slot: the one with the most
        event-driven devices (door sensors, remotes...), then the most devices."""
        decode = {s["name"]: s for s in self.plan["slots"] if s["kind"] == "decode"}
        by_slot = {}
        for d in self.db.approved():
            if d["slot"] in decode:
                by_slot.setdefault(d["slot"], []).append(d)
        if not by_slot:
            return None
        home = max(by_slot, key=lambda k: (sum(is_event_device(d["category"]) for d in by_slot[k]),
                                           len(by_slot[k])))
        return decode, by_slot, home

    def next_monitor_slot(self):
        """Freshness-driven monitor scheduling.

        The dongle parks on the home slot. Another slot is visited only when one of its
        approved periodic devices hasn't been heard for monitor_refresh_min; the visit ends as
        soon as all of that slot's periodic devices have reported (or monitor_dwell_s passes).
        Returns (slot, uids_to_wait_for) or None.
        """
        plan = self.monitor_plan()
        if not plan:
            return None
        decode, by_slot, home = plan
        now = time.time()
        refresh = float(self.o.get("monitor_refresh_min") or 10) * 60
        due, next_due = [], float("inf")
        for name, devs in by_slot.items():
            if name == home:
                continue
            periodic = [d for d in devs if not is_event_device(d["category"])] or devs
            oldest = min(d["last_seen"] or 0 for d in periodic)
            if oldest + refresh <= now:
                due.append((oldest, name, periodic))
            else:
                next_due = min(next_due, oldest + refresh)
        if due:
            _, name, periodic = min(due)
            dwell = int(self.o.get("monitor_dwell_s") or 90)
            return dict(decode[name], dwell=dwell), {d["uid"] for d in periodic}
        dwell = 3600 if next_due == float("inf") else next_due - now
        return dict(decode[home], dwell=int(max(30, min(3600, dwell)))), None

    def _cap_dwell(self, slot):
        """Don't run past the next radiosonde window or the next auto-mode switch."""
        now = time.time()
        limits = []
        if self.o.get("radiosonde_enabled") and self.rs_host:
            nxt = rs.next_window_start(self.rs_windows)
            if nxt:
                limits.append(nxt.timestamp() - now)
        if (self.mode == "auto" and self.effective_mode() == "monitor"
                and int(self.o.get("daytime_survey_min_per_hour") or 0) > 0):
            limits.append(self._next_day_survey - now)
        if self.mode == "auto" and self.survey_windows:
            end = self._local_window_end()
            if end:
                limits.append(end - now)
            else:
                nxt = rs.next_window_start(self.survey_windows, dt.datetime.now())
                if nxt:
                    limits.append(nxt.timestamp() - now)
        if limits:
            cap = int(max(20, min(limits) + 1))
            if cap < slot["dwell"]:
                slot = dict(slot, dwell=cap)
        return slot

    def status(self):
        now = time.time()
        nxt = rs.next_window_start(self.rs_windows) if self.o.get("radiosonde_enabled") else None
        eff = self.effective_mode()
        order = self.survey_order()
        cycle = sum(s["dwell"] for s in order)
        mon = None
        plan = self.monitor_plan()
        if plan:
            decode, by_slot, home = plan
            mon = {"home": home, "slots": [
                {"name": k, "devices": len(v),
                 "event_devices": sum(is_event_device(d["category"]) for d in v),
                 "oldest_age_s": round(now - min(d["last_seen"] or 0 for d in v))}
                for k, v in sorted(by_slot.items())]}
        init_until = self._initial_survey_until()
        return {
            "sdr": self.name, "device": str(self.o["device"]), "role": self.role,
            "rs_host": self.rs_host,
            "mode": self.mode,
            "effective_mode": eff if self.role == "rotating" else "dedicated",
            "survey_schedule": self.o.get("survey_schedule") or None,
            "initial_survey_until": init_until if init_until > now else None,
            "next_day_survey": (self._next_day_survey if self.mode == "auto"
                                and int(self.o.get("daytime_survey_min_per_hour") or 0) > 0
                                else None),
            "monitor": mon,
            "current": self.current and {
                "name": self.current["name"], "kind": self.current["kind"],
                "decoder": self.current["decoder"],
                "freq": self.current.get("range") or f"{self.current['freq'] / 1e6:.4f} MHz",
                "elapsed": round(now - self.started) if self.started else 0,
                "dwell": self.current["dwell"],
                "events": self.cur_events,
                "waiting_for": len(self._pending) if self._pending else 0,
            },
            "cycle_seconds": cycle,
            "order": [s["name"] for s in order],
            "plan_source": self.plan["source"],
            "mqtt_connected": bool(self.mqtt.connected),
            "mqtt_host": self.o.get("mqtt_host") or None,
            "base": self.o["base_topic"],
            "radiosonde": {
                "enabled": bool(self.o.get("radiosonde_enabled")),
                "windows_utc": self.o.get("radiosonde_windows_utc"),
                "next_window": nxt.isoformat() if nxt else None,
                "forced_until": self.rs_force_until if self.rs_force_until > now else None,
                "web_port": self.o.get("radiosonde_web_port"),
                "station": [self.o.get("station_lat"), self.o.get("station_lon")],
            },
        }

    # ------------------------------------------------------------- main loop
    async def run(self):
        self.note(f"Starting: device {self.o['device']}, {self.role}, "
                  f"{len(self.plan['slots'])} slot(s)" + (", radiosonde host" if self.rs_host else ""))
        if self.role == "dedicated":
            await self._run_dedicated()
            return
        last_eff = None
        while True:
            until = self.radiosonde_due()
            if until:
                await self.run_slot(self.radiosonde_slot(until))
                await asyncio.sleep(1.0)
                continue
            eff = self.effective_mode()
            if eff != last_eff:
                self.note(f"Now {eff}" + (" (auto)" if self.mode == "auto" else ""))
                last_eff = eff
            if eff == "monitor":
                nxt = self.next_monitor_slot()
                if nxt:
                    slot, pending = nxt
                    await self.run_slot(self._cap_dwell(slot), pending)
                    await self._maybe_day_survey()
                    await asyncio.sleep(1.0)
                    continue
                if self.mode == "monitor":
                    self.current = None
                    self.note("Monitor-only mode, but no devices are approved: idle. "
                              "Approve devices or switch to auto/survey.", logging.WARNING)
                    try:
                        await asyncio.wait_for(self._skip.wait(), 60)
                    except asyncio.TimeoutError:
                        pass
                    self._skip.clear()
                    continue
            order = self.survey_order()
            if not order:
                self.note("Survey plan is empty", logging.ERROR)
                await asyncio.sleep(30)
                continue
            i = self._pos["survey"] % len(order)
            self._pos["survey"] = i + 1
            await self.run_slot(self._cap_dwell(dict(order[i], survey=True)))
            await asyncio.sleep(1.0)  # give USB a moment to release

    async def _run_dedicated(self):
        """Dedicated dongle: only its assigned frequencies, ignoring survey/monitor modes.
        One frequency = parked on it; several = rotate among just those."""
        i = 0
        while True:
            if self.rs_host and self.o.get("radiosonde_enabled"):
                await self.run_slot(self.radiosonde_slot(time.time() + 3600))
                await asyncio.sleep(1.0)
                continue
            slots = self.plan["slots"]
            if not slots:
                self.current = None
                self.note("Dedicated dongle has no frequencies assigned: idle. Assign some on "
                          "the Frequencies tab.", logging.WARNING)
                try:
                    await asyncio.wait_for(self._skip.wait(), 60)
                except asyncio.TimeoutError:
                    pass
                self._skip.clear()
                continue
            slot = slots[i % len(slots)]
            i += 1
            await self.run_slot(dict(slot, dwell=3600) if len(slots) == 1 else slot)
            await asyncio.sleep(1.0)

    async def _maybe_day_survey(self):
        """Auto mode only: once per hour, at a random time, survey one random slot briefly."""
        mins = int(self.o.get("daytime_survey_min_per_hour") or 0)
        if self.mode != "auto" or mins <= 0 or time.time() < self._next_day_survey:
            return
        hour_start = self._next_day_survey
        so = self.survey_order()
        if so:
            pick = random.choice(so)
            self.note(f"Daytime survey: {pick['name']} for up to {mins} min")
            await self.run_slot(self._cap_dwell(dict(pick, survey=True,
                                                     dwell=min(pick["dwell"], mins * 60))))
        # next one at a random time in the following hour (never two back-to-back)
        self._next_day_survey = self._schedule_day_survey(
            max(time.time(), hour_start - (hour_start % 3600) + 3600))

    async def run_slot(self, slot, pending=None):
        self.current, self.started, self.cur_events = slot, time.time(), 0
        self._pending = set(pending) if pending else None
        self.ends = self.started + slot["dwell"]
        self._skip.clear()
        self.mqtt.status({"sdr": self.name, "mode": self.mode, "effective_mode": self.effective_mode(),
                          "slot": slot["name"], "decoder": slot["decoder"],
                          "since": int(self.started)}, sdr=self.name if len(self.mgr.scanners) > 1 else None)
        err = None
        stats = {"frames": 0, "frames_decoded": 0}
        try:
            if slot["decoder"] == "rtl_433":
                err = await self._run_rtl433(slot, stats)
            elif slot["decoder"] in ("noaa", "aprs"):
                err = await self._run_fm_pipeline(slot)
            elif slot["decoder"] == "adsb":
                err = await self._run_adsb(slot)
            elif slot["decoder"] == "ais":
                err = await self._run_ais(slot)
            elif slot["decoder"] == "auto_rx":
                err = await self._run_autorx(slot)
            elif slot["decoder"] == "rtl_power":
                err = await self._run_rtl_power(slot)
            else:
                err = f"unknown decoder {slot['decoder']}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            log.exception("slot %s failed", slot["name"])
        finally:
            await self._kill_all()
        dwell = time.time() - self.started
        self.db.slot_run(slot, dwell, events=self.cur_events, frames=stats["frames"],
                         frames_decoded=stats["frames_decoded"], error=err)
        if err:
            self.note(f"{slot['name']}: {err}", logging.ERROR)
            await asyncio.sleep(10)  # avoid hammering a missing/busy dongle
        else:
            self.note(f"{slot['name']}: done, {dwell:.0f}s, {self.cur_events} events")
        self.current = None
        self._pending = None

    # ------------------------------------------------------------- process plumbing
    async def _spawn(self, cmd, cwd=None, kill_wait=6):
        self.note("$ " + " ".join(shlex.quote(c) for c in cmd), logging.DEBUG)
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, cwd=cwd)
        p.stderr_tail = collections.deque(maxlen=15)
        p.kill_wait = kill_wait
        self._procs.append(p)
        return p

    async def _drain_stderr(self, p, name):
        async for line in p.stderr:
            s = line.decode(errors="replace").rstrip()
            if s:
                p.stderr_tail.append(s)
                log.debug("[%s] %s", name, s)

    async def _wait(self, p, out_task, deadline):
        """Wait for dwell end, skip, or process exit. Returns process exit code or None."""
        skip = asyncio.create_task(self._skip.wait())
        done_p = asyncio.create_task(p.wait())
        try:
            await asyncio.wait({skip, done_p}, timeout=max(1, deadline - time.time()),
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            skip.cancel()
        if done_p.done():
            await asyncio.wait({out_task}, timeout=5)
            return p.returncode
        done_p.cancel()
        return None

    async def _kill_all(self):
        for p in self._procs:
            if p.returncode is None:
                try:
                    os.killpg(p.pid, signal.SIGINT)
                except ProcessLookupError:
                    continue
                try:
                    await asyncio.wait_for(p.wait(), getattr(p, "kill_wait", 6))
                except asyncio.TimeoutError:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await p.wait()
        self._procs.clear()

    def _early_exit_error(self, p, name, rc):
        if rc is None:
            return None
        dwell = self.current["dwell"] if self.current else 10
        if rc != 0 or time.time() < self.ends - min(5, 0.5 * dwell):
            lines = list(p.stderr_tail)
            keys = ("error", "no supported", "fail", "usb", "busy", "not found", "unable", "denied")
            bad = [s for s in lines if any(k in s.lower() for k in keys)]
            tail = " | ".join((bad or lines)[-4:])
            return f"{name} exited early (rc={rc}): {tail}"
        return None

    # ------------------------------------------------------------- rtl_433
    async def _run_rtl433(self, slot, stats):
        cmd = [_bin("rtl_433"), *self._dev_args("rtl_433"),
               "-f", str(slot["freq"]), "-s", str(slot["sample_rate"]),
               "-F", "json", "-F", "log", "-M", "level", "-M", "stats:1:60",
               "-T", str(slot["dwell"])]
        if self._gain():
            cmd += ["-g", self._gain()]
        if int(self.o.get("ppm") or 0):
            cmd += ["-p", str(self.o["ppm"])]
        if slot.get("survey") and self.o.get("survey_all_decoders"):
            cmd += await self._all_decoder_args()
        if self.o.get("rtl_433_extra_args"):
            cmd += shlex.split(self.o["rtl_433_extra_args"])
        p = await self._spawn(cmd)
        asyncio.create_task(self._drain_stderr(p, "rtl_433"))

        async def reader():
            async for line in p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:   # "-F log" text lines: keep for error reporting
                    s = line.decode(errors="replace").strip()
                    if s:
                        p.stderr_tail.append(s)
                        log.debug("[rtl_433] %s", s)
                    continue
                if not isinstance(ev, dict):
                    continue
                if "frames" in ev:
                    fr = ev.get("frames") or {}
                    stats["frames"] += int(fr.get("count", 0)) + int(fr.get("fsk", 0))
                    stats["frames_decoded"] += int(fr.get("events", 0))
                elif "model" in ev:
                    self.handle_rtl433(slot, ev)

        t = asyncio.create_task(reader())
        rc = await self._wait(p, t, self.ends + 10)
        return self._early_exit_error(p, "rtl_433", rc)

    async def _all_decoder_args(self):
        """-R for every protocol rtl_433 lists (default-on and off-by-default, not hidden).
        Asked from the binary itself so it always matches the build."""
        if getattr(self, "_all_r", None) is None:
            p = await asyncio.create_subprocess_exec(
                _bin("rtl_433"), "-R", "help",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await p.communicate()
            nums = re.findall(r"^\s+\[(\d+)\]", out.decode(errors="replace"), re.M)
            self._all_r = [a for n in nums for a in ("-R", str(int(n)))]
            off = len(re.findall(r"^\s+\[\d+\]\*", out.decode(errors="replace"), re.M))
            self.note(f"Survey decoders: all {len(nums)} enabled ({off} normally off)")
        return self._all_r

    def handle_rtl433(self, slot, ev):
        key, model, dev_id, channel = rtl433_identity(ev)
        self._store(slot, "rtl_433", key, model, dev_id, channel, classify_rtl433(ev),
                    ev.get("freq") or slot["freq"] / 1e6, ev.get("rssi"), ev.get("snr"),
                    rtl433_state(ev), ev)

    # ------------------------------------------------------------- radiosonde_auto_rx
    async def _run_autorx(self, slot):
        base = data_dir() / "auto_rx"
        cfg = rs.write_station_cfg(self.o, base / "station.cfg")
        (base / "log").mkdir(parents=True, exist_ok=True)
        try:
            listener = await rs.open_listener(lambda pkt: self.handle_sonde(slot, pkt))
        except OSError as e:
            return f"cannot open UDP {rs.UDP_PORT}: {e}"
        try:
            cmd = [rs.AUTORX_PY, "auto_rx.py", "-c", str(cfg), "-l", str(base / "log")]
            p = await self._spawn(cmd, cwd=str(rs.AUTORX_DIR), kill_wait=20)
            asyncio.create_task(self._drain_stderr(p, "auto_rx"))

            async def reader():   # auto_rx logs to stdout
                async for line in p.stdout:
                    s = line.decode(errors="replace").rstrip()
                    if s:
                        p.stderr_tail.append(s)
                        log.debug("[auto_rx] %s", s)
                        if "Detected new" in s or "landing" in s.lower():
                            self.note("auto_rx: " + s.split(" - ", 1)[-1][:200])

            t = asyncio.create_task(reader())
            rc = await self._wait(p, t, self.ends)
            return self._early_exit_error(p, "auto_rx", rc)
        finally:
            listener.close()

    def handle_sonde(self, slot, pkt):
        st = rs.telemetry_state(pkt, self.o)
        serial = st.get("serial") or "unknown"
        self._store(slot, "auto_rx", f"radiosonde:{serial}", st.get("sonde_type") or "Radiosonde",
                    serial, "", "Radiosonde (weather balloon)",
                    st.get("frequency_mhz"), None, st.get("snr"), st, pkt)
        self.mqtt.radiosonde_state(st)

    # ------------------------------------------------------------- rtl_fm pipelines
    def _rtl_fm(self, slot, rate):
        cmd = [_bin("rtl_fm"), *self._dev_args("rtl_fm"), "-f", str(slot["freq"]), "-M", "fm",
               "-s", str(rate), "-E", "dc"]
        if self._gain():
            cmd += ["-g", self._gain()]
        if int(self.o.get("ppm") or 0):
            cmd += ["-p", str(self.o["ppm"])]
        return cmd + ["-"]

    async def _run_fm_pipeline(self, slot):
        """rtl_fm | multimon-ng (NOAA SAME) or rtl_fm | direwolf (APRS)."""
        dec = slot["decoder"]
        if dec == "aprs":
            conf = data_dir() / "direwolf.conf"
            conf.write_text("ADEVICE null null\nCHANNEL 0\nMYCALL N0CALL\nMODEM 1200\n"
                            "AGWPORT 0\nKISSPORT 0\n")
            rate = 24000
            tool = [_bin("direwolf"), "-c", str(conf), "-r", str(rate), "-D", "1", "-t", "0",
                    "-q", "hd", "-"]
        else:
            rate = 22050
            tool = [_bin("multimon-ng"), "-t", "raw", "-q", "-a", "EAS", "-"]
        shell = (" ".join(shlex.quote(c) for c in self._rtl_fm(slot, rate)) + " 2>/dev/null | "
                 + " ".join(shlex.quote(c) for c in tool))
        p = await self._spawn(["sh", "-c", shell])
        asyncio.create_task(self._drain_stderr(p, dec))

        async def reader():
            async for raw in p.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self._handle_tool_line(slot, line)

        t = asyncio.create_task(reader())
        rc = await self._wait(p, t, self.ends)
        return self._early_exit_error(p, dec, rc)

    def _handle_tool_line(self, slot, line):
        dec, freq = slot["decoder"], slot["freq"] / 1e6
        if dec == "noaa":
            alert = tools.parse_same(line)
            if alert:
                self.note(f"NOAA {freq:.3f} MHz: {alert['event']} ({alert['sender']}) "
                          f"for {alert['locations']}, {alert['duration_min']} min")
                st = dict(alert, frequency_mhz=freq)
                self._store(slot, "noaa", f"noaa:{freq:.3f}", f"NOAA WX {freq:.3f} MHz",
                            f"{freq:.3f}", "", "NOAA weather radio alerts", freq, None, None, st,
                            {"line": line})
                self._noaa_publish(st)
        elif dec == "aprs":
            r = tools.parse_aprs(line)
            if r:
                key, model, dev_id, category, st = r
                self._store(slot, "aprs", key, model, dev_id, "", category, freq, None, None, st,
                            {"line": line})

    def _noaa_publish(self, alert):
        active = (not alert.get("is_test")
                  and dt.datetime.fromisoformat(alert["expires"]) > dt.datetime.now(dt.timezone.utc))
        st = dict(alert, active=active)
        self.mqtt.publish_fixed("noaa", st)
        if active:
            secs = (dt.datetime.fromisoformat(alert["expires"])
                    - dt.datetime.now(dt.timezone.utc)).total_seconds()
            asyncio.get_running_loop().call_later(
                max(1, secs), lambda: self.mqtt.publish_fixed("noaa", dict(st, active=False)))

    # ------------------------------------------------------------- ADS-B / AIS
    def _summary(self, slot_kind, seen):
        """Aggregate state for the fixed 'receiver' HA device."""
        lat0, lon0 = self.o.get("station_lat"), self.o.get("station_lon")
        best, mx, npos = None, 0.0, 0
        for st in seen.values():
            if "latitude" in st and lat0 and lon0:
                npos += 1
                d = tools.distance_km(lat0, lon0, st["latitude"], st["longitude"])
                mx = max(mx, d)
                if best is None or d < best[0]:
                    best = (d, st)
        name_key = "flight" if slot_kind == "adsb" else "ship_name"
        id_key = "icao" if slot_kind == "adsb" else "mmsi"
        out = {"count": len(seen), "with_position": npos, "max_range_km": round(mx, 1)}
        if best:
            out["nearest"] = str(best[1].get(name_key) or best[1].get(id_key))
            out["nearest_km"] = round(best[0], 1)
        return out

    async def _run_adsb(self, slot):
        jdir = Path(f"/tmp/readsb-{re.sub(r'[^A-Za-z0-9_-]', '_', self.name)}")
        jdir.mkdir(exist_ok=True)
        for f in jdir.glob("*.json"):
            f.unlink()
        cmd = [_bin("readsb"), "--device-type", "rtlsdr", "--device", str(self.o["device"]),
               "--write-json", str(jdir), "--write-json-every", "1", "--quiet"]
        if self._gain():
            cmd += ["--gain", self._gain()]
        if int(self.o.get("ppm") or 0):
            cmd += ["--ppm", str(self.o["ppm"])]
        if self.o.get("station_lat") and self.o.get("station_lon"):
            cmd += ["--lat", str(self.o["station_lat"]), "--lon", str(self.o["station_lon"])]
        p = await self._spawn(cmd)
        asyncio.create_task(self._drain_stderr(p, "readsb"))
        seen, last_store = {}, {}

        async def poller():
            while p.returncode is None:
                await asyncio.sleep(2)
                try:
                    data = json.loads((jdir / "aircraft.json").read_text())
                except (OSError, ValueError):
                    continue
                for ac in data.get("aircraft", []):
                    if ac.get("seen", 99) > 10:
                        continue
                    st = tools.parse_aircraft(ac)
                    if not st:
                        continue
                    if "latitude" in st and self.o.get("station_lat"):
                        st["distance_km"] = round(tools.distance_km(
                            self.o["station_lat"], self.o["station_lon"],
                            st["latitude"], st["longitude"]), 1)
                    seen[st["icao"]] = st
                    if time.time() - last_store.get(st["icao"], 0) >= 15:
                        last_store[st["icao"]] = time.time()
                        self._store(slot, "adsb", f"adsb:{st['icao']}",
                                    st.get("aircraft_type") or "Aircraft", st["icao"], "",
                                    "Aircraft (ADS-B)", 1090.0, ac.get("rssi"), None, st, ac)
                self.mqtt.publish_fixed("adsb", self._summary("adsb", seen))

        t = asyncio.create_task(poller())
        try:
            rc = await self._wait(p, t, self.ends)
        finally:
            t.cancel()
        return self._early_exit_error(p, "readsb", rc)

    async def _run_ais(self, slot):
        cmd = [_bin("rtl_ais"), "-d", str(self.o["device"]), "-n", "-h", "127.0.0.1", "-P", "10110"]
        if self._gain():
            cmd += ["-g", self._gain()]
        if int(self.o.get("ppm") or 0):
            cmd += ["-p", str(self.o["ppm"])]
        p = await self._spawn(cmd)
        asm, seen = tools.AISAssembler(), {}

        async def reader():   # rtl_ais -n logs NMEA on stderr
            async for raw in p.stderr:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                if "!AIVD" not in line:
                    p.stderr_tail.append(line)
                    continue
                st = asm.feed(line)
                if not st:
                    continue
                prev = seen.get(st["mmsi"], {})
                st = {**prev, **st}
                if "latitude" in st and self.o.get("station_lat"):
                    st["distance_km"] = round(tools.distance_km(
                        self.o["station_lat"], self.o["station_lon"],
                        st["latitude"], st["longitude"]), 1)
                seen[st["mmsi"]] = st
                self._store(slot, "ais", f"ais:{st['mmsi']}", "AIS", str(st["mmsi"]), "",
                            "Ship (AIS)", 162.0, None, None, st, {"nmea": line})
                self.mqtt.publish_fixed("ais", self._summary("ais", seen))

        t = asyncio.create_task(reader())
        rc = await self._wait(p, t, self.ends)
        return self._early_exit_error(p, "rtl_ais", rc)

    # ------------------------------------------------------------- rtl_power
    async def _run_rtl_power(self, slot):
        integ = max(1, int(self.o.get("sweep_integration_s") or 5))
        cmd = [_bin("rtl_power"), *self._dev_args("rtl_power"), "-f", slot["range"],
               "-i", str(integ), "-e", f"{slot['dwell']}s", "-c", "25%", "-P"]
        if self._gain():
            cmd += ["-g", self._gain()]
        if int(self.o.get("ppm") or 0):
            cmd += ["-p", str(self.o["ppm"])]
        p = await self._spawn(cmd)
        asyncio.create_task(self._drain_stderr(p, "rtl_power"))
        thr = float(self.o.get("peak_threshold_db") or 10)
        bucket_w = max(slot["step"], 12_500)
        buckets = {}

        async def reader():
            async for line in p.stdout:
                self._sweep_line(line.decode(errors="replace"), thr, bucket_w, buckets)

        t = asyncio.create_task(reader())
        rc = await self._wait(p, t, self.ends + 15)
        if buckets:
            self.db.merge_signals(slot["name"], buckets)
            self.cur_events = sum(1 for b in buckets.values() if b[0])
        return self._early_exit_error(p, "rtl_power", rc)

    @staticmethod
    def _sweep_line(line, thr, bucket_w, buckets):
        """rtl_power CSV: date, time, hz_low, hz_high, hz_step, samples, dB, dB, ..."""
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 8:
            return
        try:
            lo, hi, step = float(parts[2]), float(parts[3]), float(parts[4])
            vals = [float(x) for x in parts[6:] if x not in ("", "nan", "-nan")]
        except ValueError:
            return
        if len(vals) < 5:
            return
        noise = statistics.median(vals)
        centre = (lo + hi) / 2
        seen = {}
        for i, v in enumerate(vals):
            f = lo + (i + 0.5) * step
            if abs(f - centre) <= 1.5 * step:   # DC spike at the tuned centre
                continue
            b = int(f // bucket_w * bucket_w)
            snr = v - noise
            seen[b] = max(seen.get(b, -999.0), snr)
        now = time.time()
        for b, snr in seen.items():
            rec = buckets.setdefault(b, [0, 0, 0.0, 0.0, None])
            rec[1] += 1
            if snr >= thr:
                rec[0] += 1
                rec[2] = max(rec[2], snr)
                rec[3] += snr
                rec[4] = now

    # ------------------------------------------------------------- shared store
    def _store(self, slot, source, key, model, dev_id, channel, category, freq, rssi, snr,
               state, raw):
        uid = uid_for(key)
        new = self.db.upsert_device(
            uid=uid, key=key, source=source, model=model, dev_id=dev_id, channel=channel,
            category=category, slot=slot["name"], freq_mhz=freq, rssi=rssi, snr=snr,
            state=state, raw=raw)
        self.cur_events += 1
        if self._pending and uid in self._pending:
            self._pending.discard(uid)
            if not self._pending:
                self.note(f"{slot['name']}: all monitored devices reported, moving on")
                self._skip.set()
        dev = self.db.device(uid)
        if new:
            self.note(f"New device: {category} — {model} id={dev_id} on {slot['name']}")
        self.mqtt.raw_event(dev, raw)
        self.mqtt.device_state(dev, json.loads(dev["last_state"] or "{}"))
