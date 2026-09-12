"""
Modul pengecekan status username -- TANPA butuh akun Telegram sama sekali.

Dua sumber yang dicek:
1. https://t.me/<username>  -> halaman preview publik Telegram
   Dipakai untuk menentukan: available / taken / banned
2. https://fragment.com/username/<username> -> marketplace Fragment
   Dipakai untuk menentukan apakah username sedang di-auction/dijual di Fragment

PENTING -- soal akurasi:
Deteksi di bawah ini berbasis pola HTML/teks yang umum ditemukan di kedua
halaman tersebut. Karena Telegram & Fragment bisa mengubah markup halaman
sewaktu-waktu, ada kemungkinan deteksi meleset untuk kasus tertentu.

Gunakan command /raw <username> di bot untuk melihat potongan HTML mentah
dan sesuaikan kata kunci di fungsi di bawah kalau ternyata ada status yang
salah baca. Sebelum dipakai produksi, coba dulu ke beberapa username yang
statusnya sudah Anda ketahui pasti (satu yang jelas available, satu yang
jelas taken, satu yang jelas banned) untuk kalibrasi.
"""

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

BANNED_MARKERS = [
    "this account has been banned",
    "this channel can't be displayed because it violated",
    "violates telegram's terms of service",
    "account was permanently banned",
    "account is unavailable",
]


async def check_telegram(client: httpx.AsyncClient, username: str) -> dict:
    """Return {'state': 'available'|'taken'|'banned'|'unknown', 'raw': str}"""
    url = f"https://t.me/{username}"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
    except Exception as e:
        return {"state": "unknown", "error": str(e), "raw": ""}

    html = resp.text
    lower = html.lower()

    if any(marker in lower for marker in BANNED_MARKERS):
        return {"state": "banned", "raw": html[:800]}

    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".tgme_page_title")
    if title_el and title_el.get_text(strip=True):
        return {"state": "taken", "raw": html[:800]}

    return {"state": "available", "raw": html[:800]}


async def check_fragment(client: httpx.AsyncClient, username: str) -> dict:
    """Return {'state': 'on_auction'|'for_sale'|'sold'|'not_on_fragment'|'unknown', 'raw': str}"""
    url = f"https://fragment.com/username/{username}"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
    except Exception as e:
        return {"state": "unknown", "error": str(e), "raw": ""}

    if resp.status_code == 404:
        return {"state": "not_on_fragment", "raw": ""}

    lower = resp.text.lower()

    if "on auction" in lower:
        return {"state": "on_auction", "raw": resp.text[:800]}
    if "purchased on" in lower or ">sold<" in lower:
        return {"state": "sold", "raw": resp.text[:800]}
    if "for sale" in lower:
        return {"state": "for_sale", "raw": resp.text[:800]}

    return {"state": "not_on_fragment", "raw": resp.text[:800]}


async def check_username_full(client: httpx.AsyncClient, username: str) -> dict:
    """
    Gabungkan hasil t.me + fragment.com jadi satu status final:
      - FRAGMENT       -> sedang di-auction/dijual di Fragment
      - BANNED         -> akun/channel kena banned
      - AVAILABLE      -> belum dipakai siapa-siapa, bisa langsung di-keep
      - TAKEN          -> sudah dipakai orang lain
      - UNKNOWN        -> gagal fetch / tidak bisa ditentukan
    """
    tg = await check_telegram(client, username)
    fg = await check_fragment(client, username)

    if fg["state"] in ("on_auction", "for_sale", "sold"):
        final = "FRAGMENT"
    elif tg["state"] == "banned":
        final = "BANNED"
    elif tg["state"] == "available":
        final = "AVAILABLE"
    elif tg["state"] == "taken":
        final = "TAKEN"
    else:
        final = "UNKNOWN"

    return {"final": final, "telegram": tg, "fragment": fg}
