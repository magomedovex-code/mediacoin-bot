# -*- coding: utf-8 -*-
"""
MediaCoin-бот: выдаёт MediaCoin за просмотры видео (YouTube и TikTok).
Пользоваться ботом могут только люди из списка Media.
В список добавляет ТОЛЬКО владелец Discord-сервера: /addmedia, /removemedia, /medialist
Команды игроков: /link, /video, /myvideos, /help
Команды админов: /setviews, /checknow, /unlink
"""
import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from rcon_client import Rcon

BASE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mediacoin")


# ---------------- Настройки ----------------
def load_settings(path):
    if not os.path.exists(path):
        raise SystemExit(f"Не найден файл настроек: {path}")
    s = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            s[k.strip().upper()] = v.strip()
    return s


S = load_settings(os.path.join(BASE, "settings.txt"))


def need(key):
    v = S.get(key, "")
    if not v or v.startswith("ВСТАВЬ"):
        raise SystemExit(f"В settings.txt не заполнено: {key}")
    return v


def as_int(key, default):
    try:
        return int(S.get(key, "") or default)
    except ValueError:
        raise SystemExit(f"В settings.txt {key} должно быть числом")


DISCORD_TOKEN = need("DISCORD_TOKEN")
GUILD_ID = int(need("GUILD_ID"))
ADMIN_CHANNEL_ID = as_int("ADMIN_CHANNEL_ID", 0)
# Канал, куда написать человеку, которого добавили в Media, если у него закрыта личка (0 = не писать)
WELCOME_CHANNEL_ID = as_int("WELCOME_CHANNEL_ID", 0)
# Кто кроме владельца сервера может добавлять людей в Media (Discord ID через запятую). Пусто = только владелец.
MANAGER_IDS = {int(x) for x in re.findall(r"\d{15,21}", S.get("MANAGER_IDS", ""))}
YOUTUBE_API_KEY = S.get("YOUTUBE_API_KEY", "")
if YOUTUBE_API_KEY.startswith("ВСТАВЬ"):
    YOUTUBE_API_KEY = ""
# TikTok: 1 = бот сам читает подпись и просмотры, 0 = только вручную через /setviews
TIKTOK_AUTO = S.get("TIKTOK_AUTO", "1").strip() != "0"
MODE = S.get("MODE", "rcon").lower()
VIEWS_PER_COIN = max(1, as_int("VIEWS_PER_COIN", 100))
CHECK_MINUTES = max(5, as_int("CHECK_MINUTES", 60))
CODE_PREFIX = S.get("CODE_PREFIX", "TOL")
GIVE_COMMAND = S.get("GIVE_COMMAND", "minecraft:give {nick} paper {amount}")

if MODE == "rcon":
    RCON_HOST = need("RCON_HOST")
    RCON_PORT = as_int("RCON_PORT", 25575)
    RCON_PASSWORD = need("RCON_PASSWORD")
elif MODE == "manual":
    if not ADMIN_CHANNEL_ID:
        raise SystemExit("Для MODE=manual нужен ADMIN_CHANNEL_ID")
else:
    raise SystemExit("MODE должен быть rcon или manual")


