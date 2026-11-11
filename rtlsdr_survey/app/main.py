"""Entry point: load options + plan, start MQTT, scanner and web UI."""
import asyncio
import logging
import signal

from aiohttp import web

from config import data_dir, load_options
from db import DB
from mqtt_out import MQTTOut
from manager import Manager
from web import build_app


async def main():
    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, str(opts["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("main")
    safe = {k: ("***" if "password" in k and v else v) for k, v in opts.items() if k != "_user"}
    log.info("Options: %s", safe)

    db = DB(str(data_dir() / "survey.db"))
    mqtt = MQTTOut(opts, db)
    scanner = Manager(opts, db, mqtt)   # one Scanner per SDR inside

    runner = web.AppRunner(build_app(scanner, db, mqtt), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(opts["web_port"])).start()
    log.info("Web UI on port %s", opts["web_port"])
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await scanner.run()
    except asyncio.CancelledError:
        log.info("Stopping")
    finally:
        await scanner.stop()
        mqtt.pub(f"{mqtt.base}/status", "offline", retain=True)
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
