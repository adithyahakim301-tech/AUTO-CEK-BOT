"""
checker.py
Modul pengecekan status username Telegram -- pakai MTProto (Telethon) untuk
status inti (available/taken/banned) dan scraping fragment.com untuk deteksi
listing/lelang.

Status yang dikembalikan (lihat class Status):
- AVAILABLE : username kosong, bisa langsung diklaim
- FRAGMENT  : username terdaftar/dilelang/dijual di fragment.com
- TAKEN     : username sudah dipakai orang lain
- BANNED    : Telegram menandai entity ini `restricted` (kena TOS violation),
              ATAU resolveUsername melempar UsernameInvalidError padahal
              formatnya valid -- keduanya sinyal RESMI dari Telegram, bukan
              tebakan dari teks halaman publik
- INVALID   : format username tidak valid (terlalu pendek / karakter aneh)
- ERROR     : gagal dicek (network/API error)

KENAPA TIDAK LAGI SCRAPING t.me:
Sudah dibuktikan lewat perbandingan HTML mentah: halaman publik t.me
menampilkan markup YANG SAMA PERSIS untuk username yang benar-benar belum
pernah dipakai (truly available) MAUPUN yang sudah kena banned Telegram --
keduanya fallback ke "If you have Telegram, you can contact X right away"
generik. Jadi t.me TIDAK BISA dipakai untuk membedakan available vs banned,
titik. Sinyal yang valid hanya ada di MTProto API.

CATATAN soal method API:
`account.checkUsername` cuma boleh dipanggil oleh akun user asli -- kalau
dipanggil pakai bot token, Telegram selalu melempar BotMethodInvalidError.
Makanya dipakai `contacts.resolveUsername`, yang BOLEH dipanggil bot, dengan
alur:

1. resolveUsername berhasil (ada entity) ->
   - kalau entity.restricted == True -> BANNED
   - kalau tidak -> occupied (lanjut cek Fragment untuk mastiin bukan listing)
2. resolveUsername -> UsernameNotOccupiedError -> bebas dari sisi Telegram,
   tapi tetap dicek ke fragment.com dulu:
     - ketemu listing/lelang aktif -> FRAGMENT
     - tidak ketemu -> AVAILABLE
3. resolveUsername -> UsernameInvalidError padahal format lolos regex ->
   heuristik BANNED (jarang, tapi ini juga sinyal resmi dari Telegram)

Fragment.com TIDAK dipakai buat nentuin available/banned -- cuma buat deteksi
"apakah harus dibeli lewat Fragment" (baik saat masih kosong maupun saat
sedang dipakai orang). Status "Unavailable" generik di Fragment (yang
muncul untuk hampir semua username biasa karena Telegram baru buka lelang
untuk rentang huruf tertentu) TIDAK dihitung sebagai listing -- itu bukan
sinyal soal username-nya, itu cuma pembatasan platform Fragment.

Strategi anti-flood: banyak worker (bot token) round-robin, delay + jitter
tiap request, dan auto-cooldown per-worker kalau kena FloodWaitError.
"""

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.contacts import ResolveUsernameRequest

# Aturan format username Telegram: 5-32 karakter, huruf/angka/underscore,
# tidak boleh diawali angka.
USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{4,31}$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class Status:
    AVAILABLE = "available"
    FRAGMENT = "fragment"
    TAKEN = "taken"
    BANNED = "banned"
    INVALID = "invalid"
    ERROR = "error"


# Batasi request bersamaan ke fragment.com biar ga keblokir Cloudflare-nya.
_FRAGMENT_SEM = asyncio.Semaphore(5)


@dataclass
class Worker:
    """Satu sesi Telethon (login via bot token)."""

    client: TelegramClient
    name: str
    cooldown_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_ready(self) -> bool:
        return time.time() >= self.cooldown_until


class UsernamePool:
    """Kumpulan worker supaya beban & flood-wait terbagi rata.
    Dengan 1 worker (1 bot token) tetap jalan normal, cuma round-robin-nya
    trivial (selalu balik ke worker yang sama)."""

    def __init__(self, workers: list[Worker], min_delay: float = 1.3):
        if not workers:
            raise ValueError("Butuh minimal 1 worker/bot token.")
        self.workers = workers
        self.min_delay = min_delay
        self._rr = 0  # index round-robin

    async def start(self):
        await asyncio.gather(*(w.client.connect() for w in self.workers))

    def _pick_worker(self) -> Optional[Worker]:
        n = len(self.workers)
        for i in range(n):
            idx = (self._rr + i) % n
            w = self.workers[idx]
            if w.is_ready():
                self._rr = (idx + 1) % n
                return w
        return None

    async def check(self, username: str, _depth: int = 0) -> str:
        username = username.lstrip("@").strip()

        if not USERNAME_RE.match(username):
            return Status.INVALID

        if _depth > len(self.workers) + 2:
            # semua worker lagi cooldown lama sekali, cegah rekursi tak berujung
            return Status.ERROR

        worker = self._pick_worker()
        while worker is None:
            await asyncio.sleep(1)
            worker = self._pick_worker()

        occupied = False  # ada yang resolve (dipakai orang), tapi belum tentu "taken" normal

        async with worker.lock:
            await asyncio.sleep(self.min_delay + random.uniform(0, 0.6))
            try:
                result = await worker.client(ResolveUsernameRequest(username))
                entity = (result.chats or result.users or [None])[0]
                if entity is not None and getattr(entity, "restricted", False):
                    # Berhasil di-resolve TAPI ditandai restricted oleh Telegram
                    # sendiri -> ini sinyal resmi banned/TOS violation.
                    return Status.BANNED
                occupied = True
            except UsernameNotOccupiedError:
                occupied = False
            except UsernameInvalidError:
                # Format lolos regex tapi Telegram bilang invalid -- kandidat
                # lain untuk banned (jarang, tapi ini juga sinyal resmi).
                return Status.BANNED
            except FloodWaitError as e:
                worker.cooldown_until = time.time() + e.seconds + 2
                return await self.check(username, _depth + 1)
            except Exception:
                return Status.ERROR

        # Fragment dicek DI LUAR lock worker supaya worker langsung bebas
        # ngecek username lain. Dicek walau "occupied" karena username yang
        # pernah dibeli lewat Fragment tetap "milik" Fragment walau lagi
        # dipakai pemiliknya.
        try:
            is_listed = await check_fragment_listed(username)
        except Exception:
            # kalau fragment.com error/timeout, jangan gagalkan seluruh cek --
            # anggap saja tidak listed
            is_listed = False

        if is_listed:
            return Status.FRAGMENT
        return Status.TAKEN if occupied else Status.AVAILABLE


