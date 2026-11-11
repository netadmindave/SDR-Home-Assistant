"""aiohttp web UI + JSON API (works behind HA ingress: all URLs are relative)."""
import json
from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).with_name("static")


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
        if after["source"] == "auto_rx":
            db.update_device(uid, approved=0)  # sondes publish via the shared tracker device
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
            raise web.HTTPBadRequest(text="mode must be survey or monitor")
        return web.json_response({"mode": scanner.mode})

    async def radiosonde_now(req):
        body = await req.json() if req.can_read_body else {}
        minutes = float(body.get("minutes", 120))
        if minutes <= 0:
            scanner.rs_force_until = 0.0
            scanner.skip()
        else:
            scanner.force_radiosonde(minutes)
        return web.json_response({"ok": True})

    async def skip(_):
        scanner.skip()
        return web.json_response({"ok": True})

    async def reset(_):
        db.reset_survey()
        scanner.note("Survey data reset (approved devices kept)")
        return web.json_response({"ok": True})

    async def export(_):
        data = {"devices": db.devices(), "signals": db.signals(1), "slots": db.slots()}
        return web.json_response(data, headers={
            "Content-Disposition": "attachment; filename=rtlsdr_survey.json"})

    r.add_get("/", index)
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
