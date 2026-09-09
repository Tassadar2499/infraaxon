import json
import os
import sqlite3
import threading
from pathlib import Path
from cryptography.fernet import Fernet


class Store:
    def __init__(self, path: str, key: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(key.encode())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, payload TEXT, PRIMARY KEY(kind,id))")
        self.db.commit()

    def put(self, kind, value):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload",
                (kind, value["id"], json.dumps(value)),
            )
        return value

    def get(self, kind, id):
        with self.lock:
            row = self.db.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.lock:
            rows = self.db.execute("SELECT payload FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete(self, kind, id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def close(self):
        self.db.close()


def configured_store():
    return Store(os.getenv("DATABASE_PATH", "/data/platform.db"), os.environ["ENCRYPTION_KEY"])


def public_component(component):
    return {k: v for k, v in component.items() if k != "encrypted_secrets"}