# ---------------- База данных ----------------
db = sqlite3.connect(os.path.join(BASE, "mediacoin.db"))
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS players(
    discord_id INTEGER PRIMARY KEY,
    nick TEXT NOT NULL UNIQUE COLLATE NOCASE,
    code TEXT NOT NULL UNIQUE,
    linked_at INTEGER
);
CREATE TABLE IF NOT EXISTS videos(
    video_id TEXT PRIMARY KEY,          -- 'yt:xxxxxxxxxxx' или 'tt:123...'  (уникально!)
    platform TEXT NOT NULL,
    discord_id INTEGER NOT NULL,
    url TEXT,
    title TEXT,
    views INTEGER NOT NULL DEFAULT 0,
    coins_given INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',   -- active / review
    added_at INTEGER,
    last_check INTEGER
);
CREATE TABLE IF NOT EXISTS manual_requests(
    message_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at INTEGER,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(message_id, video_id)
);
CREATE TABLE IF NOT EXISTS payouts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, video_id TEXT, nick TEXT, amount INTEGER, how TEXT
);
-- Список Media: только эти люди могут пользоваться ботом
CREATE TABLE IF NOT EXISTS media_users(
    discord_id INTEGER PRIMARY KEY,
    name TEXT,
    added_by INTEGER,
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
""")
# Новая колонка: сколько раз подряд не получилось прочитать просмотры
try:
    db.execute("ALTER TABLE videos ADD COLUMN fails INTEGER NOT NULL DEFAULT 0")
except sqlite3.OperationalError:
    pass
# Переход со старой версии: кто уже привязал ник, тот остаётся в Media (один раз)
if not db.execute("SELECT 1 FROM meta WHERE k='media_migrated'").fetchone():
    db.execute("INSERT OR IGNORE INTO media_users(discord_id, name, added_by, added_at) "
               "SELECT discord_id, nick, 0, linked_at FROM players")
    db.execute("INSERT INTO meta(k, v) VALUES('media_migrated', '1')")
db.commit()


def db_one(sql, args=()):
    return db.execute(sql, args).fetchone()


def db_all(sql, args=()):
    return db.execute(sql, args).fetchall()


def new_code():
    while True:
        code = f"{CODE_PREFIX}-{random.randint(1000, 9999)}"
        if not db_one("SELECT 1 FROM players WHERE code=?", (code,)):
            return code


def is_media(discord_id):
    return db_one("SELECT 1 FROM media_users WHERE discord_id=?", (discord_id,)) is not None


NOT_MEDIA_TEXT = ("❌ Тебя нет в списке **Media**.\n"
                  "Добавить тебя может только владелец сервера. Напиши ему.")


# ---------------- Ссылки ----------------
NICK_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
TT_RE = re.compile(r"tiktok\.com/@[^/\s]*/(?:video|photo)/(\d+)")
TT_SHORT_RE = re.compile(r"(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+|tiktok\.com/t/[A-Za-z0-9]+")


def parse_link(url):
    """Возвращает (платформа, ID) или (None, None).
    Для короткой ссылки TikTok: ('tiktok_short', None) — ID узнаем после открытия ссылки."""
    m = YT_RE.search(url)
    if m:
        return "youtube", m.group(1)
    m = TT_RE.search(url)
    if m:
        return "tiktok", m.group(1)
    if TT_SHORT_RE.search(url):
        return "tiktok_short", None
    return None, None


def video_key(platform, vid):
    return ("yt:" if platform == "youtube" else "tt:") + vid


# ---------------- YouTube ----------------
YT_URL = "https://www.googleapis.com/youtube/v3/videos"


async def yt_fetch(session, ids):
    """Возвращает {id: {title, description, views}} для существующих видео."""
    out = {}
    for i in range(0, len(ids), 50):
        params = {"part": "snippet,statistics", "id": ",".join(ids[i:i + 50]), "key": YOUTUBE_API_KEY}
        async with session.get(YT_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", f"HTTP {r.status}"))
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            out[it["id"]] = {
                "title": sn.get("title", ""),
                "description": sn.get("description", ""),
                "views": int(it.get("statistics", {}).get("viewCount", 0)),
            }
    return out


# ---------------- TikTok ----------------
# У TikTok нет простого бесплатного API для просмотров, поэтому бот читает их так же,
# как браузер: открывает страницу видео. Способ 1 — библиотека yt-dlp (её часто обновляют
# под изменения TikTok), способ 2 — сам читает данные со страницы. Если оба не сработали —
# видео уходит админам на ручную проверку (/setviews), как раньше.
TT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Referer": "https://www.tiktok.com/",
}


def _tt_ytdlp(url):
    """Работает в отдельном потоке. Нужна библиотека yt-dlp."""
    import yt_dlp  # noqa: импорт здесь, чтобы бот работал и без yt-dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 20}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get("view_count") is None:
        raise RuntimeError("yt-dlp не нашёл просмотры")
    return {
        "id": str(info.get("id") or ""),
        "desc": info.get("description") or info.get("title") or "",
        "views": int(info["view_count"]),
        "url": info.get("webpage_url") or url,
        "author": info.get("uploader") or info.get("channel") or "",
    }


def parse_tiktok_html(html, final_url=""):
    """Достаёт данные видео из HTML-страницы TikTok."""
    item = None
    m = re.search(r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        data = json.loads(m.group(1))
        detail = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
        item = (detail.get("itemInfo") or {}).get("itemStruct")
    if not item:
        m = re.search(r'<script[^>]*id="SIGI_STATE"[^>]*>(.*?)</script>', html, re.S)
        if m:
            mod = json.loads(m.group(1)).get("ItemModule") or {}
            if mod:
                item = next(iter(mod.values()))
    if not item:
        raise RuntimeError("TikTok не отдал данные видео (возможно, просит капчу)")
    stats = item.get("statsV2") or {}
    views = stats.get("playCount")
    if views in (None, ""):
        views = (item.get("stats") or {}).get("playCount")
    if views in (None, ""):
        raise RuntimeError("на странице нет числа просмотров")
    author = item.get("author")
    if isinstance(author, dict):
        author = author.get("uniqueId", "")
    vid = str(item.get("id") or "")
    if not vid:
        m = TT_RE.search(final_url)
        vid = m.group(1) if m else ""
    url = f"https://www.tiktok.com/@{author}/video/{vid}" if author and vid else final_url
    return {"id": vid, "desc": item.get("desc") or "", "views": int(views), "url": url, "author": author or ""}


async def _tt_html(session, url):
    async with session.get(url, headers=TT_HEADERS, allow_redirects=True,
                           timeout=aiohttp.ClientTimeout(total=25)) as r:
        final_url = str(r.url)
        html = await r.text()
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
    return parse_tiktok_html(html, final_url)


async def tt_fetch(session, url):
    """Возвращает {id, desc, views, url, author}. Пробует два способа."""
    errors = []
    try:
        info = await asyncio.to_thread(_tt_ytdlp, url)
        if info["id"]:
            return info
    except ImportError:
        errors.append("yt-dlp не установлен")
    except Exception as e:
        errors.append(f"yt-dlp: {str(e)[:150]}")
    try:
        info = await _tt_html(session, url)
        if info["id"]:
            return info
        errors.append("страница: нет ID видео")
    except Exception as e:
        errors.append(f"страница: {str(e)[:150]}")
    raise RuntimeError("; ".join(errors))


# ---------------- Бот ----------------
class MediaBot(discord.Client):
    def __init__(self):
        # Особые разрешения (Server Members Intent) больше не нужны:
        # бот никому не пишет сам, кроме тех, кого добавил владелец.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.http_session = None
        self.check_lock = asyncio.Lock()

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        guild = discord.Object(id=GUILD_ID)
        try:
            await self.tree.sync()             # команды игроков: сервер + личные сообщения
            await self.tree.sync(guild=guild)  # команды админов и владельца: только твой сервер
        except discord.Forbidden:
            log.error("Discord не пускает бота на сервер %s (Missing Access).", GUILD_ID)
            log.error("1) Проверь GUILD_ID в settings.txt.")
            log.error("2) Пригласи бота заново: OAuth2 -> URL Generator -> отметь bot И applications.commands.")
            await self.close()
            return
        checker.change_interval(minutes=CHECK_MINUTES)
        checker.start()

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = MediaBot()


async def admin_channel():
    if not ADMIN_CHANNEL_ID:
        return None
    ch = bot.get_channel(ADMIN_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(ADMIN_CHANNEL_ID)
        except Exception:
            return None
    return ch


async def notify_admins(text):
    ch = await admin_channel()
    if ch:
        try:
            await ch.send(text[:1990])
        except Exception as e:
            log.warning("Не смог написать в админ-канал: %s", e)


async def dm(discord_id, text):
    """Пишет в личку. Возвращает True, если получилось."""
    try:
        user = bot.get_user(discord_id) or await bot.fetch_user(discord_id)
        await user.send(text)
        return True
    except Exception:
        return False  # у человека закрыты ЛС


# ---------- Выдача ----------
def give_command(nick, amount):
    return GIVE_COMMAND.replace("{nick}", nick).replace("{amount}", str(amount))


def rcon_give_all(items):
    """Работает в отдельном потоке. items = [(ник, сколько)].
    Возвращает ({ник: (выдано, ошибка)}, общая_ошибка)."""
    results = {}
    r = Rcon(RCON_HOST, RCON_PORT, RCON_PASSWORD)
    try:
        r.connect()
    except Exception as e:
        return results, f"нет связи с сервером ({type(e).__name__}: {e})"
    try:
        for nick, total in items:
            given, note = 0, ""
            results[nick] = (0, "")
            while given < total:
                n = min(64, total - given)
                resp = r.command(give_command(nick, n))
                if "Gave" not in resp:
                    note = resp.strip() or "пустой ответ"
                    break
                given += n
                results[nick] = (given, "")
            if note:
                results[nick] = (given, note)
        return results, None
    except Exception as e:
        return results, f"связь оборвалась ({type(e).__name__}: {e})"
    finally:
        r.close()


def record_given(videos, amount, nick, how):
    """Распределить выданные монеты по видео игрока."""
    now = int(time.time())
    for vid, owed in videos:
        if amount <= 0:
            break
        take = min(owed, amount)
        db.execute("UPDATE videos SET coins_given = coins_given + ? WHERE video_id=?", (take, vid))
        db.execute("INSERT INTO payouts(ts, video_id, nick, amount, how) VALUES(?,?,?,?,?)",
                   (now, vid, nick, take, how))
        amount -= take
    db.commit()


async def rcon_payout(plan, report):
    items = [(nick, sum(o for _, o in p["videos"])) for nick, p in plan.items()]
    results, error = await asyncio.to_thread(rcon_give_all, items)
    for nick, (given, note) in results.items():
        p = plan[nick]
        if given > 0:
            record_given(p["videos"], given, nick, "rcon")
            report.append(f"✅ {nick}: выдано {given} MediaCoin")
            await dm(p["discord_id"], f"🪙 Тебе выдано **{given} MediaCoin** за просмотры видео!")
        if note:
            if "No player was found" in note:
                report.append(f"⏳ {nick}: не в сети, выдам позже")
            else:
                report.append(f"⚠️ {nick}: сервер ответил: {note}")
    if error:
        report.append(f"⚠️ RCON: {error}. Монеты не пропадут, выдам при следующей проверке.")
    given_lines = [x for x in report if x.startswith("✅")]
    if given_lines:
        await notify_admins("\n".join(given_lines))


async def manual_payout(plan, report):
    ch = await admin_channel()
    if ch is None:
        report.append("⚠️ Не найден админ-канал (ADMIN_CHANNEL_ID).")
        return
    now = int(time.time())
    for nick, p in plan.items():
        total = sum(o for _, o in p["videos"])
        msg = await ch.send(
            f"🪙 **{nick}** положено **{total} MediaCoin**.\n"
            f"Выдай в игре и поставь ✅ под этим сообщением.\n"
            f"`{give_command(nick, total)}`"
        )
        await msg.add_reaction("✅")
        for vid, owed in p["videos"]:
            db.execute("INSERT INTO manual_requests(message_id, video_id, amount, created_at) VALUES(?,?,?,?)",
                       (msg.id, vid, owed, now))
        db.commit()
        report.append(f"📝 {nick}: заявка на {total}")


async def update_tiktok(report):
    """Обновить просмотры TikTok. Видео 'на проверке' тоже пробуем: если в подписи есть код — включаем."""
    rows = db_all("""
        SELECT v.video_id, v.url, v.status, v.fails, p.code, p.nick FROM videos v
        JOIN players p ON p.discord_id = v.discord_id
        JOIN media_users m ON m.discord_id = v.discord_id
        WHERE v.platform = 'tiktok' AND v.status IN ('active', 'review')
    """)
    for i, r in enumerate(rows):
        if i:
            await asyncio.sleep(3)  # не торопимся, чтобы TikTok не заблокировал
        url = r["url"] or f"https://www.tiktok.com/@_/video/{r['video_id'][3:]}"
        now = int(time.time())
        try:
            info = await tt_fetch(bot.http_session, url)
        except Exception as e:
            fails = r["fails"] + 1
            db.execute("UPDATE videos SET fails=? WHERE video_id=?", (fails, r["video_id"]))
            db.commit()
            log.warning("TikTok %s: %s", r["video_id"], e)
            if fails == 3:
                await notify_admins(
                    f"⚠️ Не могу прочитать просмотры TikTok-видео **{r['nick']}** уже 3 раза подряд.\n"
                    f"{url}\nВпиши вручную: `/setviews video:{r['video_id']} views:ЧИСЛО`\n"
                    f"(Если это у всех TikTok — обнови библиотеку: `pip install -U yt-dlp`)")
            continue
        if r["status"] == "review":
            if r["code"].upper() not in info["desc"].upper():
                continue  # кода в подписи нет — пусть решает админ
            report.append(f"🎵 {r['nick']}: TikTok проверен автоматически, код найден")
        db.execute("UPDATE videos SET views = MAX(views, ?), title=?, status='active', fails=0, "
                   "last_check=? WHERE video_id=?",
                   (info["views"], (info["desc"] or "TikTok")[:80], now, r["video_id"]))
        db.commit()


async def run_check():
    async with bot.check_lock:
        report = []
        # 1. Обновить просмотры YouTube
        yt_ids = [r["video_id"][3:] for r in
                  db_all("SELECT video_id FROM videos WHERE platform='youtube' AND status='active'")]
        if yt_ids and YOUTUBE_API_KEY:
            try:
                info = await yt_fetch(bot.http_session, yt_ids)
            except Exception as e:
                info = {}
                report.append(f"⚠️ YouTube: {e}")
            now = int(time.time())
            for vid in yt_ids:
                if vid in info:
                    db.execute("UPDATE videos SET views = MAX(views, ?), title=?, last_check=? WHERE video_id=?",
                               (info[vid]["views"], info[vid]["title"], now, "yt:" + vid))
            db.commit()

        # 2. Обновить просмотры TikTok
        if TIKTOK_AUTO:
            await update_tiktok(report)

        # 3. Посчитать, кому сколько положено (только люди из списка Media)
        rows = db_all("""
            SELECT v.video_id, v.views, v.coins_given, p.nick, p.discord_id,
                   COALESCE((SELECT SUM(m.amount) FROM manual_requests m
                             WHERE m.video_id = v.video_id AND m.done = 0), 0) AS pending
            FROM videos v
            JOIN players p ON p.discord_id = v.discord_id
            JOIN media_users mu ON mu.discord_id = v.discord_id
            WHERE v.status = 'active'
        """)
        plan = {}
        for r in rows:
            owed = r["views"] // VIEWS_PER_COIN - r["coins_given"] - r["pending"]
            if owed > 0:
                p = plan.setdefault(r["nick"], {"discord_id": r["discord_id"], "videos": []})
                p["videos"].append((r["video_id"], owed))
        if not plan:
            report.append("Никому ничего не положено.")
            return report

        # 4. Выдать
        if MODE == "manual":
            await manual_payout(plan, report)
        else:
            await rcon_payout(plan, report)
        return report


@tasks.loop(minutes=60)
async def checker():
    try:
        report = await run_check()
        log.info("Проверка: %s", " | ".join(report))
    except Exception:
        log.exception("Ошибка проверки")


@checker.before_loop
async def before_checker():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    n = db_one("SELECT COUNT(*) AS n FROM media_users")["n"]
    log.info("Бот запущен как %s. Режим: %s. TikTok авто: %s. В списке Media: %s чел.",
             bot.user, MODE, "вкл" if TIKTOK_AUTO else "выкл", n)


@bot.event
async def on_raw_reaction_add(payload):
    if str(payload.emoji) != "✅" or (bot.user and payload.user_id == bot.user.id):
        return
    rows = db_all("SELECT video_id, amount FROM manual_requests WHERE message_id=? AND done=0",
                  (payload.message_id,))
    if not rows:
        return
    member = payload.member
    if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
        return
    nick_row = db_one("SELECT p.nick FROM videos v JOIN players p ON p.discord_id=v.discord_id WHERE v.video_id=?",
                      (rows[0]["video_id"],))
    nick = nick_row["nick"] if nick_row else "?"
    total, now = 0, int(time.time())
    for r in rows:
        db.execute("UPDATE videos SET coins_given = coins_given + ? WHERE video_id=?", (r["amount"], r["video_id"]))
        db.execute("UPDATE manual_requests SET done=1 WHERE message_id=? AND video_id=?",
                   (payload.message_id, r["video_id"]))
        db.execute("INSERT INTO payouts(ts, video_id, nick, amount, how) VALUES(?,?,?,?,?)",
                   (now, r["video_id"], nick, r["amount"], f"manual:{member.display_name}"))
        total += r["amount"]
    db.commit()
    ch = bot.get_channel(payload.channel_id)
    if ch:
        await ch.send(f"✅ Записано: {nick} получил {total} MediaCoin (выдал {member.display_name}).")


# ---------------- Тексты ----------------
def welcome_text():
    return (
        "👋 **Привет! Тебя добавили в Media. Я MediaBot.**\n"
        f"Я выдаю **MediaCoin** за твои видео про сервер: за каждые **{VIEWS_PER_COIN} просмотров — 1 монета**.\n\n"
        "**Как начать (3 шага):**\n"
        "1️⃣ Напиши мне `/link ТвойНик` — ник из Minecraft. Я дам тебе личный код, например `TOL-1234`.\n"
        "2️⃣ Вставь этот код в **описание** YouTube-видео или в **подпись** TikTok-видео.\n"
        "3️⃣ Напиши `/video ссылка_на_видео`.\n\n"
        "Всё! Дальше я сам раз в час смотрю просмотры (и YouTube, и TikTok) и кладу монеты тебе "
        "в инвентарь, когда ты онлайн на сервере.\n\n"
        "📊 `/myvideos` — твои видео и монеты\n"
        "❓ `/help` — показать это сообщение снова\n\n"
        "Команды можно писать прямо здесь, в личке: начни с `/`."
    )


def code_text(nick, code):
    return (
        f"✅ Ник **{nick}** привязан.\n\n"
        f"Твой код: `{code}`\n"
        f"Добавляй его в **описание** YouTube-видео или **подпись** TikTok, потом пиши `/video ссылка`.\n"
        f"За каждые {VIEWS_PER_COIN} просмотров — 1 MediaCoin."
    )


@bot.tree.command(name="help", description="Как получать MediaCoin")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
async def cmd_help(inter: discord.Interaction):
    if not is_media(inter.user.id):
        await inter.response.send_message(
            "🪙 Я выдаю MediaCoin за видео про сервер.\n" + NOT_MEDIA_TEXT, ephemeral=True)
        return
    await inter.response.send_message(welcome_text(), ephemeral=True)


# ---------------- Команды игроков ----------------
def link_player(discord_id, nick):
    """Привязать ник. Возвращает (код, ошибка)."""
    me = db_one("SELECT nick, code FROM players WHERE discord_id=?", (discord_id,))
    if me:
        return None, (f"Уже привязан к **{me['nick']}**. Код: `{me['code']}`.\n"
                      f"Сменить ник может только админ (/unlink).")
    if db_one("SELECT 1 FROM players WHERE nick=?", (nick,)):
        return None, "❌ Этот ник уже привязан к другому Discord."
    code = new_code()
    db.execute("INSERT INTO players(discord_id, nick, code, linked_at) VALUES(?,?,?,?)",
               (discord_id, nick, code, int(time.time())))
    db.commit()
    return code, None


@bot.tree.command(name="link", description="Привязать ник Minecraft к своему Discord")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
@app_commands.describe(nick="Твой ник на сервере")
async def cmd_link(inter: discord.Interaction, nick: str):
    if not is_media(inter.user.id):
        await inter.response.send_message(NOT_MEDIA_TEXT, ephemeral=True)
        return
    nick = nick.strip()
    if not NICK_RE.fullmatch(nick):
        await inter.response.send_message("❌ Ник: 3–16 символов, только латиница, цифры и _.", ephemeral=True)
        return
    code, err = link_player(inter.user.id, nick)
    await inter.response.send_message(err or code_text(nick, code), ephemeral=True)


@bot.tree.command(name="video", description="Зарегистрировать своё видео")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
@app_commands.describe(link="Ссылка на видео YouTube или TikTok")
async def cmd_video(inter: discord.Interaction, link: str):
    await inter.response.defer(ephemeral=True)
    if not is_media(inter.user.id):
        await inter.followup.send(NOT_MEDIA_TEXT, ephemeral=True)
        return
    me = db_one("SELECT nick, code FROM players WHERE discord_id=?", (inter.user.id,))
    if not me:
        await inter.followup.send("❌ Сначала привяжи ник: `/link ник`.", ephemeral=True)
        return
    link = link.strip()
    platform, vid = parse_link(link)
    if not platform:
        await inter.followup.send("❌ Не понял ссылку. Нужна ссылка на YouTube или TikTok.", ephemeral=True)
        return
    if vid and db_one("SELECT 1 FROM videos WHERE video_id=?", (video_key(platform, vid),)):
        await inter.followup.send("❌ Это видео уже зарегистрировано.", ephemeral=True)
        return

    now = int(time.time())
    if platform == "youtube":
        key = video_key(platform, vid)
        if not YOUTUBE_API_KEY:
            await inter.followup.send("❌ YouTube пока не настроен у бота. Напиши админу.", ephemeral=True)
            return
        try:
            info = await yt_fetch(bot.http_session, [vid])
        except Exception as e:
            await inter.followup.send(f"⚠️ YouTube сейчас не отвечает: {e}. Попробуй позже.", ephemeral=True)
            return
        if vid not in info:
            await inter.followup.send("❌ Видео не найдено. Оно удалено, приватное или ссылка неверная.",
                                      ephemeral=True)
            return
        v = info[vid]
        if me["code"].upper() not in v["description"].upper():
            await inter.followup.send(
                f"❌ В описании видео нет твоего кода `{me['code']}`.\n"
                f"Добавь его в описание, подожди минуту и повтори `/video`.", ephemeral=True)
            return
        try:
            db.execute("INSERT INTO videos(video_id, platform, discord_id, url, title, views, status, added_at, "
                       "last_check) VALUES(?,?,?,?,?,?,?,?,?)",
                       (key, platform, inter.user.id, link, v["title"], v["views"], "active", now, now))
            db.commit()
        except sqlite3.IntegrityError:
            await inter.followup.send("❌ Это видео уже зарегистрировано.", ephemeral=True)
            return
        await inter.followup.send(
            f"✅ Видео **{v['title']}** принято!\n"
            f"Сейчас {v['views']} просмотров → положено {v['views'] // VIEWS_PER_COIN} MediaCoin.\n"
            f"Монеты придут при следующей проверке (раз в {CHECK_MINUTES} мин), когда ты будешь онлайн.",
            ephemeral=True)
        return

    # ---------- TikTok ----------
    info = None
    if TIKTOK_AUTO:
        try:
            info = await tt_fetch(bot.http_session, link)
        except Exception as e:
            log.warning("TikTok %s: %s", link, e)
    if info:
        key = "tt:" + info["id"]
        if db_one("SELECT 1 FROM videos WHERE video_id=?", (key,)):
            await inter.followup.send("❌ Это видео уже зарегистрировано.", ephemeral=True)
            return
        if me["code"].upper() not in info["desc"].upper():
            await inter.followup.send(
                f"❌ В подписи TikTok-видео нет твоего кода `{me['code']}`.\n"
                f"Измени подпись (… → Изменить), подожди пару минут и повтори `/video`.", ephemeral=True)
            return
        title = (info["desc"] or "TikTok")[:80]
        try:
            db.execute("INSERT INTO videos(video_id, platform, discord_id, url, title, views, status, added_at, "
                       "last_check) VALUES(?,?,?,?,?,?,?,?,?)",
                       (key, "tiktok", inter.user.id, info["url"], title, info["views"], "active", now, now))
            db.commit()
        except sqlite3.IntegrityError:
            await inter.followup.send("❌ Это видео уже зарегистрировано.", ephemeral=True)
            return
        await inter.followup.send(
            f"✅ TikTok-видео принято!\n"
            f"Сейчас {info['views']} просмотров → положено {info['views'] // VIEWS_PER_COIN} MediaCoin.\n"
            f"Монеты придут при следующей проверке (раз в {CHECK_MINUTES} мин), когда ты будешь онлайн.",
            ephemeral=True)
        return

    # Не получилось прочитать TikTok автоматически → заявка админам (как раньше)
    if platform == "tiktok_short":
        await inter.followup.send(
            "⚠️ Не смог открыть короткую ссылку TikTok. Открой видео в браузере и пришли полную ссылку "
            "вида tiktok.com/@ник/video/123…", ephemeral=True)
        return
    key = video_key(platform, vid)
    try:
        db.execute("INSERT INTO videos(video_id, platform, discord_id, url, status, added_at) "
                   "VALUES(?,?,?,?,?,?)", (key, platform, inter.user.id, link, "review", now))
        db.commit()
    except sqlite3.IntegrityError:
        await inter.followup.send("❌ Это видео уже зарегистрировано.", ephemeral=True)
        return
    await notify_admins(
        f"🎵 Заявка TikTok от **{me['nick']}** (<@{inter.user.id}>) — автоматически прочитать не вышло.\n{link}\n"
        f"Проверь, что в подписи есть код `{me['code']}`, и введи: `/setviews video:{key} views:ЧИСЛО`")
    await inter.followup.send(
        "✅ Видео сохранено. TikTok сейчас не дал прочитать данные, поэтому я попробую ещё раз при "
        "следующей проверке, а заявка ушла админам.", ephemeral=True)


@bot.tree.command(name="myvideos", description="Мои видео и монеты")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
async def cmd_myvideos(inter: discord.Interaction):
    if not is_media(inter.user.id):
        await inter.response.send_message(NOT_MEDIA_TEXT, ephemeral=True)
        return
    rows = db_all("SELECT * FROM videos WHERE discord_id=? ORDER BY added_at DESC LIMIT 15", (inter.user.id,))
    if not rows:
        await inter.response.send_message("У тебя пока нет видео. Используй `/video ссылка`.", ephemeral=True)
        return
    lines, total = [], 0
    for r in rows:
        owed = max(0, r["views"] // VIEWS_PER_COIN - r["coins_given"])
        total += r["coins_given"]
        icon = "🎵" if r["platform"] == "tiktok" else "▶️"
        name = (r["title"] or r["video_id"])[:45]
        status = " (на проверке)" if r["status"] == "review" else ""
        lines.append(f"{icon} **{name}**{status} — 👁 {r['views']} | выдано {r['coins_given']} | ждёт {owed}")
    lines.append(f"\nВсего получено: **{total} MediaCoin**")
    await inter.response.send_message("\n".join(lines), ephemeral=True)


# ---------------- Команды владельца: список Media ----------------
ADMIN_GUILD = discord.Object(id=GUILD_ID)


def can_manage(inter: discord.Interaction):
    """Список Media меняет только владелец сервера (и люди из MANAGER_IDS)."""
    if inter.guild is None:
        return False
    return inter.user.id == inter.guild.owner_id or inter.user.id in MANAGER_IDS


NOT_OWNER_TEXT = "⛔ Добавлять и убирать людей из Media может только владелец сервера."


@bot.tree.command(name="addmedia", guild=ADMIN_GUILD, description="[Владелец] Добавить человека в Media")
@app_commands.describe(user="Начни писать ник в Discord и выбери человека",
                       minecraft="Его ник в Minecraft (необязательно: тогда бот сразу пришлёт ему код)")
@app_commands.default_permissions(administrator=True)
async def cmd_addmedia(inter: discord.Interaction, user: discord.Member, minecraft: str = None):
    if not can_manage(inter):
        await inter.response.send_message(NOT_OWNER_TEXT, ephemeral=True)
        return
    if user.bot:
        await inter.response.send_message("❌ Это бот.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    if minecraft is not None:
        minecraft = minecraft.strip()
        if not NICK_RE.fullmatch(minecraft):
            await inter.followup.send("❌ Ник Minecraft: 3–16 символов, только латиница, цифры и _.", ephemeral=True)
            return
    already = is_media(user.id)
    db.execute("INSERT OR REPLACE INTO media_users(discord_id, name, added_by, added_at) VALUES(?,?,?,?)",
               (user.id, user.name, inter.user.id, int(time.time())))
    db.commit()

    text = welcome_text()
    note = ""
    if minecraft:
        code, err = link_player(user.id, minecraft)
        if code:
            text += "\n\n" + code_text(minecraft, code)
            note = f"\nНик **{minecraft}** привязан, код `{code}`."
        else:
            note = "\n" + err.replace("Уже привязан", "Он уже привязан")

    sent = await dm(user.id, text)
    if not sent and WELCOME_CHANNEL_ID:
        ch = bot.get_channel(WELCOME_CHANNEL_ID)
        if ch:
            try:
                await ch.send(f"{user.mention}\n" + text, allowed_mentions=discord.AllowedMentions(users=[user]))
                sent = True
            except Exception:
                pass
    head = f"ℹ️ {user.mention} уже был в Media." if already else f"✅ {user.mention} добавлен в Media."
    tail = "\n📨 Инструкцию ему отправил." if sent else \
        "\n⚠️ Не смог написать ему: у него закрыта личка. Попроси его написать боту `/help`."
    await inter.followup.send(head + note + tail, ephemeral=True)


@bot.tree.command(name="removemedia", guild=ADMIN_GUILD, description="[Владелец] Убрать человека из Media")
@app_commands.describe(user="Кого убрать")
@app_commands.default_permissions(administrator=True)
async def cmd_removemedia(inter: discord.Interaction, user: discord.User):
    if not can_manage(inter):
        await inter.response.send_message(NOT_OWNER_TEXT, ephemeral=True)
        return
    if not is_media(user.id):
        await inter.response.send_message("Его и так нет в Media.", ephemeral=True)
        return
    db.execute("DELETE FROM media_users WHERE discord_id=?", (user.id,))
    db.commit()
    await inter.response.send_message(
        f"✅ {user.mention} убран из Media. Бот больше не принимает его видео и не выдаёт монеты.\n"
        f"Ник и видео остались в базе: если добавишь его снова, всё продолжится.", ephemeral=True)


@bot.tree.command(name="medialist", guild=ADMIN_GUILD, description="[Владелец] Кто в списке Media")
@app_commands.default_permissions(administrator=True)
async def cmd_medialist(inter: discord.Interaction):
    if not can_manage(inter):
        await inter.response.send_message(NOT_OWNER_TEXT, ephemeral=True)
        return
    rows = db_all("""SELECT m.discord_id, m.name, p.nick,
                     (SELECT COUNT(*) FROM videos v WHERE v.discord_id = m.discord_id) AS n
                     FROM media_users m LEFT JOIN players p ON p.discord_id = m.discord_id
                     ORDER BY m.added_at""")
    if not rows:
        await inter.response.send_message("Список Media пуст. Добавь: `/addmedia`.", ephemeral=True)
        return
    lines = [f"**Media ({len(rows)}):**"]
    for r in rows:
        mc = f"🎮 {r['nick']}" if r["nick"] else "ник не привязан"
        lines.append(f"• <@{r['discord_id']}> — {mc} — видео: {r['n']}")
    await inter.response.send_message("\n".join(lines)[:1990], ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())


# ---------------- Команды админов ----------------
@bot.tree.command(name="setviews", guild=ADMIN_GUILD, description="[Админ] Вписать просмотры видео вручную")
@app_commands.describe(video="ID вида tt:123… или ссылка", views="Количество просмотров")
@app_commands.default_permissions(administrator=True)
async def cmd_setviews(inter: discord.Interaction, video: str, views: int):
    video = video.strip()
    key = video
    if not re.fullmatch(r"(yt|tt):[\w-]+", video):
        platform, vid = parse_link(video)
        if not vid:
            await inter.response.send_message("❌ Не понял видео. Вставь полную ссылку или ID вида tt:123…",
                                              ephemeral=True)
            return
        key = video_key(platform, vid)
    row = db_one("SELECT views FROM videos WHERE video_id=?", (key,))
    if not row:
        await inter.response.send_message("❌ Такого видео нет в базе.", ephemeral=True)
        return
    db.execute("UPDATE videos SET views=?, status='active', fails=0, last_check=? WHERE video_id=?",
               (max(0, views), int(time.time()), key))
    db.commit()
    await inter.response.send_message(f"✅ {key}: просмотров теперь {views}. Монеты — при следующей проверке "
                                      f"или сразу через /checknow.", ephemeral=True)


@bot.tree.command(name="checknow", guild=ADMIN_GUILD, description="[Админ] Проверить просмотры и выдать монеты сейчас")
@app_commands.default_permissions(administrator=True)
async def cmd_checknow(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    report = await run_check()
    await inter.followup.send("\n".join(report)[:1900], ephemeral=True)


@bot.tree.command(name="unlink", guild=ADMIN_GUILD, description="[Админ] Отвязать ник игрока")
@app_commands.default_permissions(administrator=True)
async def cmd_unlink(inter: discord.Interaction, user: discord.User):
    row = db_one("SELECT nick FROM players WHERE discord_id=?", (user.id,))
    if not row:
        await inter.response.send_message("У него нет привязки.", ephemeral=True)
        return
    db.execute("DELETE FROM players WHERE discord_id=?", (user.id,))
    db.commit()
    await inter.response.send_message(f"✅ {user.mention} отвязан от {row['nick']}. Видео остались в базе "
                                      f"и снова заработают после новой /link.", ephemeral=True)


if __name__ == "__main__":
    # Защита от двух копий бота на одном компьютере
    import socket as _socket
    _lock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _lock.bind(("127.0.0.1", 47219))
    except OSError:
        print("Бот УЖЕ запущен на этом компьютере (в другом окне).")
        print("Закрой лишние окна или запусти STOP_BOT.bat, потом ZAPUSK_BOT.bat.")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN, log_handler=None)
