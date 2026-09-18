import os
import re
import asyncio
import threading
import time
import tempfile
import requests

from flask import Flask, jsonify
from mutagen.mp3 import MP3
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from messages import (
    START_TEXT,
    WAITING_TEXT,
    CANCEL_TEXT,
    EMPTY_QUERY_TEXT,
    NOT_FOUND_TEXT,
    HINT_TEXT,
    ALTERNATIVES_HEADER,
)


BOT_TOKEN = os.environ.get("BOT_TOKEN")

TRIGGERS = [
    "найти песню",
    "найди песню",
    "что за песня",
    "что за трек",
    "название песни",
    "помоги найти песню",
    "/find",
]

TRIGGER_RE = re.compile(
    r"(найти\s+песню|найди\s+песню|что\s+за\s+песня|что\s+за\s+трек|"
    r"название\s+песни|помоги\s+найти\s+песню)",
    re.IGNORECASE,
)

app_web = Flask(__name__)


@app_web.route("/")
def home():
    return "Music bot is running"


@app_web.route("/health")
def health():
    return jsonify({"status": "ok", "time": time.time()})


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app_web.run(host="0.0.0.0", port=port, use_reloader=False)


def contains_trigger(text):
    if not text:
        return False
    lower = text.lower().strip()
    for t in TRIGGERS:
        if lower == t or lower.startswith(t):
            return True
    return bool(TRIGGER_RE.search(lower))


def extract_query(text):
    if not text:
        return None
    lower = text.lower()
    m = TRIGGER_RE.search(lower)
    if not m:
        return None
    after = text[m.end():].strip(" ,.;:\n\t")
    return after or None