async def check_fragment_listed(username: str) -> bool:
    """
    True HANYA kalau username ini beneran dijual/dilelang/sudah kejual lewat
    Fragment (butuh transaksi TON di Fragment buat dapetinnya).

    SINYAL YANG DIPAKAI (terbukti dari 3 kasus nyata):
    1. Request ke /username/<username> di-redirect ke "?query=..." (halaman
       pencarian generik) -> Fragment sama sekali tidak melacak username ini
       -> BUKAN listing. Kejadian nyata: @weonho, @fikayr.
    2. Request TETAP di /username/<username> (halaman detail sendiri) DAN
       og:title diawali "Buy @" -> ini beneran item Fragment (baik lagi
       dijual maupun sudah kejual dengan riwayat transaksi TON) -> LISTING.
       Kejadian nyata: @lsanghyeon (og:title "Buy @lsanghyeon", body-nya
       malah nunjukin "Sold" + riwayat kepemilikan -- tetap dihitung listing
       karena statusnya "milik" Fragment, cuma udah pernah/lagi ditransaksikan).
    3. Request TETAP di /username/<username> TAPI og:title-nya
       "Make an offer for @username" -> ini FITUR GENERIK Fragment yang
       muncul untuk SEMUA username taken biasa (nawarin pengunjung buat
       kirim tawaran ke pemiliknya) -- BUKAN berarti username-nya beneran
       dijual di Fragment. Kejadian nyata: @dintak, @chanyeold (taken biasa,
       bukan listing).
    """
    url = f"https://fragment.com/username/{username}"
    async with _FRAGMENT_SEM:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        except Exception:
            return False

    final_url = str(resp.url)

    # Kasus 1: di-redirect ke halaman pencarian -> Fragment tidak melacak
    # username ini sama sekali -> bukan listing.
    if "?query=" in final_url or "/query=" in final_url:
        return False

    # Kasus 2 & 3: baca og:title dari halaman detail.
    soup = BeautifulSoup(resp.text, "html.parser")
    og_title_tag = soup.find("meta", property="og:title")
    og_title = (og_title_tag.get("content") or "").strip().lower() if og_title_tag else ""

    if og_title.startswith("buy @"):
        return True  # beneran item Fragment (dijual/sudah kejual)

    # "make an offer for @username" atau pola lain di halaman detail yang
    # bukan "buy @" -> cuma fitur generik utk username taken biasa, bukan
    # listing beneran.
    return False


async def debug_fragment_row(username: str) -> str:
    """Util kecil buat kalibrasi manual: kembalikan status_code, final_url,
    title halaman, dan kesimpulan is_listed berdasarkan logika terbaru
    (redirect ke ?query= -> bukan listing, tetap di /username/<nama> ->
    listing). Dipakai lewat command bot kalau suatu saat perlu debug lagi."""
    url = f"https://fragment.com/username/{username}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "(tidak ada)"
    final_url = str(resp.url)

    og_title_tag = soup.find("meta", property="og:title")
    og_title = (og_title_tag.get("content") or "").strip() if og_title_tag else "(tidak ada)"
    og_desc_tag = soup.find("meta", property="og:description")
    og_desc = (og_desc_tag.get("content") or "").strip() if og_desc_tag else "(tidak ada)"

    body_text = soup.get_text(" ", strip=True)

    is_listed = ("?query=" not in final_url and "/query=" not in final_url) and og_title.lower().startswith("buy @")

    diag = (
        f"status_code = {resp.status_code}\n"
        f"final_url = {final_url}\n"
        f"title halaman = {title!r}\n"
        f"og_title = {og_title!r}\n"
        f"og_description = {og_desc!r}\n"
        f"panjang HTML = {len(html)} karakter\n"
        f"kesimpulan is_listed (final) = {is_listed}\n\n"
        f"body_preview:\n{body_text[:600]}\n"
    )

    if "cloudflare" in html.lower() or "captcha" in html.lower() or "checking your browser" in html.lower():
        diag += "\n⚠️ HTML mengandung indikasi Cloudflare/captcha challenge -- kemungkinan request kita lagi diblokir sementara.\n"

    return diag
