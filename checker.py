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
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Terbukti reliable dari pengalaman lapangan: baca meta tag og:title,
# bukan class HTML yang gampang berubah / gagal match.
BANNED_MARKERS = [
    "this account has been banned",
    "this channel can't be displayed",
    "violates telegram's terms of service",
    "account was permanently banned",
    "spread pornographic content",
    "spread violent content",
]

# og:title generik yang muncul kalau t.me/<username> TIDAK menemukan apapun
GENERIC_TG_TITLES = {"telegram messenger", "telegram", ""}


def _get_og_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", property="og:title")
    return (tag.get("content") or "").strip() if tag else ""


async def check_telegram(client: httpx.AsyncClient, username: str) -> dict:
    """Return {'state': 'available'|'taken'|'banned'|'unknown', 'og_title': str, 'raw': str}"""
    url = f"https://t.me/{username}"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
    except Exception as e:
        return {"state": "unknown", "error": str(e), "og_title": "", "raw": ""}

    html = resp.text
    lower = html.lower()
    og_title = _get_og_title(html)

    if any(marker in lower for marker in BANNED_MARKERS):
        return {"state": "banned", "og_title": og_title, "raw": html[:800]}

    if og_title.lower() in GENERIC_TG_TITLES:
        # tidak ada preview user/channel/bot -> username belum dipakai siapapun
        return {"state": "available", "og_title": og_title, "raw": html[:800]}

    return {"state": "taken", "og_title": og_title, "raw": html[:800]}


async def check_fragment(client: httpx.AsyncClient, username: str) -> dict:
    """
    Return {'state': 'for_sale'|'owned_via_fragment'|'not_on_fragment'|'unknown', 'og_title': str, 'raw': str}

    Berdasarkan pola og:title di fragment.com/username/<username>:
      - "...auctions for usernames..." -> generic homepage -> tidak pernah lewat Fragment
      - "Buy @username" (awalan)       -> lagi dijual harga tetap di Fragment
      - mengandung "make an offer"     -> sudah dimiliki orang, hanya bisa nego swasta
    """
    url = f"https://fragment.com/username/{username}"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
    except Exception as e:
        return {"state": "unknown", "error": str(e), "og_title": "", "raw": ""}

    og_title = _get_og_title(resp.text)
    lower_title = og_title.lower()

    if "auctions for usernames" in lower_title:
        return {"state": "not_on_fragment", "og_title": og_title, "raw": resp.text[:800]}
    if lower_title.startswith("buy @"):
        return {"state": "for_sale", "og_title": og_title, "raw": resp.text[:800]}
    if "make an offer" in lower_title:
        return {"state": "owned_via_fragment", "og_title": og_title, "raw": resp.text[:800]}

    return {"state": "unknown", "og_title": og_title, "raw": resp.text[:800]}


async def debug_dump(client: httpx.AsyncClient, username: str) -> dict:
    """Dump beberapa sinyal dari t.me untuk kalibrasi manual (bukan dipakai di logika utama)."""
    url = f"https://t.me/{username}"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
    except Exception as e:
        return {"error": str(e)}

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    og_desc_tag = soup.find("meta", property="og:description")
    og_image_tag = soup.find("meta", property="og:image")
    has_page_photo = soup.select_one(".tgme_page_photo") is not None
    has_action_button = soup.select_one(".tgme_action_button_new") is not None
    body_text = soup.get_text(" ", strip=True)

    return {
        "status_code": resp.status_code,
        "title": title_tag.get_text(strip=True) if title_tag else "(tidak ada)",
        "og_description": (og_desc_tag.get("content") if og_desc_tag else "(tidak ada)"),
        "og_image": (og_image_tag.get("content") if og_image_tag else "(tidak ada)"),
        "has_page_photo": has_page_photo,
        "has_action_button": has_action_button,
        "body_preview": body_text[:500],
    }
    """
    Gabungkan hasil t.me + fragment.com jadi satu status final. Fragment dicek
    DULU karena kalau sudah kelihatan for_sale/owned di Fragment, itu paling
    definitif -- t.me tidak perlu jadi penentu akhir untuk kasus itu.

      - FRAGMENT       -> sedang dijual (fixed price) di Fragment
      - TAKEN          -> sudah dipakai orang lain (baik lewat Fragment ataupun tidak)
      - BANNED         -> akun/channel kena banned
      - AVAILABLE      -> belum dipakai siapa-siapa & tidak nyangkut di Fragment
      - UNKNOWN        -> gagal fetch / tidak bisa dipastikan
    """
    fg = await check_fragment(client, username)
    tg = await check_telegram(client, username)

    if fg["state"] == "for_sale":
        final = "FRAGMENT"
    elif fg["state"] == "owned_via_fragment":
        final = "TAKEN"
    elif tg["state"] == "banned":
        final = "BANNED"
    elif tg["state"] == "available" and fg["state"] in ("not_on_fragment", "unknown"):
        final = "AVAILABLE"
    elif tg["state"] == "taken":
        final = "TAKEN"
    elif tg["state"] == "available":
        # t.me bilang available tapi fragment ambigu -> jangan buru-buru bilang available
        final = "UNKNOWN"
    else:
        final = "UNKNOWN"

    return {"final": final, "telegram": tg, "fragment": fg}
