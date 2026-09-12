"""
Penyimpanan sederhana berbasis file JSON untuk watchlist username.

CATATAN PENTING soal Railway:
Filesystem di Railway bersifat EPHEMERAL pada plan gratis/trial -- artinya
file data.json ini BISA HILANG setiap kali Anda melakukan redeploy (push
ulang kode / restart service dari awal image baru). Untuk pemakaian normal
(bot jalan terus tanpa redeploy), data akan tetap aman.

Kalau nanti butuh data yang benar-benar persisten walau redeploy, opsi
selanjutnya: pakai Railway Volume (butuh upgrade plan) atau pindah ke
database eksternal gratis (misal Supabase/Neon Postgres free tier).
Untuk sekarang JSON sudah cukup untuk mulai.
"""

import json
import os
from threading import Lock

DATA_FILE = os.getenv("DATA_FILE", "data.json")
_lock = Lock()


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"usernames": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"usernames": {}}


def _save(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_usernames(usernames: list[str]) -> list[str]:
    """Tambahkan username baru ke watchlist. Return daftar yang benar-benar baru ditambahkan."""
    with _lock:
        data = _load()
        added = []
        for u in usernames:
            u = u.strip().lstrip("@").lower()
            if not u:
                continue
            if u not in data["usernames"]:
                data["usernames"][u] = {
                    "last_state": None,
                    "last_checked": None,
                }
                added.append(u)
        _save(data)
        return added


def remove_username(username: str) -> bool:
    username = username.strip().lstrip("@").lower()
    with _lock:
        data = _load()
        if username in data["usernames"]:
            del data["usernames"][username]
            _save(data)
            return True
        return False


def get_watchlist() -> dict:
    with _lock:
        return _load()["usernames"]


def update_state(username: str, state: str, checked_at: str) -> None:
    with _lock:
        data = _load()
        if username in data["usernames"]:
            data["usernames"][username]["last_state"] = state
            data["usernames"][username]["last_checked"] = checked_at
            _save(data)
