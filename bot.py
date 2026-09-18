import os
import re
import threading
import time
import urllib.parse

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
    app_web.run(host="0.0.0.0", port=port)


def contains_trigger(text: str) -> bool:
    if not text:
        return False
    lower = text.lower().strip()
    for t in TRIGGERS:
        if lower == t or lower.startswith(t):
            return True
    return bool(TRIGGER_RE.search(lower))


def extract_query(text: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    m = TRIGGER_RE.search(lower)
    if not m:
        return None
    after = text[m.end():].strip(" ,.;:\n\t")
    return after or None


def search_itunes(query: str, limit: int = 5) -> list:
    """Ищем через iTunes Search API — свежие релизы, все жанры, RU + US."""
    results = []

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
                # Дедуп по trackName+artistName
                key = (item.get("trackName", ""), item.get("artistName", ""))
                if any((k[0] == key[0] and k[1] == key[1]) for k in [(x["trackName"], x["artistName"]) for x in results]):
                    continue
                results.append(item)
 except Exception:
            continue

    return results[:limit]


def search_deezer(query: str, limit: int = 5) -> list:
    """Deezer как дополнительный источник — там есть свежие треки и TikTok-хиты."""
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
            link = item.get("link", "")
            album = item.get("album", {}).get("title", "")
            release = item.get("release_date", "")
            if not artist or not title:
                continue
            results.append({
                "artistName": artist,
                "trackName": title,
                "collectionName": album,
                "trackViewUrl": link,
                "releaseDate": release,
                "source": "Deezer",
            })
    except Exception:
        pass
    return results


def merge_results(itunes: list, deezer: list) -> list:
    """Объединяем iTunes и Deezer, убираем дубли."""
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
            "release": item.get("releaseDate", "")[:10],
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


def detect_version_tags(title: str) -> list:
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


def format_results(results: list, query: str) -> str:
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

        keys = []
        buttons = []

        for i, item in enumerate(rest[:4], start=2):
            label = f"{i}️⃣ {item['artist']} — {item['title']}"
            lines.append(label)
            keys.append(f"alt:{i-1}")

        lines.append("")
        lines.append("Если это не та песня — пришли точнее: «найти песню исполнитель название».")

    return "\n".join(lines)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Я ищу музыку прямо в чате.\n\n"
        "Как использовать:\n"
        "1) Напиши «найти песню Моника»\n"
        "2) Или напиши «найти песню» — следующим сообщением пришли название/исполнителя\n"
        "3) Я дам ссылку на трек 🔎\n\n"
        "Триггеры:\n"
        "• найти песню\n"
        "• найди песню\n"
        "• что за песня\n"
        "• что за трек\n"
        "• название песни\n"
        "• помоги найти песню\n"
        "• /find"
    )


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        query = " ".join(context.args).strip()
        await do_search(update, query)
    else:
        context.user_data["waiting_query"] = True
        await update.message.reply_text(
            "🔎 Напиши название или исполнителя следующим сообщением."
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_query", None)
    await update.message.reply_text("Окей, отменил.")


async def do_search(update: Update, query: str):
    if not query:
        await update.message.reply_text(
            "Напиши название или исполнителя.\n"
            "Пример:\n"
            "найти песню Моника"
        )
        return

    msg = await update.message.reply_text(f"🔎 Ищу: {query}…")

    itunes = search_itunes(query)
    deezer = search_deezer(query)
    merged = merge_results(itunes, deezer)

    text = format_results(merged, query)
    await msg.edit_text(text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_type = update.effective_chat.type

    # В группе реагируем ТОЛЬКО на триггеры — иначе бот спамит
    if chat_type in ("group", "supergroup"):
        if not contains_trigger(text):
            return    query = extract_query(text)

    if query:
        await do_search(update, query)
        return

    # Случай: написали просто «найти песню» — ждём название
    if context.user_data.get("waiting_query"):
        context.user_data.pop("waiting_query", None)
        await do_search(update, text)
        return


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN")

    threading.Thread(target=run_web_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text,
    ))

    application.run_polling()


if __name__ == "__main__":
    main()
