#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ស្រង់ឆាប់ (SrangChap) — Telegram bot ទាញ video / រូប ពី TikTok, Facebook, IG, YT, X…
BOT_TOKEN + ADMIN_ID + DATA_DIR=/var/data
"""
from __future__ import annotations

import os
import re
import json
import time
import shutil
import logging
import threading
import traceback
from pathlib import Path
from urllib.parse import urlparse

import telebot
from telebot import types
from flask import Flask

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
BOT_NAME = "ស្រង់ឆាប់"
BOT_NAME_EN = "SrangChap"
MAX_MB = float(os.environ.get("MAX_FILE_MB", "48"))  # Telegram bot limit ~50MB

try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    DATA_DIR = "."
    os.makedirs(DATA_DIR, exist_ok=True)

TMP_DIR = os.path.join(DATA_DIR, "tmp")
os.makedirs(TMP_DIR, exist_ok=True)
STATS_FILE = os.path.join(DATA_DIR, "stats.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("srangchap")

URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)

SUPPORTED_HINT = (
    "TikTok · Facebook · Instagram · YouTube · X/Twitter · "
    "CapCut · Threads · និង site ផ្សេង yt-dlp គាំទ្រ"
)

if not BOT_TOKEN:
    log.error("BOT_TOKEN missing")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None

_busy: set = set()
_stats_lock = threading.Lock()


def _load_stats():
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ok": 0, "fail": 0, "users": []}


def _save_stats(s):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception as e:
        log.warning("stats save: %s", e)


def track_user(uid):
    with _stats_lock:
        s = _load_stats()
        u = s.setdefault("users", [])
        if uid not in u:
            u.append(uid)
        _save_stats(s)


def track_result(ok: bool):
    with _stats_lock:
        s = _load_stats()
        s["ok" if ok else "fail"] = int(s.get("ok" if ok else "fail", 0)) + 1
        _save_stats(s)


def main_kb(uid=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row("📥 របៀបប្រើ", "ℹ️ អំពី")
    if uid and ADMIN_ID and int(uid) == ADMIN_ID:
        kb.row("🛠 Admin")
    return kb


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    found = URL_RE.findall(text)
    # clean trailing punctuation
    out = []
    for u in found:
        u = u.rstrip(").,]}>\"'")
        if u not in out:
            out.append(u)
    return out


def platform_label(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "tiktok" in host or "vm.tiktok" in host:
        return "TikTok"
    if "facebook" in host or "fb.watch" in host or "fb.com" in host:
        return "Facebook"
    if "instagram" in host:
        return "Instagram"
    if "youtube" in host or "youtu.be" in host:
        return "YouTube"
    if "twitter" in host or "x.com" in host:
        return "X"
    if "threads" in host:
        return "Threads"
    return host or "Web"


def download_media(url: str, workdir: str) -> dict:
    """
    Returns {files: [path], title, thumb, error}
    """
    if yt_dlp is None:
        return {"files": [], "error": "yt-dlp មិនបាន install"}

    outtmpl = os.path.join(workdir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        # prefer mp4 under size limit
        "format": (
            f"bv*[ext=mp4][filesize<{int(MAX_MB)}M]+ba[ext=m4a]/"
            f"b[ext=mp4][filesize<{int(MAX_MB)}M]/"
            f"bv*+ba/b"
        ),
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    }

    files = []
    title = ""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return {"files": [], "error": "ទាញព័ត៌មានមិនបាន"}
            title = (info.get("title") or "")[:200]
            # single or multi (carousel)
            if "requested_downloads" in info:
                for d in info["requested_downloads"]:
                    fp = d.get("filepath")
                    if fp and os.path.isfile(fp):
                        files.append(fp)
            # fallback scan workdir
            if not files:
                for fp in Path(workdir).iterdir():
                    if fp.is_file() and fp.suffix.lower() in (
                        ".mp4", ".webm", ".mkv", ".mov",
                        ".jpg", ".jpeg", ".png", ".webp", ".gif",
                        ".mp3", ".m4a", ".opus",
                    ):
                        files.append(str(fp))
            # also check prepared filename
            try:
                prep = ydl.prepare_filename(info)
                if prep and os.path.isfile(prep) and prep not in files:
                    files.append(prep)
                base, _ = os.path.splitext(prep)
                for ext in (".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mp3", ".m4a"):
                    cand = base + ext
                    if os.path.isfile(cand) and cand not in files:
                        files.append(cand)
            except Exception:
                pass
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private" in msg or "private" in msg:
            return {"files": [], "error": "Content ឯកជន — មិនអាចទាញ"}
        if "Unsupported" in msg:
            return {"files": [], "error": "Link នេះមិនគាំទ្រទេ"}
        return {"files": [], "error": "ទាញមិនបាន: " + msg[:180]}
    except Exception as e:
        log.error("download: %s\n%s", e, traceback.format_exc())
        return {"files": [], "error": "Error: " + str(e)[:180]}

    # filter too large
    ok_files = []
    for fp in files:
        try:
            sz = os.path.getsize(fp)
            if sz > MAX_MB * 1024 * 1024:
                continue
            if sz < 100:
                continue
            ok_files.append(fp)
        except OSError:
            continue

    if not ok_files:
        return {
            "files": [],
            "title": title,
            "error": f"ឯកសារធំពេក (>{MAX_MB:.0f}MB) ឬទាញមិនបាន",
        }
    return {"files": ok_files, "title": title, "error": None}


def send_files(chat_id, files: list, title: str, plat: str):
    caption_base = f"📥 <b>{BOT_NAME}</b> · {plat}"
    if title:
        caption_base += f"\n<i>{title[:100]}</i>"

    for i, fp in enumerate(files[:8]):
        ext = os.path.splitext(fp)[1].lower()
        cap = caption_base if i == 0 else None
        try:
            if ext in (".mp4", ".mov", ".mkv", ".webm"):
                with open(fp, "rb") as f:
                    bot.send_video(chat_id, f, caption=cap, supports_streaming=True)
            elif ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                with open(fp, "rb") as f:
                    bot.send_photo(chat_id, f, caption=cap)
            elif ext in (".mp3", ".m4a", ".opus", ".ogg"):
                with open(fp, "rb") as f:
                    bot.send_audio(chat_id, f, caption=cap)
            else:
                with open(fp, "rb") as f:
                    bot.send_document(chat_id, f, caption=cap)
        except Exception as e:
            log.warning("send %s: %s", fp, e)
            try:
                with open(fp, "rb") as f:
                    bot.send_document(chat_id, f, caption=cap or "file")
            except Exception as e2:
                bot.send_message(chat_id, f"ផ្ញើ file មិនបាន: {e2}")


def process_url(chat_id, uid, url: str):
    plat = platform_label(url)
    status = bot.send_message(chat_id, f"⏳ កំពុងទាញពី <b>{plat}</b>…")
    workdir = os.path.join(TMP_DIR, f"{uid}_{int(time.time())}")
    os.makedirs(workdir, exist_ok=True)
    try:
        result = download_media(url, workdir)
        if result.get("error") or not result.get("files"):
            track_result(False)
            bot.edit_message_text(
                f"❌ {result.get('error') or 'ទាញមិនបាន'}\n<code>{url[:80]}</code>",
                chat_id,
                status.message_id,
            )
            return
        track_result(True)
        try:
            bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass
        send_files(chat_id, result["files"], result.get("title") or "", plat)
        bot.send_message(chat_id, "✅ រួចរាល់ — ផ្ញើ link ផ្សេងបន្តបាន", reply_markup=main_kb(uid))
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


# ── handlers ──
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    track_user(uid)
    bot.send_message(
        message.chat.id,
        f"📥 <b>{BOT_NAME}</b> <i>({BOT_NAME_EN})</i>\n\n"
        f"ទាញ <b>video · រូប · audio</b> ពី:\n{SUPPORTED_HINT}\n\n"
        "➡️ <b>ផ្ញើ link</b> មកទីនេះ\n\n"
        "⚠️ ប្រើសម្រាប់ content ដែលអ្នកមានសិទ្ធិ / public តែប៉ុណ្ណោះ។",
        reply_markup=main_kb(uid),
    )


@bot.message_handler(func=lambda m: m.text == "📥 របៀបប្រើ")
def how_to(message):
    bot.send_message(
        message.chat.id,
        "1️⃣ Copy link ពី TikTok / Facebook / IG / YT\n"
        "2️⃣ Paste ផ្ញើមក bot\n"
        "3️⃣ រង់ចាំ file\n\n"
        f"ទំហំអតិបរមា ~{MAX_MB:.0f}MB (ដែនកំណត់ Telegram Bot)",
        reply_markup=main_kb(message.from_user.id),
    )


@bot.message_handler(func=lambda m: m.text == "ℹ️ អំពី")
def about(message):
    bot.send_message(
        message.chat.id,
        f"<b>{BOT_NAME}</b> — {BOT_NAME_EN}\n"
        "Bot ទាញមេឌៀពី social links\n"
        "Powered by yt-dlp",
        reply_markup=main_kb(message.from_user.id),
    )


@bot.message_handler(func=lambda m: m.text == "🛠 Admin")
def admin_panel(message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    s = _load_stats()
    bot.send_message(
        message.chat.id,
        f"🛠 <b>Admin — {BOT_NAME}</b>\n"
        f"👥 Users: <b>{len(s.get('users', []))}</b>\n"
        f"✅ OK: <b>{s.get('ok', 0)}</b>\n"
        f"❌ Fail: <b>{s.get('fail', 0)}</b>",
        reply_markup=main_kb(message.from_user.id),
    )


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    admin_panel(message)


@bot.message_handler(content_types=["text"])
def on_text(message):
    uid = message.from_user.id
    track_user(uid)
    text = message.text or ""
    urls = extract_urls(text)
    if not urls:
        bot.send_message(
            message.chat.id,
            "ផ្ញើ <b>link</b> (TikTok, Facebook, IG, YouTube…)",
            reply_markup=main_kb(uid),
        )
        return
    if uid in _busy:
        bot.send_message(message.chat.id, "⏳ កំពុងទាញមួយហើយ — រង់ចាំបន្តិច")
        return

    url = urls[0]

    def job():
        _busy.add(uid)
        try:
            process_url(message.chat.id, uid, url)
        except Exception as e:
            log.error("job: %s", e)
            try:
                bot.send_message(message.chat.id, f"❌ Error: {e}")
            except Exception:
                pass
        finally:
            _busy.discard(uid)

    threading.Thread(target=job, daemon=True).start()


app = Flask(__name__)


@app.get("/health")
def health():
    return {"ok": True, "bot": BOT_NAME_EN}, 200


def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN")
    if yt_dlp is None:
        log.error("yt-dlp not installed")
    log.info("Starting %s", BOT_NAME_EN)
    threading.Thread(target=run_flask, daemon=True).start()
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as e:
            log.error("polling: %s", e)
            time.sleep(3)
