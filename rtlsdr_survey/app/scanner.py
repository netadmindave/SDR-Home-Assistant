"""Time-slices one RTL-SDR between rtl_433 (decoding) and rtl_power (energy sweeps)."""
import asyncio
import collections
import json
import logging
import os
import shlex
import signal
import statistics
import time

import datetime as dt

import radiosonde as rs
from classify import classify_rtl433, rtl433_identity, rtl433_state, uid_for
from config import data_dir

log = logging.getLogger("scanner")

BIN = os.environ.get("BIN_DIR", "")


def _bin(name):
    return os.path.join(BIN, name) if BIN else name


class Scanner:
    def __init__(self, opts, plan, db, mqtt):
        self.o = opts
        self.plan = plan
        self.db = db
        self.mqtt = mqtt
        self.current = None
        self.started = None
        self.ends = None
        self.cur_events = 0
        self.ring = collections.deque(maxlen=400)
        self._skip = asyncio.Event()
        self._procs = []
        self._pos = {"survey": 0, "monitor": 0}
        self._last_mini_survey = time.time()
        # The UI toggle persists across restarts, unless the add-on option itself was changed.
        if db.get_setting("mode_option") != opts["mode"]:
            db.set_setting("mode_option", opts["mode"])
            db.set_setting("mode", opts["mode"])
        self.mode = db.get_setting("mode", opts["mode"])
        self.rs_windows = rs.parse_windows(opts.get("radiosonde_windows_utc", ""))
        self.rs_force_until = 0.0

    # ------------------------------------------------------------- helpers
    def note(self, msg, level=logging.INFO):
        log.log(level, msg)
        self.ring.append({"t": time.time(), "msg": msg, "lvl": logging.getLevelName(level)})

    def set_mode(self, mode):
        if mode not in ("survey", "monitor"):
            raise ValueError(mode)
        self.mode = mode
        self.db.set_setting("mode", mode)
        self.note(f"Mode set to {mode}")
        self.skip()

    def skip(self):
        if self.current and self.current.get("decoder") == "auto_rx":
            self.rs_force_until = 0.0
        self._skip.set()

    def force_radiosonde(self, minutes):
        self.rs_force_until = time.time() + 60 * float(minutes)
        self.note(f"Radiosonde tracking forced for {minutes} min")
        self._skip.set()

    def radiosonde_due(self):
        """Return the UTC timestamp the radiosonde slot should run until, or None."""
        if not self.o.get("radiosonde_enabled"):
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

    def monitor_order(self):
        wanted = {d["slot"] for d in self.db.approved()}
        slots = [s for s in self.plan["slots"] if s["kind"] == "decode" and s["name"] in wanted]
        if len(slots) == 1:
            slots = [dict(slots[0], dwell=3600)]
        return slots

    def status(self):
        now = time.time()
        nxt = rs.next_window_start(self.rs_windows) if self.o.get("radiosonde_enabled") else None
        order = self.survey_order() if self.mode == "survey" else self.monitor_order()
        cycle = sum(s["dwell"] for s in order)
        return {
            "mode": self.mode,
            "current": self.current and {
                "name": self.current["name"], "kind": self.current["kind"],
                "decoder": self.current["decoder"],
                "freq": self.current.get("range") or f"{self.current['freq'] / 1e6:.4f} MHz",
                "elapsed": round(now - self.started) if self.started else 0,
                "dwell": self.current["dwell"],
                "events": self.cur_events,
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
        self.note(f"Scanner starting in {self.mode} mode, device {self.o['device']}")
        while True:
            until = self.radiosonde_due()
            if until:
                await self.run_slot(self.radiosonde_slot(until))
                await asyncio.sleep(1.0)
                continue
            if self.mode == "monitor":
                order = self.monitor_order()
                if not order:
                    self.note("Monitor mode but no approved devices; surveying instead",
                              logging.WARNING)
                    order, key = self.survey_order(), "survey"
                else:
                    key = "monitor"
            else:
                order, key = self.survey_order(), "survey"
            if not order:
                self.note("Survey plan is empty", logging.ERROR)
                await asyncio.sleep(30)
                continue
            i = self._pos[key] % len(order)
            self._pos[key] = i + 1
            await self.run_slot(order[i])

            # optional mini-survey while monitoring
            mins = int(self.o.get("monitor_hourly_survey_min") or 0)
            if (self.mode == "monitor" and key == "monitor" and mins > 0
                    and time.time() - self._last_mini_survey > 3600):
                self._last_mini_survey = time.time()
                so = self.survey_order()
                j = self._pos["survey"] % len(so)
                self._pos["survey"] = j + 1
                await self.run_slot(dict(so[j], dwell=min(so[j]["dwell"], mins * 60)))
            await asyncio.sleep(1.0)  # give USB a moment to release

    async def run_slot(self, slot):
        self.current, self.started, self.cur_events = slot, time.time(), 0
        self.ends = self.started + slot["dwell"]
        self._skip.clear()
        self.mqtt.status({"mode": self.mode, "slot": slot["name"], "decoder": slot["decoder"],
                          "since": int(self.started)})
        err = None
        stats = {"frames": 0, "frames_decoded": 0}
        try:
            if slot["decoder"] == "rtl_433":
                err = await self._run_rtl433(slot, stats)
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
        dev = self.db.device(uid)
        if new:
            self.note(f"New device: {category} — {model} id={dev_id} on {slot['name']}")
        self.mqtt.raw_event(dev, raw)
        self.mqtt.device_state(dev, json.loads(dev["last_state"] or "{}"))
