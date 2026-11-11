"""aiohttp web UI + JSON API (works behind HA ingress: all URLs are relative)."""
import asyncio
import json
from pathlib import Path

from aiohttp import web

import settings as st

STATIC = Path(__file__).with_name("static")
# transient or broadcast sources: published through fixed HA devices, not per-device approval
NON_APPROVABLE = {"auto_rx", "adsb", "ais", "noaa"}


def build_app(scanner, db, mqtt):
    app = web.Application()
    r = app.router

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    async def status(_):
        st = scanner.status()
        st["slots"] = db.slots()
        st["log"] = list(scanner.ring)[-150:]
        return web.json_response(st)

    async def devices(_):
        out = []
        for d in db.devices():
            d["last_state"] = json.loads(d["last_state"] or "{}")
            d["last_raw"] = json.loads(d["last_raw"] or "{}")
            out.append(d)
        return web.json_response(out)

    async def signals(req):
        min_hits = int(req.query.get("min_hits", 2))
        sig = db.signals(min_hits)
        devs = db.devices()
        decoded = [(d["freq_mhz"] or 0) * 1e6 for d in devs if d["source"] == "rtl_433"]
        for s in sig:
            f = s["bucket_hz"]
            s["duty"] = round(s["hits"] / s["obs"], 3) if s["obs"] else 0
            s["avg_snr"] = round(s["sum_snr"] / s["hits"], 1) if s["hits"] else 0
            if any(abs(f - df) < 150e3 for df in decoded):
                near = "decoded device nearby"
            else:
                near = ""
            s["decoded_nearby"] = near
            if s["duty"] > 0.8:
                s["kind"] = "continuous (carrier, spur, or broadcast)"
            elif s["duty"] > 0.2:
                s["kind"] = "frequent / bursty"
            else:
                s["kind"] = "intermittent bursts"
        return web.json_response(sig)

    async def update_device(req):
        uid = req.match_info["uid"]
        body = await req.json()
        before = db.device(uid)
        if not before:
            raise web.HTTPNotFound()
        db.update_device(uid, **body)
        after = db.device(uid)
        if after["source"] in NON_APPROVABLE:
            db.update_device(uid, approved=0)  # these publish via a shared receiver device
            after = db.device(uid)
        if after["approved"] and (not before["approved"] or before["name"] != after["name"]):
            mqtt.announce(after)
            mqtt.device_state(after, json.loads(after["last_state"] or "{}"))
        elif before["approved"] and not after["approved"]:
            mqtt.unannounce(before)
        return web.json_response(after)

    async def delete_device(req):
        uid = req.match_info["uid"]
        d = db.device(uid)
        if d and d["approved"]:
            mqtt.unannounce(d)
        db.delete_device(uid)
        return web.json_response({"ok": True})

    async def set_mode(req):
        body = await req.json()
        try:
            scanner.set_mode(body.get("mode"))
        except ValueError:
            raise web.HTTPBadRequest(text="mode must be auto, survey or monitor")
        # keep the add-on configuration in sync with the header selector
        try:
            await st.save({"mode": scanner.mode})
            scanner.o["_user"] = dict(scanner.o.get("_user") or {}, mode=scanner.mode)
            scanner.o["mode"] = scanner.mode
            db.set_setting("mode_option", scanner.mode)
        except Exception as e:  # noqa: BLE001
            scanner.note(f"Mode changed for now, but saving it to the config failed: {e}")
        return web.json_response({"mode": scanner.mode})

    async def radiosonde_now(req):
        body = await req.json() if req.can_read_body else {}
        minutes = float(body.get("minutes", 120))
        if minutes <= 0:
            scanner.stop_radiosonde()
        else:
            scanner.force_radiosonde(minutes)
        return web.json_response({"ok": True})

    async def skip(req):
        body = await req.json() if req.can_read_body else {}
        scanner.skip(body.get("sdr"))
        return web.json_response({"ok": True})

    async def reset(_):
        db.reset_survey()
        scanner.note("Survey data reset (approved devices kept)")
        return web.json_response({"ok": True})

    async def export(_):
        data = {"devices": db.devices(), "signals": db.signals(1), "slots": db.slots()}
        return web.json_response(data, headers={
            "Content-Disposition": "attachment; filename=rtlsdr_survey.json"})

    async def get_dongles(_):
        """List connected RTL-SDRs (index, name, serial) without opening them."""
        import re as _re
        from scanner import _bin
        try:
            p = await asyncio.create_subprocess_exec(
                _bin("rtl_test"), "-d", "9999", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(p.communicate(), 10)
        except (OSError, asyncio.TimeoutError) as e:
            return web.json_response({"dongles": [], "error": str(e)})
        found = []
        for m in _re.finditer(r"^\s*(\d+):\s+(.*?),\s+(.*?),\s+SN:\s*(\S*)", out.decode(errors="replace"), _re.M):
            found.append({"index": int(m.group(1)), "vendor": m.group(2), "product": m.group(3),
                          "serial": m.group(4)})
        in_use = {str(s.o["device"]): s.name for s in scanner.scanners}
        for d in found:
            d["used_by"] = in_use.get(d["serial"]) or in_use.get(str(d["index"]))
        return web.json_response({"dongles": found})

    async def get_frequencies(_):
        import catalog
        d = catalog.describe(scanner.o)
        d["sdrs"] = [{"name": s.name, "role": s.role, "device": str(s.o["device"])} for s in scanner.scanners]
        d["assignments"] = scanner.o.get("assignments") or []
        d["dwell"] = {"decode": scanner.o["decode_dwell_s"], "sweep": scanner.o["sweep_dwell_s"]}
        return web.json_response(d)

    async def get_settings(_):
        return web.json_response(st.describe(scanner.o))

    async def post_settings(req):
        body = await req.json()
        clean, errors = st.validate(body.get("changes") or {})
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=400)
        user = scanner.o.get("_user") or {}
        clean = {k: v for k, v in clean.items() if user.get(k) != v}   # only real changes
        if not clean:
            return web.json_response({"ok": True, "changed": [], "restart": False})
        try:
            new_user = await st.save(clean)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "errors": {"_": f"Could not save: {e}"}},
                                     status=502)
        merged = dict(user)
        for k, v in clean.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        scanner.o["_user"] = merged
        live = {k: v for k, v in clean.items() if st.SPEC_BY_KEY[k][6]}
        needs_restart = any(not st.SPEC_BY_KEY[k][6] for k in clean)
        if live:
            scanner.apply_settings(live)
        if needs_restart:
            scanner.note("Settings need a restart; restarting in 2 s")

            async def later():
                await asyncio.sleep(2)
                await st.restart()
            asyncio.get_running_loop().create_task(later())
        return web.json_response({"ok": True, "changed": sorted(clean), "restart": needs_restart,
                                  "backend": "supervisor" if st.has_supervisor() else "file"})

    r.add_get("/", index)
    r.add_get("/api/settings", get_settings)
    r.add_get("/api/frequencies", get_frequencies)
    r.add_get("/api/dongles", get_dongles)
    r.add_post("/api/settings", post_settings)
    r.add_get("/api/status", status)
    r.add_get("/api/devices", devices)
    r.add_get("/api/signals", signals)
    r.add_post("/api/devices/{uid}", update_device)
    r.add_delete("/api/devices/{uid}", delete_device)
    r.add_post("/api/mode", set_mode)
    r.add_post("/api/skip", skip)
    r.add_post("/api/radiosonde", radiosonde_now)
    r.add_post("/api/reset", reset)
    r.add_get("/api/export", export)
    return app
