from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .models import Proposal, now


def dumps(value):
    return json.dumps(value, default=str, separators=(",", ":"))


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, at TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS votes (proposal_id TEXT, model TEXT, round INTEGER, payload TEXT, PRIMARY KEY(proposal_id, model, round));
        CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE, broker TEXT, external_id TEXT, status TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, provider TEXT, at TEXT, cost INTEGER);
        CREATE INDEX IF NOT EXISTS usage_at ON usage(provider, at);
        CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, at TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS backtests (id INTEGER PRIMARY KEY, at TEXT, payload TEXT);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, dumps(value)))

    def event(self, kind, payload):
        self.db.execute("INSERT INTO events(at,kind,payload) VALUES (?,?,?)", (now().isoformat(), kind, dumps(payload)))

    def proposal(self, proposal: Proposal, status="RESEARCHING"):
        self.db.execute("INSERT INTO proposals(id,at,side,status,payload) VALUES(?,?,?,?,?)", (proposal.id, proposal.created_at.isoformat(), proposal.side, status, proposal.model_dump_json()))

    def proposal_status(self, proposal_id, status, reason=""):
        self.db.execute("UPDATE proposals SET status=?,reason=? WHERE id=?", (status, reason, proposal_id))

    def vote(self, proposal_id, model, round_number, value):
        self.db.execute("INSERT OR REPLACE INTO votes VALUES (?,?,?,?)", (proposal_id, model, round_number, dumps(value)))

    def save_evidence(self, evidence):
        self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?)", (evidence.id, evidence.model_dump_json()))

    def record_snapshot(self, portfolio):
        value = portfolio.model_dump(mode="json")
        self.set("portfolio", value)
        self.db.execute("INSERT INTO snapshots(at,payload) VALUES (?,?)", (portfolio.at.isoformat(), dumps(value)))

    def begin_order(self, proposal):
        stamp = now().isoformat()
        with self.transaction():
            if self.pending_orders():
                raise ValueError("An outstanding or uncertain order must reconcile first")
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", (proposal.id, proposal.id, proposal.broker, None, "SUBMITTING", proposal.model_dump_json(), stamp, stamp))

    def update_order(self, order_id, update):
        self.db.execute("UPDATE orders SET status=?,external_id=COALESCE(?,external_id),updated_at=? WHERE id=?", (update.status, update.external_id, now().isoformat(), order_id))
        self.event("order_update", {"id": order_id, **update.model_dump(mode="json")})

    def pending_orders(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM orders WHERE status IN ('SUBMITTING','SUBMITTED','PARTIAL','UNKNOWN')")]

    def recent(self, table, limit=60):
        if table not in {"events", "proposals", "orders", "snapshots", "backtests", "evidence"}:
            raise ValueError("Unknown table")
        rows = self.db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def votes_for(self, proposal_id):
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in self.db.execute("SELECT * FROM votes WHERE proposal_id=? ORDER BY round,model", (proposal_id,))]

    def buy_count_today(self):
        start = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self.db.execute("SELECT COUNT(*) FROM proposals WHERE side='BUY' AND at>=?", (start,)).fetchone()[0]

    def instrument_recently_reviewed(self, instrument_id, side):
        start = (now() - timedelta(hours=24)).isoformat()
        return any(json.loads(r[0])["instrument_id"] == instrument_id for r in self.db.execute("SELECT payload FROM proposals WHERE side=? AND at>=?", (side, start)))

    def next_nonce(self):
        with self.transaction():
            value = max(int(now().timestamp() * 1000), self.get("kraken_nonce", 0) + 1)
            self.set("kraken_nonce", value)
        return value