def search_itunes(query, limit=5):
    results = []
    seen = set()
    for country in ("RU", "US"):
        try:
            r = requests.get(
                "https://itunes.apple.com/search",
                params={
                    "term": query,
                    "media": "music",
                    "entity": "song",
                    "limit": limit,
                    "country": country,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            for item in data.get("results", []):
                key = (
                    (item.get("artistName") or "").strip().lower(),
                    (item.get("trackName") or "").strip().lower(),
                )
                if not key[0] or not key[1] or key in seen:
                    continue
                seen.add(key)
                results.append(item)
        except Exception:
            continue
    return results[:limit]


def search_deezer(query, limit=5):
    """Deezer — основной источник: даёт mp3-preview и обложку."""
    results = []
    try:
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", []):
            artist = item.get("artist", {}).get("name", "")
            title = item.get("title", "")
            if not artist or not title:
                continue
            results.append({
                "artistName": artist,
                "trackName": title,
                "collectionName": item.get("album", {}).get("title", ""),
                "trackViewUrl": item.get("link", ""),
                "previewUrl": item.get("preview", ""),
                "coverUrl": (item.get("album") or {}).get("cover_big")
                             or (item.get("album") or {}).get("cover_medium")
                             or (item.get("album") or {}).get("cover", ""),
                "releaseDate": item.get("release_date", ""),
                "duration": item.get("duration", 0),
                "source": "Deezer",
            })
    except Exception:
        pass
    return results


def merge_results(itunes, deezer):
    out = []
    seen = set()

    # Сначала Deezer — у него есть mp3-preview
    for item in deezer:
        artist = (item.get("artistName") or "").strip().lower()
        title = (item.get("trackName") or "").strip().lower()
        key = (artist, title)
        if not artist or not title or key in seen:
            continue
        seen.add(key)
        out.append({
            "artist": item.get("artistName", ""),
            "title": item.get("trackName", ""),
            "album": item.get("collectionName", ""),
            "link": item.get("trackViewUrl", ""),
            "release": item.get("releaseDate", ""),
            "previewUrl": item.get("previewUrl", ""),
            "coverUrl": item.get("coverUrl", ""),
            "duration": item.get("duration", 0),
            "source": "Deezer",
        })

    for item in itunes:
        artist = (item.get("artistName") or "").strip().lower()
        title = (item.get("trackName") or "").strip().lower()
        key = (artist, title)
        if not artist or not title or key in seen:
            continue
        seen.add(key)
        out.append({
            "artist": item.get("artistName", ""),
            "title": item.get("trackName", ""),
            "album": item.get("collectionName", ""),
            "link": item.get("trackViewUrl", ""),
            "release": (item.get("releaseDate") or "")[:10],
            "previewUrl": item.get("previewUrl", ""),
            "coverUrl": (item.get("artworkUrl100") or "")
 .replace("100x100bb", "600x600bb"),
            "duration": (item.get("trackTimeMillis") or 0) // 1000,
            "source": "iTunes",
        })

    return out


def detect_version_tags(title):
    t = (title or "").lower()
    tags = []
    if "sped up" in t:
        tags.append("⚡ sped up")
    if "slowed" in t:
        tags.append("🐌 slowed")
    if "reverb" in t:
        tags.append("🌌 reverb")
    if "live" in t:
        tags.append("🎤 live")
    if "cover" in t or "кавер" in t:
        tags.append("🎸 cover")
    if "remix" in t:
        tags.append("🔊 remix")
    if "instrumental" in t or "минус" in t:
        tags.append("🎼 instrumental")
    if "tiktok" in t:
        tags.append("📱 TikTok version")
    return tags


def download_file(url, timeout=30):
    """Скачивает файл по URL, возвращает байты или None."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def get_mp3_duration(audio_bytes):
    """Считает длительность mp3 в секундах через mutagen."""
    if not audio_bytes:
        return 0
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as f:
            f.write(audio_bytes)
            f.flush()
            audio = MP3(f.name)
            return int(audio.info.length)
    except Exception:
        return 0


def format_alternatives(rest):
    """Текст со списком альтернатив под аудио."""
    if not rest:
        return ""
    lines = ["", ALTERNATIVES_HEADER]
    for i, item in enumerate(rest[:4], start=2):
        lines.append(f"{i}️⃣ {item['artist']} — {item['title']}")
    lines.append("")
    lines.append(HINT_TEXT)
    return "\n".join(lines)


async def start_command(update, context):
    await update.message.reply_text(START_TEXT)


async def find_command(update, context):
    if context.args:
        query = " ".join(context.args).strip()
        await do_search(update, query)
    else:
        context.user_data["waiting_query"] = True
        await update.message.reply_text(WAITING_TEXT)


async def cancel_command(update, context):
    context.user_data.pop("waiting_query", None)
    await update.message.reply_text(CANCEL_TEXT)


async def do_search(update, query):
    if not query:
        await update.message.reply_text(EMPTY_QUERY_TEXT)
        return

    msg = await update.message.reply_text(f"🔎 Ищу: {query}…")

    itunes = search_itunes(query)
    deezer = search_deezer(query)
    merged = merge_results(itunes, deezer)

    if not merged:
        await msg.edit_text(NOT_FOUND_TEXT)
        return

    main = merged[0]
    rest = merged[1:]

    # Скачиваем mp3-preview
    audio_bytes = None
    if main.get("previewUrl"):
        audio_bytes = download_file(main["previewUrl"])

    # Скачиваем обложку
    thumb_bytes = None
    if main.get("coverUrl"):
        thumb_bytes = download_file(main["coverUrl"])

    tags = detect_version_tags(main["title"])
    caption_lines = [
        f"🎵 {main['artist']} — {main['title']}",
    ]
    if main.get("album"):
        caption_lines.append(f"💿 {main['album']}")
    if tags:
        caption_lines.append(f"🎧 {', '.join(tags)}")
    caption = "\n".join(caption_lines)

    try:
        await msg.delete()
    except Exception:
        pass

    if audio_bytes:
        try:
            duration = get_mp3_duration(audio_bytes) or main.get("duration") or 30
            await update.message.reply_audio(
                audio=audio_bytes,
                filename=f"{main['artist']} - {main['title']}.mp3",
                title=main["title"],
                performer=main["artist"],
                duration=duration,
                thumbnail=thumb_bytes,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await update.message.reply_text(
                f"⚠️ Не получилось отправить аудио.\n\n{caption}"
            )
    else:
        await update.message.reply_text(
            f"⚠️ Превью недоступно.\n\n{caption}"
        )

    # Альтернативы — отдельным сообщением
    alt_text = format_alternatives(rest)
    if alt_text:
        await update.message.reply_text(alt_text)


async def handle_text(update, context):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        if not contains_trigger(text):
            return

    query = extract_query(text)

    if query:
        await do_search(update, query)
        return

    if context.user_data.get("waiting_query"):
        context.user_data.pop("waiting_query", None)
        await do_search(update, text)
        return


async def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text,
    ))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    while True:
        await asyncio.sleep(3600)


def start_bot_in_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_bot())
    finally:
        loop.close()


def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    start_bot_in_thread()


if __name__ == "__main__":
    main()
