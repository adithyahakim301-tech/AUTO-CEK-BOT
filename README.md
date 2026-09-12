# Bot Pemantau Username Telegram

Bot ini mengecek status username Telegram (AVAILABLE / TAKEN / FRAGMENT / BANNED)
secara terus-menerus tanpa memakai akun Telegram pribadi Anda sama sekali —
hanya scraping halaman publik `t.me/<username>` dan `fragment.com/username/<username>`.

## 1. Buat Bot Telegram (untuk notifikasi)

1. Buka Telegram, chat ke **@BotFather**
2. Kirim `/newbot`, ikuti instruksinya, catat **token** yang diberikan (format: `123456:ABC-DEF...`)
3. Untuk dapat **chat ID** Anda sendiri: chat ke **@userinfobot**, dia akan balas dengan ID Anda (angka)

## 2. Siapkan repo GitHub

1. Buat repo baru di GitHub
2. Upload semua file di folder ini (`bot.py`, `checker.py`, `storage.py`,
   `requirements.txt`, `Procfile`)
3. **Jangan upload file `.env`** kalau Anda sempat buat — isi token itu rahasia

## 3. Deploy ke Railway

1. Buka [railway.app](https://railway.app), login pakai GitHub (tidak perlu kartu kredit untuk trial)
2. New Project → Deploy from GitHub repo → pilih repo yang tadi dibuat
3. Setelah project dibuat, masuk ke tab **Variables**, tambahkan:
   - `BOT_TOKEN` = token dari BotFather
   - `OWNER_CHAT_ID` = chat ID Anda dari @userinfobot
   - `CHECK_DELAY_SECONDS` = `3` (opsional, jeda antar cek dalam detik)
4. Masuk ke tab **Settings**, pastikan Start Command terisi `python bot.py`
   (biasanya otomatis terbaca dari `Procfile`, tapi cek ulang untuk memastikan)
5. Deploy. Cek tab **Deployments → Logs**, pastikan muncul log `Bot starting...`

## 4. Pakai Bot-nya

Chat ke bot Anda di Telegram:

- `/start` — cek bot aktif
- `/add username1 username2 username3` — tambah ke watchlist (bisa banyak sekaligus)
- `/list` — lihat semua username yang dipantau + status terakhir
- `/remove username1` — hapus dari watchlist
- `/check username1` — cek langsung satu username saat itu juga
- `/raw username1` — lihat potongan HTML mentah (buat kalibrasi, lihat poin 6)

Begitu ada perubahan status pada username yang dipantau (misal dari TAKEN
jadi AVAILABLE, atau jadi masuk FRAGMENT/BANNED), bot otomatis kirim notifikasi
ke Anda.

## 5. Soal Railway trial

Trial Railway (~$5 kredit) akan habis dalam beberapa hari tergantung intensitas
pemakaian — makin banyak username & makin sering cek, makin cepat habis kreditnya.
Setelah trial habis, Anda perlu upgrade ke plan Hobby ($5/bulan) supaya bot tetap
jalan 24 jam. Pantau saldo kredit di dashboard Railway supaya tidak kaget bot
tiba-tiba berhenti.

## 6. Kalibrasi deteksi (penting!)

Deteksi status di `checker.py` berbasis pola teks/HTML umum dari halaman
`t.me` dan `fragment.com`. Karena kedua situs itu bisa berubah markup-nya
sewaktu-waktu, ada kemungkinan deteksi meleset untuk kasus tertentu.

**Sebelum pakai serius**, coba dulu `/check` dan `/raw` ke beberapa username
yang Anda sudah tahu pasti statusnya:
- satu yang jelas **available** (belum pernah dipakai)
- satu yang jelas **taken** (sedang dipakai akun/channel aktif)
- satu yang jelas **banned**
- satu yang Anda tahu ada di **Fragment**

Kalau ada yang salah baca, buka `checker.py`, cari bagian `BANNED_MARKERS` atau
logika di `check_telegram` / `check_fragment`, lalu sesuaikan berdasarkan apa
yang muncul di hasil `/raw`.

## 7. Catatan soal data

Watchlist disimpan di file `data.json` di server. Di Railway trial/free,
filesystem bisa ter-reset kalau Anda redeploy ulang kode. Selama bot jalan
terus tanpa redeploy, data aman. Kalau butuh data permanen walau redeploy,
opsi lanjutan: Railway Volume (plan berbayar) atau database eksternal gratis
seperti Supabase/Neon Postgres.
