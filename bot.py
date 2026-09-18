import os
import re
import asyncio
import threading
import time
import requests

from flask import Flask, jsonify
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
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
    after = text[m.end():].strip($" ,.;:\n\t"$)
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
                "releaseDate": item.get("release_date", ""),
                "source": "Deezer",
            })
    except Exception:
        pass
    return results


def merge_results(itunes, deezer):
    out = []
    seen = set()

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
            "source": "iTunes",
        })

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
            "source": "Deezer",
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


def format_results(results, query):
    if not results:
        return (
            "❌ Ничего не нашёл.\n\n"
            "Попробуй написать точнее:\n"
            "• исполнитель + название\n"
            "• только исполнитель\n"
            "• только название"
        )

    main = results[0]
    rest = results[1:]

    lines = []
    lines.append(f"🎵 {main['artist']} — {main['title']}")

    if main.get("album"):
        lines.append(f"💿 Альбом: {main['album']}")
    if main.get("release"):
        lines.append(f"📅 Дата выхода: {main['release']}")

    tags = detect_version_tags(main["title"])
    if tags:
        lines.append(f"🎧 Версия: {', '.join(tags)}")

    lines.append("")
    lines.append("🔥 Найдено точно")

    if main.get("link"):
        lines.append(f"🔗 {main['link']}")

    if rest:
        lines.append("")
        lines.append("🤔 Другие варианты:")
        for i, item in enumerate(rest[:4], start=2):
            lines.append(f"{i}️⃣ {item['artist']} — {item['title']}")

        lines.append("")
        lines.append(
            "Если это не та песня — пришли точнее: "
            "«найти песню исполнитель название»."
        )

    return "\n".join(lines)


async def start_command(update, context):
    await update.message.reply_text(
        "🎵 Я ищу музыку прямо в чате.\n\n"
        "Как использовать:\n"
        "1) Напиши «найти песню Моника»\n"
        "2) Или напиши «найти песню» — следующим сообщением пришли название/исполнителя\n"
        "3) Я д
