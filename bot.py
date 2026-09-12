"""
Bot pemantau username Telegram.

Fitur:
- /add user1 user2 ...   -> tambah username ke watchlist
- /remove username       -> hapus dari watchlist
- /list                  -> lihat watchlist + status terakhir
- /check username        -> cek langsung satu username
- /raw username          -> lihat potongan HTML mentah (buat kalibrasi deteksi)
- Background loop        -> jalan terus, muter round-robin ke semua username
                             di watchlist dengan jeda antar-cek supaya aman
                             dari rate limit, dan kirim notifikasi ke Anda
                             kalau ada status yang BERUBAH.

Tidak pakai akun Telegram pribadi sama sekali untuk pengecekan -- hanya
scraping halaman publik t.me dan fragment.com. Bot Telegram (BOT_TOKEN)
cuma dipakai untuk kirim/terima pesan command dengan Anda.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import checker
import storage

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])
CHECK_DELAY_SECONDS = float(os.getenv("CHECK_DELAY_SECONDS", "3"))

STATE_LABEL = {
    "AVAILABLE": "🟢 AVAILABLE (bisa di-keep)",
    "TAKEN": "🟡 TAKEN (sedang dipakai orang)",
    "FRAGMENT": "🔷 FRAGMENT (di-auction/dijual di Fragment)",
    "BANNED": "🔴 BANNED",
    "UNKNOWN": "⚪ UNKNOWN (gagal cek)",
}


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
        "/remove username - hapus username\n"
        "/list - lihat watchlist\n"
        "/check username - cek langsung\n"
        "/raw username - lihat HTML mentah (debug)"
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
        await update.message.reply_text("Contoh: /remove username1")
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
        state = info.get("last_state") or "belum dicek"
        label = STATE_LABEL.get(state, state)
        checked = info.get("last_checked") or "-"
        lines.append(f"@{uname} -> {label} (terakhir: {checked})")
    # Telegram punya limit panjang pesan, potong per 50 baris kalau kepanjangan
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
    async with httpx.AsyncClient() as client:
        result = await checker.check_username_full(client, username)
    await update.message.reply_text(
        f"@{username} -> {STATE_LABEL.get(result['final'], result['final'])}"
    )


@_owner_only
async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buat kalibrasi: lihat potongan HTML mentah dari t.me dan fragment.com"""
    if not context.args:
        await update.message.reply_text("Contoh: /raw username1")
        return
    username = context.args[0].lstrip("@")
    async with httpx.AsyncClient() as client:
        tg = await checker.check_telegram(client, username)
        fg = await checker.check_fragment(client, username)
    await update.message.reply_text(f"[t.me] state={tg['state']}\n{tg.get('raw', '')[:1500]}")
    await update.message.reply_text(f"[fragment] state={fg['state']}\n{fg.get('raw', '')[:1500]}")


async def background_checker(app: Application):
    """Loop tanpa henti: muter round-robin ke semua username di watchlist,
    dengan jeda CHECK_DELAY_SECONDS antar-cek supaya tidak kena rate limit."""
    await app.bot.send_message(OWNER_CHAT_ID, "✅ Background checker mulai jalan.")
    async with httpx.AsyncClient() as client:
        while True:
            wl = storage.get_watchlist()
            if not wl:
                await asyncio.sleep(10)
                continue

            for username in list(wl.keys()):
                try:
                    result = await checker.check_username_full(client, username)
                    new_state = result["final"]
                    old_state = wl.get(username, {}).get("last_state")
                    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

                    storage.update_state(username, new_state, now_str)

                    if old_state is not None and old_state != new_state:
                        label = STATE_LABEL.get(new_state, new_state)
                        await app.bot.send_message(
                            OWNER_CHAT_ID,
                            f"🔔 Status berubah!\n@{username}\n{old_state} -> {label}",
                        )
                except Exception as e:
                    logger.exception(f"Gagal cek {username}: {e}")

                await asyncio.sleep(CHECK_DELAY_SECONDS)


async def _post_init(app: Application):
    # Jalankan background loop sebagai task terpisah, tidak blocking bot command
    asyncio.create_task(background_checker(app))


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("raw", cmd_raw))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
