"""
Bot pemantau username Telegram.

Fitur:
- /add user1 user2 ...   -> tambah username ke watchlist
- /hapus username        -> hapus 1 username dari watchlist
- /list                  -> lihat watchlist + status terakhir
- /check username        -> cek langsung satu username
- Background loop        -> jalan terus, muter round-robin ke semua username
                             di watchlist dengan jeda antar-cek supaya aman
                             dari rate limit, dan kirim notifikasi ke Anda
                             kalau ada status yang BERUBAH.

Pengecekan status (available/taken/banned/fragment) sekarang pakai MTProto
(Telethon) lewat checker.UsernamePool -- BUKAN scraping t.me lagi, karena
t.me terbukti tidak bisa membedakan username available vs banned (halaman
publiknya identik untuk keduanya). Fragment.com tetap dicek via scraping,
tapi cuma untuk deteksi listing/lelang, bukan untuk nentuin available/banned.

BOT_TOKEN dipakai dua jalur sekaligus (aman, beda transport):
- python-telegram-bot (Bot API)  -> kirim/terima pesan command dengan Anda
- Telethon (MTProto)             -> panggil contacts.resolveUsername buat cek
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient

import checker
import storage
from checker import Status, UsernamePool, Worker

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])
API_ID = int(os.environ["API_ID"])       # dari my.telegram.org
API_HASH = os.environ["API_HASH"]        # dari my.telegram.org
RELAY_CHAT_ID = os.getenv("RELAY_CHAT_ID")  # opsional: id grup relay ke bot autokeep
CHECK_DELAY_SECONDS = float(os.getenv("CHECK_DELAY_SECONDS", "1"))

STATE_LABEL = {
    "AVAILABLE": "🟢 Available",
    "TAKEN": "🟡 Taken",
    "FRAGMENT": "🌀 Fragment",
    "BANNED": "🔴 Banned",
    "INVALID": "⚫ Invalid",
    "ERROR": "⚪ Error",
}

# Status dari checker.Status (huruf kecil) -> key yang dipakai di STATE_LABEL
# dan ACTION_LINE di bawah (huruf besar).
STATUS_TO_FINAL = {
    Status.AVAILABLE: "AVAILABLE",
    Status.FRAGMENT: "FRAGMENT",
    Status.TAKEN: "TAKEN",
    Status.BANNED: "BANNED",
    Status.INVALID: "INVALID",
    Status.ERROR: "ERROR",
}

pool: UsernamePool | None = None


def _owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != OWNER_CHAT_ID:
            return  # abaikan pesan dari siapapun selain owner
        return await func(update, context)
    return wrapper


@_owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot pemantau username aktif.\n\n"
        "/add user1 user2 ... - tambah username\n"
        "/hapus username - hapus username\n"
        "/list - lihat watchlist\n"
        "/check username - cek langsung"
    )


@_owner_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: /add username1 username2")
        return
    added = storage.add_usernames(context.args)
    if added:
        await update.message.reply_text("Ditambahkan: " + ", ".join(added))
    else:
        await update.message.reply_text("Tidak ada yang baru ditambahkan (mungkin sudah ada).")


@_owner_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: /hapus username1")
        return
    ok = storage.remove_username(context.args[0])
    await update.message.reply_text("Dihapus." if ok else "Username tidak ditemukan di watchlist.")


@_owner_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wl = storage.get_watchlist()
    if not wl:
        await update.message.reply_text("Watchlist masih kosong. Pakai /add dulu.")
        return
    lines = []
    for uname, info in wl.items():
        state = info.get("last_state")
        label = STATE_LABEL.get(state, "belum dicek")
        lines.append(f"@{uname} {label}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500])


@_owner_only
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: /check username1")
        return
    username = context.args[0].lstrip("@")
    await update.message.reply_text(f"Mengecek @{username} ...")
    raw_status = await pool.check(username)
    final = STATUS_TO_FINAL.get(raw_status, "ERROR")
    await update.message.reply_text(f"@{username} {STATE_LABEL.get(final, final)}")


@_owner_only
async def cmd_fragdebug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug sementara: lihat status_text + potongan HTML mentah yang dibaca
    check_fragment_listed untuk satu username. Dipakai buat kalibrasi ulang
    kalau ada username yang seharusnya FRAGMENT tapi kebaca AVAILABLE/TAKEN."""
    if not context.args:
        await update.message.reply_text("Contoh: /fragdebug username1")
        return
    username = context.args[0].lstrip("@")
    result = await checker.debug_fragment_row(username)
    for i in range(0, len(result), 3500):
        await update.message.reply_text(result[i:i + 3500])


