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
(preview terpotong) atau /rawfull <username> untuk mengambil HTML LENGKAP
sebagai file .html -- ini penting kalau ada status yang salah baca, karena
marker yang dicari mungkin ada di luar batas potongan preview.

Sebelum dipakai produksi, coba dulu ke beberapa username yang statusnya
sudah Anda ketahui pasti (satu yang jelas available, satu yang jelas taken,
satu yang jelas banned) untuk kalibrasi.
"""

import re

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BANNED_MARKERS = [
    "this account has been banned",
    "this channel can't be displayed",
    "violates telegram's terms of service",
    "account was permanently banned",
    "spread pornographic content",
    "spread violent content",
]

# Pola: "If you have Telegram, you can [contact|view and join|launch|join] X right away."
# Kalau X == username itu sendiri -> generic fallback -> username TIDAK terdaftar.
# Kalau X == nama lain (nama tampilan asli akun) -> username SUDAH dipakai.
# PENTING: harus dijangkar ke "If you have Telegram, you can..." -- kalau cuma
# cari kata "contact" saja, dia kepancing sama judul halaman "Telegram: Contact
# @username" yang SELALU ada di awal body, sebelum kalimat yang sebenarnya.
RIGHT_AWAY_RE = re.compile(
    r"if you have telegram\s*,?\s*you can\s+(?:contact|view and join|launch|join)\s+(.+?)\s+right away",
    re.IGNORECASE,
)


def _get_og_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", property="og:title")
    return (tag.get("content") or "").strip() if tag else ""


async def check_telegram(client: httpx.AsyncClient, username: str) -> dict:
    """Return {'state': 'available'|'taken'|'banned'|'unknown', 'raw': str}

    Catatan kalibrasi (dari data nyata): og:title SELALU berformat generik
    "Telegram: Contact @username" baik untuk username yang ada maupun yang
    tidak -- jadi og:title TIDAK dipakai untuk penentuan status. Sinyal asli
    ada di body: kalimat "...you can contact X right away" memakai username
    itu sendiri sebagai fallback kalau akun tidak ada, tapi memakai nama
    tampilan asli akun kalau akun ADA. Dikuatkan dengan cek foto profil &
    og:description (keduanya kosong/tidak ada kalau username belum dipakai).

    CATATAN PENTING soal BANNED: sejauh ini terbukti username yang di-banned
    BISA menghasilkan pola body yang SAMA PERSIS dengan username yang benar-
    benar available (generic fallback "you can contact X right away" dengan
    X == username itu sendiri, tanpa foto, tanpa og:description). Artinya
    BANNED_MARKERS di atas belum tentu lengkap/akurat untuk semua kasus --
    Telegram bisa memakai frasa lain untuk halaman banned yang belum masuk
    daftar. Kalau ketemu username yang harusnya banned tapi kebaca available,
    gunakan /rawfull untuk ambil HTML utuh, cari frasa pembeda yang sebenarnya,
    lalu tambahkan ke BANNED_MARKERS.
    """
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
    og_desc_tag = soup.find("meta", property="og:description")
    og_description = (og_desc_tag.get("content") or "").strip() if og_desc_tag else ""
    has_page_photo = soup.select_one(".tgme_page_photo") is not None
    body_text = soup.get_text(" ", strip=True)

    m = RIGHT_AWAY_RE.search(body_text)
    mentioned_name = m.group(1).strip() if m else None
    name_matches_username = (
        mentioned_name is not None
        and mentioned_name.lower().lstrip("@").rstrip(".").strip() == username.lower()
    )

    # Sinyal kuat akun ADA: nama di kalimat "right away" beda dari username,
    # ATAU ada foto profil, ATAU ada og:description (bio).
    exists_signal = (mentioned_name is not None and not name_matches_username) or has_page_photo or bool(og_description)

    state = "taken" if exists_signal else "available"
    return {
        "state": state,
        "raw": html[:800],
        "mentioned_name": mentioned_name,
        "has_page_photo": has_page_photo,
        "og_description": og_description,
    }


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
    """Dump beberapa sinyal dari t.me + fragment.com untuk kalibrasi manual.
    CATATAN: body_preview di sini DIPOTONG (500/700 karakter) hanya untuk
    ditampilkan enak di chat. Deteksi asli (check_telegram/check_fragment)
    tetap scan HTML PENUH, bukan potongan ini. Kalau butuh HTML lengkap
    untuk cari marker baru, pakai fetch_raw_html() / command /rawfull.
    """
    tg_url = f"https://t.me/{username}"
    fg_url = f"https://fragment.com/username/{username}"

    result = {}

    try:
        resp = await client.get(tg_url, headers=HEADERS, timeout=15, follow_redirects=True)
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        og_desc_tag = soup.find("meta", property="og:description")
        og_image_tag = soup.find("meta", property="og:image")
        has_page_photo = soup.select_one(".tgme_page_photo") is not None
        has_action_button = soup.select_one(".tgme_action_button_new") is not None
        body_text = soup.get_text(" ", strip=True)
        result["tme"] = {
            "status_code": resp.status_code,
            "title": title_tag.get_text(strip=True) if title_tag else "(tidak ada)",
            "og_description": (og_desc_tag.get("content") if og_desc_tag else "(tidak ada)"),
            "og_image": (og_image_tag.get("content") if og_image_tag else "(tidak ada)"),
            "has_page_photo": has_page_photo,
            "has_action_button": has_action_button,
            "body_preview": body_text[:500],
        }
    except Exception as e:
        result["tme"] = {"error": str(e)}

    try:
        resp2 = await client.get(fg_url, headers=HEADERS, timeout=15, follow_redirects=True)
        html2 = resp2.text
        soup2 = BeautifulSoup(html2, "html.parser")
        title_tag2 = soup2.find("title")
        og_title_tag2 = soup2.find("meta", property="og:title")
        og_desc_tag2 = soup2.find("meta", property="og:description")
        body_text2 = soup2.get_text(" ", strip=True)
        result["fragment"] = {
            "status_code": resp2.status_code,
            "final_url": str(resp2.url),
            "title": title_tag2.get_text(strip=True) if title_tag2 else "(tidak ada)",
            "og_title": (og_title_tag2.get("content") if og_title_tag2 else "(tidak ada)"),
            "og_description": (og_desc_tag2.get("content") if og_desc_tag2 else "(tidak ada)"),
            "body_preview": body_text2[:700],
        }
    except Exception as e:
        result["fragment"] = {"error": str(e)}

    return result


async def fetch_raw_html(client: httpx.AsyncClient, username: str) -> dict:
    """Ambil HTML MENTAH LENGKAP (tanpa dipotong sama sekali) dari t.me dan
    fragment.com. Dipakai untuk kalibrasi manual: bandingkan HTML username
    yang sudah pasti banned vs yang sudah pasti available, cari frasa
    pembeda yang sebenarnya, lalu tambahkan ke BANNED_MARKERS di atas.

    Return: {'tme_html': str, 'fragment_html': str}
    (kalau gagal fetch salah satu, isinya string '[error: ...]')
    """
    result = {}
    tg_url = f"https://t.me/{username}"
    fg_url = f"https://fragment.com/username/{username}"

    try:
        resp = await client.get(tg_url, headers=HEADERS, timeout=15, follow_redirects=True)
        result["tme_html"] = resp.text
    except Exception as e:
        result["tme_html"] = f"[error: {e}]"

    try:
        resp2 = await client.get(fg_url, headers=HEADERS, timeout=15, follow_redirects=True)
        result["fragment_html"] = resp2.text
    except Exception as e:
        result["fragment_html"] = f"[error: {e}]"

    return result


async def check_username_full(client: httpx.AsyncClient, username: str) -> dict:
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
