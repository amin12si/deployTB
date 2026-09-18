"""Storage for the bot: which Railway accounts are known, and which panels
have been deployed on each. Separate from any deployed panel's own DB."""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import crypto

DB_PATH = os.environ.get("BOT_DB_PATH", "bot.db")

_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("PRAGMA journal_mode=WAL")
_db.row_factory = sqlite3.Row


def init_db():
    _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            token_encrypted TEXT NOT NULL,
            workspace_id TEXT,
            last_valid INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS panels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            label TEXT NOT NULL,
            railway_project_id TEXT NOT NULL,
            railway_service_id TEXT NOT NULL,
            railway_environment_id TEXT NOT NULL,
            domain TEXT,
            admin_password TEXT NOT NULL,
            alerts_enabled INTEGER NOT NULL DEFAULT 1,
            last_health_ok INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        """
    )
    _db.commit()
    # migrate older DBs created before workspace_id existed
    cols = [r["name"] for r in _db.execute("PRAGMA table_info(accounts)").fetchall()]
    if "workspace_id" not in cols:
        _db.execute("ALTER TABLE accounts ADD COLUMN workspace_id TEXT")
        _db.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── accounts ────────────────────────────────────────────────────────────

def add_account(label: str, token: str, workspace_id: str | None = None) -> int:
    cur = _db.execute(
        "INSERT INTO accounts (label, token_encrypted, workspace_id, last_valid, created_at) VALUES (?, ?, ?, 1, ?)",
        (label, crypto.encrypt(token), workspace_id, now()),
    )
    _db.commit()
    return cur.lastrowid


def list_accounts():
    return _db.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()


def get_account(account_id: int):
    return _db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


def get_account_token(account_id: int) -> str:
    row = get_account(account_id)
    return crypto.decrypt(row["token_encrypted"])


def set_account_workspace_id(account_id: int, workspace_id: str | None):
    _db.execute("UPDATE accounts SET workspace_id = ? WHERE id = ?", (workspace_id, account_id))
    _db.commit()


def set_account_validity(account_id: int, valid: bool):
    _db.execute("UPDATE accounts SET last_valid = ? WHERE id = ?", (1 if valid else 0, account_id))
    _db.commit()


def delete_account(account_id: int):
    _db.execute("DELETE FROM panels WHERE account_id = ?", (account_id,))
    _db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    _db.commit()


def count_panels_for_account(account_id: int) -> int:
    row = _db.execute("SELECT COUNT(*) c FROM panels WHERE account_id = ?", (account_id,)).fetchone()
    return row["c"]


# ── panels ──────────────────────────────────────────────────────────────

def add_panel(account_id: int, label: str, project_id: str, service_id: str,
              environment_id: str, domain: str, admin_password: str) -> int:
    cur = _db.execute(
        "INSERT INTO panels (account_id, label, railway_project_id, railway_service_id, "
        "railway_environment_id, domain, admin_password, alerts_enabled, last_health_ok, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
        (account_id, label, project_id, service_id, environment_id, domain, admin_password, now()),
    )
    _db.commit()
    return cur.lastrowid


def list_panels():
    return _db.execute("SELECT * FROM panels ORDER BY created_at").fetchall()


def list_panels_for_account(account_id: int):
    return _db.execute("SELECT * FROM panels WHERE account_id = ? ORDER BY created_at", (account_id,)).fetchall()


def get_panel(panel_id: int):
    return _db.execute("SELECT * FROM panels WHERE id = ?", (panel_id,)).fetchone()


def update_panel_password(panel_id: int, new_password: str):
    _db.execute("UPDATE panels SET admin_password = ? WHERE id = ?", (new_password, panel_id))
    _db.commit()


def set_panel_alerts(panel_id: int, enabled: bool):
    _db.execute("UPDATE panels SET alerts_enabled = ? WHERE id = ?", (1 if enabled else 0, panel_id))
    _db.commit()


def set_panel_health(panel_id: int, ok: bool):
    _db.execute("UPDATE panels SET last_health_ok = ? WHERE id = ?", (1 if ok else 0, panel_id))
    _db.commit()


def delete_panel(panel_id: int):
    _db.execute("DELETE FROM panels WHERE id = ?", (panel_id,))
    _db.commit()


# ── backup / import ────────────────────────────────────────────────────
# Tokens stay encrypted in the export — restoring on a fresh deploy only
# works if that deploy uses the same ENCRYPTION_KEY.

def export_all() -> str:
    accounts = [dict(r) for r in _db.execute("SELECT * FROM accounts").fetchall()]
    panels = [dict(r) for r in _db.execute("SELECT * FROM panels").fetchall()]
    return json.dumps({"version": 1, "exported_at": now(), "accounts": accounts, "panels": panels}, indent=2)


def import_all(blob: str, wipe_existing: bool = False):
    data = json.loads(blob)
    if data.get("version") != 1:
        raise ValueError("unrecognized backup format")

    if wipe_existing:
        _db.execute("DELETE FROM panels")
        _db.execute("DELETE FROM accounts")

    id_map = {}
    for acc in data["accounts"]:
        # sanity check the token decrypts with this deploy's ENCRYPTION_KEY before committing
        crypto.decrypt(acc["token_encrypted"])
        cur = _db.execute(
            "INSERT INTO accounts (label, token_encrypted, workspace_id, last_valid, created_at) VALUES (?, ?, ?, ?, ?)",
            (acc["label"], acc["token_encrypted"], acc.get("workspace_id"), acc["last_valid"], acc["created_at"]),
        )
        id_map[acc["id"]] = cur.lastrowid

    imported_panels = 0
    for p in data["panels"]:
        new_account_id = id_map.get(p["account_id"])
        if new_account_id is None:
            continue
        _db.execute(
            "INSERT INTO panels (account_id, label, railway_project_id, railway_service_id, "
            "railway_environment_id, domain, admin_password, alerts_enabled, last_health_ok, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_account_id, p["label"], p["railway_project_id"], p["railway_service_id"],
             p["railway_environment_id"], p["domain"], p["admin_password"],
             p["alerts_enabled"], p["last_health_ok"], p["created_at"]),
        )
        imported_panels += 1

    _db.commit()
    return len(id_map), imported_panels
