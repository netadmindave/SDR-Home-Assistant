"""MQTT publishing: raw survey events, approved-device state, and HA discovery."""
import datetime
import json
import logging

import paho.mqtt.client as mqtt

from classify import FIELD_MAP, SKIP_ENTITY, entity_name
from radiosonde import TRACKER_FIELDS

log = logging.getLogger("mqtt")


class MQTTOut:
    def __init__(self, opts, db):
        self.o = opts
        self.db = db
        self.base = opts["base_topic"].rstrip("/")
        self.disc = opts["discovery_prefix"].rstrip("/")
        self.connected = False
        self.client = None
        self._announced = set()   # uid -> discovery already sent this session
        if not opts.get("mqtt_host"):
            log.warning("No MQTT broker configured; running without MQTT")
            return
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rtlsdr-survey")
        except AttributeError:  # paho < 2.0
            self.client = mqtt.Client(client_id="rtlsdr-survey")
        if opts.get("mqtt_user"):
            self.client.username_pw_set(opts["mqtt_user"], opts.get("mqtt_password") or None)
        self.client.will_set(f"{self.base}/status", "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(2, 60)
        self.client.connect_async(opts["mqtt_host"], int(opts["mqtt_port"]), keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, rc, *args):
        ok = (rc == 0) if isinstance(rc, int) else not getattr(rc, "is_failure", False)
        self.connected = ok
        if not ok:
            log.error("MQTT connect failed: %s", rc)
            return
        log.info("MQTT connected to %s", self.o["mqtt_host"])
        client.publish(f"{self.base}/status", "online", retain=True)
        self._announced.clear()
        for d in self.db.approved():
            self.announce(d)
        if self.o.get("radiosonde_enabled") and self.o.get("radiosonde_publish_ha"):
            self.announce_radiosonde()

    def _on_disconnect(self, *args):
        self.connected = False
        log.warning("MQTT disconnected")

    def pub(self, topic, payload, retain=False):
        if not self.client:
            return
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, default=str)
        self.client.publish(topic, payload, retain=retain)

    # ------------------------------------------------------------ events
    def raw_event(self, dev, raw):
        if self.o.get("publish_raw"):
            self.pub(f"{self.base}/survey/{dev['uid']}", {
                "key": dev["key"], "category": dev["category"], "slot": dev["slot"], "event": raw})

    def device_state(self, dev, state):
        if not dev.get("approved"):
            return
        if dev["uid"] not in self._announced:
            self.announce(dev)
        payload = dict(state)
        payload["last_seen"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        self.pub(self._state_topic(dev), payload, retain=True)

    def status(self, payload):
        self.pub(f"{self.base}/scanner", payload, retain=True)

    # ------------------------------------------------------------ discovery
    def _state_topic(self, dev):
        return f"{self.base}/devices/{dev['uid']}/state"

    def _components(self, dev):
        state = json.loads(dev.get("last_state") or "{}")
        comps = []
        for field, val in state.items():
            if field in SKIP_ENTITY:
                continue
            if field in FIELD_MAP:
                comps.append((field, FIELD_MAP[field]))
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                comps.append((field, ("sensor", None, None, "measurement", None)))
        comps.append(("last_seen", ("sensor", "timestamp", None, None, "diagnostic")))
        return comps

    def announce(self, dev):
        if not self.client:
            return
        name = dev.get("name") or f"{dev['model']} {dev['dev_id']}"
        device = {
            "identifiers": [f"rtlsdr_{dev['uid']}"],
            "name": name,
            "model": dev["model"],
            "manufacturer": f"via {dev['source']}",
        }
        st = self._state_topic(dev)
        for field, (comp, dclass, unit, sclass, ecat) in self._components(dev):
            obj = f"{dev['uid']}_{field}"
            cfg = {
                "name": entity_name(field) if field != "last_seen" else "Last seen",
                "unique_id": f"rtlsdr_{obj}",
                "object_id": f"rtlsdr_{obj}",
                "state_topic": st,
                "availability_topic": f"{self.base}/status",
                "device": device,
            }
            if comp == "binary_sensor":
                if field == "battery_ok":   # HA battery: on == low
                    cfg["value_template"] = "{{ 'OFF' if (value_json.battery_ok|int(1)) == 1 else 'ON' }}"
                else:
                    cfg["value_template"] = (
                        "{{ 'ON' if (value_json.%s|int(0)) != 0 else 'OFF' }}" % field)
            else:
                cfg["value_template"] = "{{ value_json.%s }}" % field
            if dclass:
                cfg["device_class"] = dclass
            if unit:
                cfg["unit_of_measurement"] = unit
            if sclass:
                cfg["state_class"] = sclass
            if ecat:
                cfg["entity_category"] = ecat
            self.pub(f"{self.disc}/{comp}/rtlsdr_{dev['uid']}/{field}/config", cfg, retain=True)
        self._announced.add(dev["uid"])
        log.info("HA discovery published for %s (%s)", name, dev["key"])

    def unannounce(self, dev):
        if not self.client:
            return
        for field, (comp, *_r) in self._components(dev):
            self.pub(f"{self.disc}/{comp}/rtlsdr_{dev['uid']}/{field}/config", "", retain=True)
        self.pub(self._state_topic(dev), "", retain=True)
        self._announced.discard(dev["uid"])

    # ------------------------------------------------------------ radiosonde tracker
    def announce_radiosonde(self):
        if not self.client:
            return
        device = {"identifiers": ["rtlsdr_radiosonde_tracker"], "name": "Radiosonde tracker",
                  "model": "radiosonde_auto_rx", "manufacturer": "via rtlsdr_survey"}
        st = f"{self.base}/radiosonde/state"
        common = {"availability_topic": f"{self.base}/status", "device": device}
        for field, (comp, dclass, unit, sclass, ecat, name) in TRACKER_FIELDS.items():
            cfg = dict(common, name=name, unique_id=f"rtlsdr_radiosonde_{field}",
                       object_id=f"radiosonde_{field}", state_topic=st,
                       value_template="{{ value_json.%s }}" % field)
            if dclass:
                cfg["device_class"] = dclass
            if unit:
                cfg["unit_of_measurement"] = unit
            if sclass:
                cfg["state_class"] = sclass
            if ecat:
                cfg["entity_category"] = ecat
            self.pub(f"{self.disc}/{comp}/rtlsdr_radiosonde/{field}/config", cfg, retain=True)
        tracker = dict(common, name="Position", unique_id="rtlsdr_radiosonde_position",
                       object_id="radiosonde_position", source_type="gps",
                       json_attributes_topic=f"{self.base}/radiosonde/position")
        self.pub(f"{self.disc}/device_tracker/rtlsdr_radiosonde/position/config", tracker, retain=True)

    def radiosonde_state(self, state):
        if not (self.o.get("radiosonde_publish_ha") and self.client):
            return
        payload = dict(state)
        payload["last_seen"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        self.pub(f"{self.base}/radiosonde/state", payload, retain=True)
        if "latitude" in state and "longitude" in state:
            self.pub(f"{self.base}/radiosonde/position", {
                "latitude": state["latitude"], "longitude": state["longitude"],
                "gps_accuracy": 30, "altitude": state.get("altitude_m"),
                "serial": state.get("serial")}, retain=True)