ACTIONABLE_STATES = {"AVAILABLE", "FRAGMENT", "BANNED"}

ACTION_LINE = {
    "AVAILABLE": lambda u: f"@{u} 🟢 Available",
    "FRAGMENT": lambda u: f"@{u} 🌀 Fragment",
    "BANNED": lambda u: f"@{u} 🔴 Banned",
}


async def background_checker(app: Application):
    """Loop tanpa henti: muter round-robin ke semua username di watchlist.
    checker.UsernamePool sudah punya jeda + jitter internal sendiri per
    request MTProto, jadi CHECK_DELAY_SECONDS di sini cuma jeda TAMBAHAN
    antar-username (boleh dikecilkan/dihilangkan kalau mau lebih cepat).

    Setelah SATU PUTARAN PENUH selesai, kirim SATU pesan ringkasan berisi
    semua username yang statusnya AVAILABLE, FRAGMENT, atau BANNED -- tapi
    cuma yang BARU actionable (baru berubah status, atau baru pertama kali
    dicek dan langsung actionable). Kalau statusnya sudah pernah dikabarkan
    dan belum berubah, tidak diulang lagi supaya tidak spam."""
    await app.bot.send_message(OWNER_CHAT_ID, "✅ Background checker mulai jalan.")
    while True:
        wl = storage.get_watchlist()
        if not wl:
            await asyncio.sleep(10)
            continue

        to_report = []  # list of (username, state) yang perlu dikabarkan putaran ini

        for username in list(wl.keys()):
            try:
                raw_status = await pool.check(username)
                new_state = STATUS_TO_FINAL.get(raw_status, "ERROR")
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

                storage.update_state(username, new_state, now_str)

                last_notified = wl.get(username, {}).get("last_notified")

                if new_state in ACTIONABLE_STATES:
                    if last_notified != new_state:
                        to_report.append((username, new_state))
                        storage.update_notified(username, new_state)
                else:
                    # status sudah tidak actionable lagi -> reset, supaya kalau
                    # nanti balik lagi jadi available/fragment/banned, dikabarkan ulang
                    if last_notified is not None:
                        storage.update_notified(username, None)

            except Exception as e:
                logger.exception(f"Gagal cek {username}: {e}")

            await asyncio.sleep(CHECK_DELAY_SECONDS)

        if to_report:
            lines = [ACTION_LINE[state](u) for u, state in to_report]
            text = "\n".join(lines)
            await app.bot.send_message(OWNER_CHAT_ID, text)
            if RELAY_CHAT_ID:
                try:
                    await app.bot.send_message(int(RELAY_CHAT_ID), text)
                except Exception as e:
                    logger.exception(f"Gagal kirim ke grup relay: {e}")


async def _post_init(app: Application):
    global pool
    os.makedirs("sessions", exist_ok=True)
    tele_client = TelegramClient("sessions/checker_worker", API_ID, API_HASH)
    await tele_client.start(bot_token=BOT_TOKEN)
    worker = Worker(client=tele_client, name="worker_0")
    pool = UsernamePool([worker], min_delay=1.3)
    logger.info("UsernamePool siap (1 worker).")

    # Daftarkan menu command supaya muncul saat user ketik "/" di chat.
    await app.bot.set_my_commands([
        BotCommand("start", "Mulai / lihat daftar command"),
        BotCommand("add", "Tambah username ke watchlist"),
        BotCommand("hapus", "Hapus username dari watchlist"),
        BotCommand("list", "Lihat semua username di watchlist"),
        BotCommand("check", "Cek status satu username langsung"),
    ])

    # Jalankan background loop sebagai task terpisah, tidak blocking bot command
    asyncio.create_task(background_checker(app))


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler(["hapus", "remove"], cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("fragdebug", cmd_fragdebug))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
