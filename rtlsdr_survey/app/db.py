"""SQLite store for devices, undecoded energy peaks, and per-slot coverage."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    uid TEXT PRIMARY KEY,
    key TEXT UNIQUE,
    source TEXT, model TEXT, dev_id TEXT, channel TEXT,
    category TEXT, slot TEXT, freq_mhz REAL,
    first_seen REAL, last_seen REAL, count INTEGER DEFAULT 0,
    rssi REAL, snr REAL,
    last_state TEXT, last_raw TEXT,
    approved INTEGER DEFAULT 0, name TEXT, ignored INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS signals (
    bucket_hz INTEGER PRIMARY KEY,
    band TEXT, hits INTEGER DEFAULT 0, obs INTEGER DEFAULT 0,
    max_snr REAL DEFAULT 0, sum_snr REAL DEFAULT 0,
    first_seen REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS slots (
    name TEXT PRIMARY KEY,
    kind TEXT, decoder TEXT, freq_desc TEXT,
    runs INTEGER DEFAULT 0, dwell_total REAL DEFAULT 0,
    events INTEGER DEFAULT 0, frames INTEGER DEFAULT 0, frames_decoded INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0, last_run REAL, last_error TEXT
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.c.executescript(SCHEMA)
            self.c.execute("PRAGMA journal_mode=WAL")

    # ---------------- settings
    def get_setting(self, k, default=None):
        with self.lock:
            r = self.c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_setting(self, k, v):
        with self.lock, self.c:
            self.c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, str(v)))

    # ---------------- devices
    def upsert_device(self, *, uid, key, source, model, dev_id, channel, category, slot,
                      freq_mhz, rssi, snr, state, raw):
        now = time.time()
        with self.lock, self.c:
            cur = self.c.execute("SELECT uid FROM devices WHERE uid=?", (uid,)).fetchone()
            if cur is None:
                self.c.execute(
                    """INSERT INTO devices(uid,key,source,model,dev_id,channel,category,slot,freq_mhz,
                       first_seen,last_seen,count,rssi,snr,last_state,last_raw)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                    (uid, key, source, model, dev_id, channel, category, slot, freq_mhz,
                     now, now, rssi, snr, json.dumps(state), json.dumps(raw)))
                return True
            self.c.execute(
                """UPDATE devices SET last_seen=?, count=count+1, slot=?, category=?,
                   freq_mhz=COALESCE(?,freq_mhz), rssi=COALESCE(?,rssi), snr=COALESCE(?,snr),
                   last_state=?, last_raw=? WHERE uid=?""",
                (now, slot, category, freq_mhz, rssi, snr, json.dumps(state), json.dumps(raw), uid))
            return False

    def device(self, uid):
        with self.lock:
            r = self.c.execute("SELECT * FROM devices WHERE uid=?", (uid,)).fetchone()
        return dict(r) if r else None

    def devices(self):
        with self.lock:
            rows = self.c.execute("SELECT * FROM devices ORDER BY approved DESC, count DESC").fetchall()
        return [dict(r) for r in rows]

    def approved(self):
        with self.lock:
            rows = self.c.execute("SELECT * FROM devices WHERE approved=1").fetchall()
        return [dict(r) for r in rows]

    def update_device(self, uid, **fields):
        allowed = {"approved", "name", "ignored"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self.lock, self.c:
            self.c.execute(
                f"UPDATE devices SET {', '.join(f'{k}=?' for k in sets)} WHERE uid=?",
                (*sets.values(), uid))

    def delete_device(self, uid):
        with self.lock, self.c:
            self.c.execute("DELETE FROM devices WHERE uid=?", (uid,))

    # ---------------- signals
    def merge_signals(self, band, buckets):
        """buckets: {hz: [hits, obs, max_snr, sum_snr, last_hit_ts]}"""
        now = time.time()
        with self.lock, self.c:
            for hz, (hits, obs, mx, sm, last) in buckets.items():
                self.c.execute(
                    """INSERT INTO signals(bucket_hz,band,hits,obs,max_snr,sum_snr,first_seen,last_seen)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(bucket_hz) DO UPDATE SET
                         hits=hits+excluded.hits, obs=obs+excluded.obs,
                         max_snr=MAX(max_snr, excluded.max_snr), sum_snr=sum_snr+excluded.sum_snr,
                         first_seen=COALESCE(first_seen, excluded.first_seen),
                         last_seen=COALESCE(excluded.last_seen, last_seen), band=excluded.band""",
                    (hz, band, hits, obs, mx, sm, last if hits else None, last if hits else None))
        return now

    def signals(self, min_hits=1):
        with self.lock:
            rows = self.c.execute(
                "SELECT * FROM signals WHERE hits>=? ORDER BY hits DESC LIMIT 2000", (min_hits,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- slots
    def slot_run(self, slot, dwell, events=0, frames=0, frames_decoded=0, error=None):
        fd = slot.get("range") or f"{slot.get('freq', 0) / 1e6:.4f} MHz"
        with self.lock, self.c:
            self.c.execute(
                """INSERT INTO slots(name,kind,decoder,freq_desc) VALUES(?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, decoder=excluded.decoder,
                   freq_desc=excluded.freq_desc""",
                (slot["name"], slot["kind"], slot["decoder"], fd))
            self.c.execute(
                """UPDATE slots SET runs=runs+1, dwell_total=dwell_total+?, events=events+?,
                   frames=frames+?, frames_decoded=frames_decoded+?, last_run=?,
                   errors=errors+?, last_error=? WHERE name=?""",
                (dwell, events, frames, frames_decoded, time.time(), 1 if error else 0, error,
                 slot["name"]))

    def slots(self):
        with self.lock:
            return [dict(r) for r in self.c.execute("SELECT * FROM slots").fetchall()]

    def reset_survey(self):
        with self.lock, self.c:
            self.c.execute("DELETE FROM devices WHERE approved=0")
            self.c.execute("DELETE FROM signals")
            self.c.execute("DELETE FROM slots")
