import asyncio
import random
import aiohttp
import aiosqlite
import logging
import time
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    LabeledPrice, FSInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8575048598:AAFJHa2Cgb13naDZUDIIE4ywnoTbX6AXldY"
CHANNEL_ID = -1003907832797
CHANNEL_USERNAME = "hmsearch"
ADMIN_IDS = [189770280,8763447820]
DB_PATH = "/data/database.db"
PHOTO_PATH = "photo.jpg"
CRYPTOBOT_TOKEN = "593969:AAB5F287wFD8WDk3AeCnIWs9KCAOIvmkG3f"
CRYPTOBOT_API = "https://pay.crypt.bot/api"

FREE_DAILY_LIMIT = 5
FREE_MASK_LIMIT = 1
SEARCH_COOLDOWN = 3

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}

VOWELS = "aeiou"
CONSONANTS = "bcdfghjklmnpqrstvwxyz"
PATTERNS_5 = ["CVCVC", "CVCCV", "VCCVC", "VCVCV", "CVCVV", "CVVCV"]
PATTERNS_6 = ["CVCVCV", "CVCCVC", "VCVCVC", "CVVCVC", "CCVCVC", "CVCCVV"]

search_cooldowns = {}
mask_locks = {}  # uid -> asyncio.Lock, предотвращает параллельные запросы маски

def get_mask_lock(uid):
    if uid not in mask_locks:
        mask_locks[uid] = asyncio.Lock()
    return mask_locks[uid]

# Фришный режим — хранит время окончания и что именно фришно
# freetime_until = timestamp когда заканчивается
# freetime_types = set из "search", "filter", "mask"
# freetime_cooldown = КД в секундах (None = не менять)
freetime_until = 0
freetime_types = set()
freetime_cooldown = None

PLANS = {
    "1d":  {"label": "1 день",  "days": 1,  "rub": 95,  "usd": 1.2, "stars": 65},
    "3d":  {"label": "3 дня",   "days": 3,  "rub": 145, "usd": 1.8, "stars": 100},
    "7d":  {"label": "7 дней",  "days": 7,  "rub": 315, "usd": 3.8, "stars": 215},
    "30d": {"label": "30 дней", "days": 30, "rub": 769, "usd": 9.5, "stars": 530},

}

REF_REWARDS = [(10, 1), (20, 3), (30, 7), (40, 30)]


# ── БД ────────────────────────────────────────────────────────

def parse_duration(text: str) -> int:
    """Парсит строку вида 1д, 2ч, 30м в минуты. Возвращает минуты или None при ошибке."""
    text = text.strip().lower()
    try:
        if text.endswith("д"):
            return int(text[:-1]) * 24 * 60
        elif text.endswith("ч"):
            return int(text[:-1]) * 60
        elif text.endswith("м"):
            return int(text[:-1])
        else:
            return None  # без буквы — не принимаем, требуем явный формат
    except ValueError:
        return None

def minutes_to_str(minutes: int) -> str:
    """Переводит минуты в читаемую строку."""
    if minutes >= 24 * 60:
        d = minutes // (24 * 60)
        h = (minutes % (24 * 60)) // 60
        return str(d) + " дн." + (" " + str(h) + " ч." if h else "")
    elif minutes >= 60:
        h = minutes // 60
        m = minutes % 60
        return str(h) + " ч." + (" " + str(m) + " мин." if m else "")
    else:
        return str(minutes) + " мин."

async def init_db():
    os.makedirs("/data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, ref_by INTEGER,
            ref_count INTEGER DEFAULT 0, total_found INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expires_at TEXT);
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0, first_used TEXT
        );
        CREATE TABLE IF NOT EXISTS traps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY, days INTEGER, max_uses INTEGER,
            used INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS promo_uses (user_id INTEGER, code TEXT, PRIMARY KEY (user_id, code));
        CREATE TABLE IF NOT EXISTS ref_rewards_given (
            user_id INTEGER, threshold INTEGER, PRIMARY KEY (user_id, threshold)
        );
        CREATE TABLE IF NOT EXISTS payments (
            invoice_id TEXT PRIMARY KEY, user_id INTEGER, plan TEXT,
            method TEXT, status TEXT DEFAULT 'pending', created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS search_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            username_found TEXT, length INTEGER, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bonus_searches (
            user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bonus_filter (
            user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bonus_mask (
            user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS roulette_usage (
            user_id INTEGER PRIMARY KEY, last_spin TEXT
        );
        CREATE TABLE IF NOT EXISTS filter_usage (
            user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0, first_used TEXT
        );
        CREATE TABLE IF NOT EXISTS filter_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, pattern TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS mask_usage (
            user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0, first_used TEXT
        );
        CREATE TABLE IF NOT EXISTS mask_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, word TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY, search_count INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS watermarks (
            user_id INTEGER PRIMARY KEY, watermark_text TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS blocked_letters (
            user_id INTEGER PRIMARY KEY, letters TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS market_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER,
            seller_username TEXT,
            username TEXT,
            price INTEGER,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS market_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id INTEGER,
            buyer_id INTEGER,
            seller_id INTEGER,
            rating INTEGER,
            text TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS market_blocked (
            user_id INTEGER PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        # Миграция: добавляем search_count в user_settings если нет
        try:
            await db.execute("ALTER TABLE user_settings ADD COLUMN search_count INTEGER DEFAULT 1")
        except Exception:
            pass
        # Миграция: добавляем extra_spins в roulette_usage
        try:
            await db.execute("ALTER TABLE roulette_usage ADD COLUMN extra_spins INTEGER DEFAULT 0")
        except Exception:
            pass
        # Миграция: market_blocked
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS market_blocked (user_id INTEGER PRIMARY KEY, created_at TEXT DEFAULT (datetime('now')))")
        except Exception:
            pass
        await db.commit()


async def get_mask_usage(uid):
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute(
            "SELECT count, first_used FROM mask_usage WHERE user_id=? AND first_used > ?",
            (uid, cutoff)
        )).fetchone()
        return (r[0], r[1]) if r else (0, None)

async def inc_mask_usage(uid):
    now_iso = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute(
            "SELECT count, first_used FROM mask_usage WHERE user_id=? AND first_used > ?",
            (uid, cutoff)
        )).fetchone()
        if r:
            await db.execute("UPDATE mask_usage SET count=count+1 WHERE user_id=?", (uid,))
        else:
            await db.execute(
                "INSERT OR REPLACE INTO mask_usage (user_id, count, first_used) VALUES (?,1,?)",
                (uid, now_iso)
            )
        await db.commit()

async def save_mask_history(uid, word):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM mask_history WHERE user_id=? AND word=?", (uid, word))
        await db.execute("INSERT INTO mask_history (user_id,word) VALUES (?,?)", (uid, word))
        await db.execute("""
            DELETE FROM mask_history WHERE user_id=? AND id NOT IN (
                SELECT id FROM mask_history WHERE user_id=? ORDER BY id DESC LIMIT 3
            )""", (uid, uid))
        await db.commit()

async def get_mask_history(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT word FROM mask_history WHERE user_id=? ORDER BY id DESC LIMIT 3", (uid,)
        )).fetchall()
        return [r[0] for r in rows]

async def get_search_count(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute(
            "SELECT search_count FROM user_settings WHERE user_id=?", (uid,)
        )).fetchone()
        return r[0] if r else 1

async def set_search_count(uid, count):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, search_count) VALUES (?,?)",
            (uid, count)
        )
        await db.commit()

async def get_blocked_letters(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT letters FROM blocked_letters WHERE user_id=?", (uid,))).fetchone()
        if not r or not r[0]:
            return set()
        return set(r[0])

async def add_blocked_letter(uid, letter):
    letters = await get_blocked_letters(uid)
    letters.add(letter)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blocked_letters (user_id, letters) VALUES (?,?)",
            (uid, "".join(sorted(letters)))
        )
        await db.commit()

async def remove_blocked_letter(uid, letter):
    letters = await get_blocked_letters(uid)
    letters.discard(letter)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blocked_letters (user_id, letters) VALUES (?,?)",
            (uid, "".join(sorted(letters)))
        )
        await db.commit()


# ── МАРКЕТ ────────────────────────────────────────────────────

async def market_add_lot(seller_id, seller_username, username, price):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO market_lots (seller_id, seller_username, username, price) VALUES (?,?,?,?)",
            (seller_id, seller_username, username, price)
        )
        await db.commit()

async def market_get_lots(page=0, per_page=5):
    async with aiosqlite.connect(DB_PATH) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM market_lots WHERE active=1")).fetchone())[0]
        rows = await (await db.execute(
            "SELECT id, seller_id, seller_username, username, price, created_at FROM market_lots WHERE active=1 ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, page * per_page)
        )).fetchall()
        return rows, total

async def market_get_lot(lot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        return await (await db.execute(
            "SELECT id, seller_id, seller_username, username, price, created_at FROM market_lots WHERE id=? AND active=1",
            (lot_id,)
        )).fetchone()

async def market_delete_lot(lot_id, seller_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if seller_id:
            await db.execute("UPDATE market_lots SET active=0 WHERE id=? AND seller_id=?", (lot_id, seller_id))
        else:
            await db.execute("UPDATE market_lots SET active=0 WHERE id=?", (lot_id,))
        await db.commit()

async def market_get_my_lots(seller_id):
    async with aiosqlite.connect(DB_PATH) as db:
        return await (await db.execute(
            "SELECT id, username, price, created_at FROM market_lots WHERE seller_id=? AND active=1 ORDER BY id DESC",
            (seller_id,)
        )).fetchall()

async def market_get_reviews(seller_id):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT rating, text, created_at FROM market_reviews WHERE seller_id=? ORDER BY id DESC LIMIT 20",
            (seller_id,)
        )).fetchall()
        total = (await (await db.execute(
            "SELECT COUNT(*), AVG(rating) FROM market_reviews WHERE seller_id=?", (seller_id,)
        )).fetchone())
        count = total[0] or 0
        avg = round(total[1] or 0, 1)
        return rows, count, avg

async def market_get_reviews_admin(seller_id, page=0, per_page=5):
    async with aiosqlite.connect(DB_PATH) as db:
        total = (await (await db.execute(
            "SELECT COUNT(*) FROM market_reviews WHERE seller_id=?", (seller_id,)
        )).fetchone())[0]
        rows = await (await db.execute(
            "SELECT id, rating, created_at FROM market_reviews WHERE seller_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (seller_id, per_page, page * per_page)
        )).fetchall()
        return rows, total

async def market_delete_review(review_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM market_reviews WHERE id=?", (review_id,))
        await db.commit()

async def market_block_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO market_blocked (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def is_market_blocked(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT 1 FROM market_blocked WHERE user_id=?", (user_id,))).fetchone()
        return bool(r)

async def market_add_review(lot_id, buyer_id, seller_id, rating, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO market_reviews (lot_id, buyer_id, seller_id, rating, text) VALUES (?,?,?,?,?)",
            (lot_id, buyer_id, seller_id, rating, text)
        )
        await db.commit()

async def market_get_user_rating(seller_id):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute(
            "SELECT COUNT(*), AVG(rating) FROM market_reviews WHERE seller_id=?", (seller_id,)
        )).fetchone()
        count = r[0] or 0
        avg = round(r[1] or 0, 1)
        return count, avg

def stars_display(avg, count):
    full = int(round(avg))
    full = max(0, min(5, full))
    filled = "★" * full
    empty = "☆" * (5 - full)
    return filled + empty + " (" + str(count) + ")"

async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        return await (await db.execute("SELECT * FROM users WHERE user_id=?", (uid,))).fetchone()

async def ensure_user(uid, uname, fname, ref_by=None):
    async with aiosqlite.connect(DB_PATH) as db:
        ex = await (await db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))).fetchone()
        if not ex:
            await db.execute("INSERT INTO users (user_id,username,full_name,ref_by) VALUES (?,?,?,?)", (uid, uname, fname, ref_by))
            if ref_by:
                await db.execute("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (ref_by,))
            await db.commit()
            return True
        await db.execute("UPDATE users SET username=?,full_name=? WHERE user_id=?", (uname, fname, uid))
        await db.commit()
        return False

async def is_blocked(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT is_blocked FROM users WHERE user_id=?", (uid,))).fetchone()
        return bool(r and r[0])

async def has_subscription(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (uid,))).fetchone()
        return bool(r and datetime.fromisoformat(r[0]) > datetime.now())

async def get_sub_expires(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (uid,))).fetchone()
        if not r: return None
        exp = datetime.fromisoformat(r[0])
        return exp if exp > datetime.now() else None

async def add_subscription(uid, minutes: int):
    """Добавляет подписку поверх существующей. minutes - количество минут."""
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (uid,))).fetchone()
        base = max(datetime.fromisoformat(r[0]), datetime.now()) if r else datetime.now()
        new_exp = base + timedelta(minutes=minutes)
        await db.execute("INSERT OR REPLACE INTO subscriptions (user_id,expires_at) VALUES (?,?)", (uid, new_exp.isoformat()))
        await db.commit()

async def set_subscription(uid, minutes: int):
    """Устанавливает подписку с нуля, сбрасывая текущую. minutes - количество минут."""
    async with aiosqlite.connect(DB_PATH) as db:
        new_exp = datetime.now() + timedelta(minutes=minutes)
        await db.execute("INSERT OR REPLACE INTO subscriptions (user_id,expires_at) VALUES (?,?)", (uid, new_exp.isoformat()))
        await db.commit()

async def get_daily_usage(uid):
    """Возвращает (count, first_used_dt) за последние 24ч. Если истекло - (0, None)."""
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute(
            "SELECT count, first_used FROM daily_usage WHERE user_id=?",
            (uid,)
        )).fetchone()
        if not r or not r[1]:
            return (0, None)
        count, first_used_str = r
        try:
            first_dt = datetime.fromisoformat(first_used_str.replace(" ", "T"))
        except Exception:
            return (0, None)
        if datetime.now() - first_dt >= timedelta(hours=24):
            return (0, None)
        return (min(count, FREE_DAILY_LIMIT), first_used_str)

async def inc_daily_usage(uid):
    now_iso = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute(
            "SELECT count, first_used FROM daily_usage WHERE user_id=?",
            (uid,)
        )).fetchone()
        in_window = False
        if r and r[1]:
            try:
                first_dt = datetime.fromisoformat(r[1].replace(" ", "T"))
                in_window = (datetime.now() - first_dt) < timedelta(hours=24)
            except Exception:
                pass
        if in_window:
            new_count = min((r[0] or 0) + 1, FREE_DAILY_LIMIT)
            await db.execute(
                "UPDATE daily_usage SET count=? WHERE user_id=?",
                (new_count, uid)
            )
        else:
            await db.execute(
                "INSERT OR REPLACE INTO daily_usage (user_id, count, first_used) VALUES (?,1,?)",
                (uid, now_iso)
            )
        await db.commit()

async def inc_total_found(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET total_found=total_found+1 WHERE user_id=?", (uid,))
        await db.commit()

async def get_bonus_searches(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT count FROM bonus_searches WHERE user_id=?", (uid,))).fetchone()
        return r[0] if r else 0

async def add_bonus_searches(uid, count):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bonus_searches (user_id, count) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET count=count+?",
            (uid, count, count)
        )
        await db.commit()

async def use_bonus_search(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT count FROM bonus_searches WHERE user_id=?", (uid,))).fetchone()
        if r and r[0] > 0:
            await db.execute("UPDATE bonus_searches SET count=count-1 WHERE user_id=?", (uid,))
            await db.commit()
            return True
    return False

async def get_bonus_filter(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT count FROM bonus_filter WHERE user_id=?", (uid,))).fetchone()
        return r[0] if r else 0

async def add_bonus_filter(uid, count):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bonus_filter (user_id, count) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET count=count+?",
            (uid, count, count))
        await db.commit()

async def use_bonus_filter(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT count FROM bonus_filter WHERE user_id=?", (uid,))).fetchone()
        if r and r[0] > 0:
            await db.execute("UPDATE bonus_filter SET count=count-1 WHERE user_id=?", (uid,))
            await db.commit()
            return True
    return False

async def get_bonus_mask(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT count FROM bonus_mask WHERE user_id=?", (uid,))).fetchone()
        return r[0] if r else 0

async def add_bonus_mask(uid, count):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bonus_mask (user_id, count) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET count=count+?",
            (uid, count, count))
        await db.commit()

async def use_bonus_mask(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT count FROM bonus_mask WHERE user_id=?", (uid,))).fetchone()
        if r and r[0] > 0:
            await db.execute("UPDATE bonus_mask SET count=count-1 WHERE user_id=?", (uid,))
            await db.commit()
            return True
    return False

async def get_roulette_last(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT last_spin FROM roulette_usage WHERE user_id=?", (uid,))).fetchone()
        return r[0] if r else None

async def set_roulette_last(uid):
    now_iso = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO roulette_usage (user_id, last_spin) VALUES (?,?)", (uid, now_iso))
        await db.commit()

async def get_extra_spins(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT extra_spins FROM roulette_usage WHERE user_id=?", (uid,))).fetchone()
        return r[0] if r else 0

async def add_extra_spins(uid, count):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO roulette_usage (user_id, extra_spins) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET extra_spins = COALESCE(extra_spins, 0) + ?
        """, (uid, count, count))
        await db.commit()

async def use_extra_spin(uid):
    """Использует 1 extra spin. Возвращает True если использован."""
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT extra_spins FROM roulette_usage WHERE user_id=?", (uid,))).fetchone()
        if r and r[0] and r[0] > 0:
            await db.execute("UPDATE roulette_usage SET extra_spins=extra_spins-1 WHERE user_id=?", (uid,))
            await db.commit()
            return True
    return False

async def get_traps(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        return await (await db.execute("SELECT id,username FROM traps WHERE user_id=? AND active=1", (uid,))).fetchall()

async def add_trap(uid, username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO traps (user_id,username) VALUES (?,?)", (uid, username))
        await db.commit()

async def del_trap(tid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE traps SET active=0 WHERE id=?", (tid,))
        await db.commit()

async def get_all_traps():
    async with aiosqlite.connect(DB_PATH) as db:
        return await (await db.execute("SELECT id,user_id,username FROM traps WHERE active=1")).fetchall()

async def trigger_trap(tid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE traps SET active=0 WHERE id=?", (tid,))
        await db.commit()

async def get_ref_count(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT ref_count FROM users WHERE user_id=?", (uid,))).fetchone()
        return r[0] if r else 0

async def check_ref_rewards(uid, bot):
    rc = await get_ref_count(uid)
    async with aiosqlite.connect(DB_PATH) as db:
        for threshold, days in REF_REWARDS:
            if rc >= threshold:
                given = await (await db.execute(
                    "SELECT 1 FROM ref_rewards_given WHERE user_id=? AND threshold=?", (uid, threshold)
                )).fetchone()
                if not given:
                    await db.execute("INSERT INTO ref_rewards_given (user_id,threshold) VALUES (?,?)", (uid, threshold))
                    await db.commit()
                    await set_subscription(uid, days * 24 * 60)
                    await bot.send_message(
                        uid,
                        "🎉 <b>Реферальная награда!</b>\n\nДостигли <b>" + str(threshold) + " рефералов</b> — получаете <b>" + str(days) + " дней подписки!</b>",
                        parse_mode="HTML"
                    )

async def log_search(uid, uname_found, length):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO search_log (user_id,username_found,length) VALUES (?,?,?)", (uid, uname_found, length))
        await db.commit()

FREE_FILTER_LIMIT = 3

async def get_filter_usage(uid):
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute(
            "SELECT count, first_used FROM filter_usage WHERE user_id=? AND first_used > ?",
            (uid, cutoff)
        )).fetchone()
        return (r[0], r[1]) if r else (0, None)

async def inc_filter_usage(uid):
    now_iso = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute(
            "SELECT count, first_used FROM filter_usage WHERE user_id=? AND first_used > ?",
            (uid, cutoff)
        )).fetchone()
        if r:
            await db.execute("UPDATE filter_usage SET count=count+1, first_used=? WHERE user_id=?", (now_iso, uid))
        else:
            await db.execute("INSERT OR REPLACE INTO filter_usage (user_id,count,first_used) VALUES (?,1,?)", (uid, now_iso))
        await db.commit()

async def save_filter_history(uid, pattern):
    async with aiosqlite.connect(DB_PATH) as db:
        # Удаляем дубликат если уже есть
        await db.execute("DELETE FROM filter_history WHERE user_id=? AND pattern=?", (uid, pattern))
        await db.execute("INSERT INTO filter_history (user_id,pattern) VALUES (?,?)", (uid, pattern))
        # Оставляем только последние 3 уникальных
        await db.execute("""
            DELETE FROM filter_history WHERE user_id=? AND id NOT IN (
                SELECT id FROM filter_history WHERE user_id=? ORDER BY id DESC LIMIT 3
            )
        """, (uid, uid))
        await db.commit()

async def get_filter_history(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT pattern FROM filter_history WHERE user_id=? ORDER BY id DESC LIMIT 3",
            (uid,)
        )).fetchall()
        return [r[0] for r in rows]

async def get_stats(period="all"):
    async with aiosqlite.connect(DB_PATH) as db:
        if period == "day":
            df = datetime.now().strftime("%Y-%m-%d")
            nu = (await (await db.execute("SELECT COUNT(*) FROM users WHERE created_at>=?", (df,))).fetchone())[0]
            ns = (await (await db.execute("SELECT COUNT(*) FROM search_log WHERE created_at>=?", (df,))).fetchone())[0]
        elif period == "week":
            df = (datetime.now() - timedelta(days=7)).isoformat()
            nu = (await (await db.execute("SELECT COUNT(*) FROM users WHERE created_at>=?", (df,))).fetchone())[0]
            ns = (await (await db.execute("SELECT COUNT(*) FROM search_log WHERE created_at>=?", (df,))).fetchone())[0]
        else:
            nu = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            ns = (await (await db.execute("SELECT COUNT(*) FROM search_log")).fetchone())[0]
        tu = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        sa = (await (await db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE expires_at>?", (datetime.now().isoformat(),)
        )).fetchone())[0]
        bl = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1")).fetchone())[0]
        at = (await (await db.execute("SELECT COUNT(*) FROM traps WHERE active=1")).fetchone())[0]
        # Новые метрики
        total_found = (await (await db.execute("SELECT SUM(total_found) FROM users")).fetchone())[0] or 0
        wm_count = (await (await db.execute("SELECT COUNT(*) FROM watermarks")).fetchone())[0]
        paid_ever = (await (await db.execute("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status='paid'")).fetchone())[0]
        # Топ юзеров по поискам
        top_rows = await (await db.execute(
            "SELECT user_id, username, total_found FROM users ORDER BY total_found DESC LIMIT 5"
        )).fetchall()
        return {
            "new_users": nu, "searches": ns, "total_users": tu, "active_subs": sa,
            "blocked": bl, "active_traps": at, "total_found": total_found,
            "wm_count": wm_count, "paid_ever": paid_ever, "top_users": top_rows
        }

async def get_all_uids():
    async with aiosqlite.connect(DB_PATH) as db:
        return [r[0] for r in await (await db.execute("SELECT user_id FROM users WHERE is_blocked=0")).fetchall()]

async def get_watermark(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        r = await (await db.execute("SELECT watermark_text FROM watermarks WHERE user_id=?", (uid,))).fetchone()
        return r[0] if r else None

async def set_watermark(uid, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO watermarks (user_id, watermark_text) VALUES (?,?)",
            (uid, text)
        )
        await db.commit()

async def remove_watermark(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM watermarks WHERE user_id=?", (uid,))
        await db.commit()


# ── ГЕНЕРАЦИЯ / ПРОВЕРКА ──────────────────────────────────────
def gen_username(length):
    pat = random.choice(PATTERNS_5 if length == 5 else PATTERNS_6)
    return "".join(random.choice(VOWELS if c == "V" else CONSONANTS) for c in pat)

GOOD_BIGRAMS = set([
    'al','an','ar','as','at','el','en','er','es','in','is','it','on','or','re',
    'la','le','li','lo','lu','ma','me','mi','mo','mu','na','ne','ni','no','nu',
    'ra','ro','ri','ru','sa','se','si','so','su','ta','te','ti','to','tu',
    'ka','ke','ki','ko','ku','ba','be','bi','bo','bu','da','de','di','do','du',
    'fa','fe','fi','fo','ga','ge','go','ha','he','hi','ho','hu',
    'pa','pe','pi','po','pu','va','ve','vi','vo','za','ze','zo',
    'xa','xe','xi','xo','xu','ax','ex','ix','ox','ux',
    'ny','ky','ry','ty','dy','by','my','ly','sy','zy',
    'br','cr','dr','fr','gr','pr','tr','st','sp','sc','sk','sl','sm','sn','sw',
    'nd','nt','ng','nk','ld','lt','lk','rd','rt','rk','mp','mb',
    'uk','us','um','un','ul','ur','uz','ku','zu','kz',
    'wo','wa','we','wi','ow','aw','ew','wn',
    'zo','iz','oz','az','av','ov','ev','iv',
])

def calc_liq(username):
    """
    Ликвидность юзернейма 1-10.
    Оценивается только красота и запоминаемость — никаких штрафных букв.
    """
    u = username.lower()
    score = 3

    # Главный критерий — насколько пары букв звучат естественно
    bigrams = [u[i:i+2] for i in range(len(u) - 1)]
    good = sum(1 for b in bigrams if b in GOOD_BIGRAMS)
    good_ratio = good / len(bigrams)

    if good_ratio == 1.0:    score += 6
    elif good_ratio >= 0.75: score += 5
    elif good_ratio >= 0.5:  score += 3
    elif good_ratio >= 0.25: score += 1

    # Чередование гласных/согласных
    alt = sum(1 for i in range(len(u) - 1) if (u[i] in VOWELS) != (u[i+1] in VOWELS))
    alt_ratio = alt / (len(u) - 1) if len(u) > 1 else 0
    if alt_ratio >= 0.8:   score += 2
    elif alt_ratio >= 0.5: score += 1
    elif alt_ratio <= 0.2: score -= 1

    # 3+ согласных подряд — каша
    run = mx = 0
    for c in u:
        if c in CONSONANTS:
            run += 1; mx = max(mx, run)
        else:
            run = 0
    if mx >= 3:
        score -= 2

    # 5 букв чуть лучше
    if len(u) == 5:
        score += 1

    # Все буквы уникальные
    if len(set(u)) == len(u):
        score += 1

    # Двойные буквы подряд
    for i in range(len(u) - 1):
        if u[i] == u[i+1]:
            score -= 1
            break

    # Штраф за длину > 6
    if len(u) > 6:
        score -= 3
    return max(1, min(10, score))

async def tme_taken(username, session):
    try:
        async with session.get(
            "https://t.me/" + username, headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=8), allow_redirects=True
        ) as r:
            return "tgme_page_title" in await r.text()
    except Exception:
        return True

async def frag_on_auction(username, session):
    """Проверяет Fragment по размеру страницы. При ошибке соединения — 2 повтора."""
    url = "https://fragment.com/username/" + username.lower()
    for attempt in range(3):
        try:
            async with session.get(
                url, headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=6),
                allow_redirects=True
            ) as r:
                if r.status == 200:
                    text = await r.text()
                    if len(text) > 20000:
                        logger.info("@" + username + " on Fragment (len=" + str(len(text)) + ") - skip")
                        return True
                    return False  # страница маленькая — ника нет на Fragment
                return False
        except Exception as e:
            logger.warning("Fragment check error @" + username + " attempt " + str(attempt + 1) + ": " + str(e))
            if attempt < 2:
                await asyncio.sleep(0.5)
    # Все 3 попытки упали — Fragment недоступен, не блокируем ник
    return False

async def ton_dns_taken(username, session):
    """Проверяет занят ли ник в блокчейне через t.me."""
    try:
        async with session.get(
            "https://t.me/" + username, headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=8), allow_redirects=True
        ) as r:
            text = await r.text()
            has_title = "tgme_page_title" in text
            has_frag = "fragment.com" in text
            has_nft = "nft" in text.lower()
            logger.info("TON_CHECK @" + username + " title=" + str(has_title) + " frag=" + str(has_frag) + " nft=" + str(has_nft) + " len=" + str(len(text)))
            if has_frag or has_nft:
                logger.info("@" + username + " taken in blockchain - skip")
                return True
    except Exception as e:
        logger.warning("TON check error @" + username + ": " + str(e))
    return False

async def check_one(uname, session, blocked=None):
    """Проверяет ник через t.me и Fragment. Фильтрует заблокированные буквы юзера + j по умолчанию."""
    default_blocked = {'j'}
    all_blocked = default_blocked | (blocked or set())
    if any(c in uname for c in all_blocked):
        return None
    if await tme_taken(uname, session):
        return None
    if await frag_on_auction(uname, session):
        return None
    return {"username": uname, "liquidity": calc_liq(uname)}


async def search_free(length, blocked=None):
    """Генерирует пачку ников и проверяет их параллельно по 10 штук за раз."""
    # Чем больше заблокированных букв — тем больше попыток нужно
    extra = len(blocked) * 20 if blocked else 0
    max_attempts = 60 + extra
    async with aiohttp.ClientSession() as session:
        batch_size = 10
        total_attempts = 0
        while total_attempts < max_attempts:
            batch = list({gen_username(length) for _ in range(batch_size)})
            total_attempts += len(batch)
            logger.info("Checking batch: " + str(batch))
            tasks = [check_one(u, session, blocked) for u in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if res and isinstance(res, dict):
                    logger.info("FREE @" + res["username"])
                    return res
        return None

async def generate_mask_candidates(word: str) -> list:
    """Генерирует 5000 уникальных кандидатов для слова."""
    bad_chars = set("jxqzwy")
    good_vowels = "aeiou"
    good_cons = "bcdfgklmnprst"
    good_1 = good_vowels + good_cons
    good_2 = ["al", "an", "ar", "at", "el", "en", "er", "es", "et", "in", "is",
              "it", "ol", "on", "or", "os", "ul", "un", "us", "ak", "ok", "ek",
              "ob", "ot", "ox", "bo", "co", "de", "go", "ka", "le", "li", "lo",
              "ma", "mo", "na", "no", "po", "re", "ro", "se", "te", "to", "ve",
              "ba", "be", "bi", "bu", "da", "di", "do", "du", "fa", "fi", "fo",
              "ga", "gi", "gu", "ha", "hi", "ho", "hu", "la", "lu", "me", "mi",
              "mu", "ne", "ni", "nu", "pa", "pe", "pi", "pu", "ra", "ri", "ru",
              "sa", "si", "so", "su", "ta", "ti", "tu", "va", "vi", "vo", "vu"]

    pool = set()

    def ok(u):
        return 3 <= len(u) <= 10 and not any(c in bad_chars for c in u)

    # Определяем последнюю и первую буквы слова для паттерна
    last = word[-1]
    first = word[0]
    is_last_vowel = last in good_vowels
    is_first_vowel = first in good_vowels

    # word + 1 буква (по паттерну CV/VC)
    # После гласной — согласная, после согласной — гласная
    next_chars = good_cons if is_last_vowel else good_vowels
    prev_chars = good_cons if is_first_vowel else good_vowels
    for c in next_chars:
        u = word + c
        if ok(u): pool.add(u)
    for c in prev_chars:
        u = c + word
        if ok(u): pool.add(u)

    # word + 2 буквы CV или VC паттерн
    # Суффикс: если последняя буква слова гласная — CV (согласная+гласная), иначе VC
    cv_pairs = [(v, c) for v in good_vowels for c in good_cons]  # VC
    vc_pairs = [(c, v) for c in good_cons for v in good_vowels]  # CV
    suf_pairs = vc_pairs if is_last_vowel else cv_pairs  # после гласной — CV, после согласной — VC
    pre_pairs = cv_pairs if is_first_vowel else vc_pairs  # перед гласной — CV, перед согласной — VC
    for a, b in suf_pairs:
        u = word + a + b
        if ok(u): pool.add(u)
    for a, b in pre_pairs:
        u = a + b + word
        if ok(u): pool.add(u)

    # Словарные слова как суффиксы/префиксы
    word_tags = ["top","pro","max","min","big","hot","win","get","set","run","fit","cut","mix","fix","box","fox","log","tag","key","map","cap","tap","zip","pop","pay","buy","try","use","add","sub","dev","web","app","bot","net","lab","hub","gen","kit","sys","api","end","bit","cod","mob"]
    for w in word_tags:
        u = word + w
        if ok(u): pool.add(u)
        u = w + word
        if ok(u): pool.add(u)

    result = list(pool)
    random.shuffle(result)
    return result


async def search_by_mask(word, count=1, blocked=None):
    """Ищет count уникальных свободных ников похожих на слово."""
    word = word.lower().strip()
    candidates = await generate_mask_candidates(word)
    logger.info("MASK candidates for " + word + ": " + str(len(candidates)))
    results = []
    found_names = set()
    async with aiohttp.ClientSession() as session:
        for uname in candidates:
            if len(results) >= count:
                break
            r = await check_one(uname, session, blocked)
            if r and uname not in found_names:
                found_names.add(uname)
                results.append(r)
                logger.info("MASK FREE @" + uname)
    return results

async def search_by_filter(pattern, blocked=None):
    """Ищет ник по паттерну. ? = согласная, ! = гласная, буква = конкретная буква.
    Blocked применяется только к рандомным позициям (? и !), не к буквам которые юзер вписал сам."""
    pattern = pattern.lower()
    # Буквы которые юзер явно указал в паттерне — не блокируем
    explicit_letters = {ch for ch in pattern if ch not in ('?', '!')}
    effective_blocked = (blocked or set()) - explicit_letters - {'j'}
    # j блокируем только если не вписана явно
    default_blocked = set() if 'j' in explicit_letters else {'j'}
    effective_blocked = effective_blocked | default_blocked

    # Убираем из пула согласных/гласных заблокированные буквы
    consonants = [c for c in CONSONANTS if c not in effective_blocked]
    vowels = [v for v in VOWELS if v not in effective_blocked]
    if not consonants:
        consonants = list(CONSONANTS)
    if not vowels:
        vowels = list(VOWELS)

    extra = len(effective_blocked) * 30 if effective_blocked else 0
    max_attempts = 200 + extra
    async with aiohttp.ClientSession() as session:
        total_attempts = 0
        while total_attempts < max_attempts:
            uname = ""
            for ch in pattern:
                if ch == "?":
                    uname += random.choice(consonants)
                elif ch == "!":
                    uname += random.choice(vowels)
                else:
                    uname += ch
            total_attempts += 1
            # Для явных букв проверяем только занятость, не blocked
            if await tme_taken(uname, session):
                continue
            if await frag_on_auction(uname, session):
                continue
            logger.info("FILTER FREE @" + uname)
            return {"username": uname, "liquidity": calc_liq(uname)}
        return None


# ── ФОТО ──────────────────────────────────────────────────────
def get_photo():
    if os.path.exists(PHOTO_PATH):
        return FSInputFile(PHOTO_PATH)
    return None

async def send_photo(target, text, reply_markup=None, parse_mode="HTML"):
    """Отправить фото с подписью или просто текст если фото нет. target = Message"""
    photo = get_photo()
    kw = {"parse_mode": parse_mode}
    if reply_markup:
        kw["reply_markup"] = reply_markup
    if photo:
        await target.answer_photo(photo, caption=text, **kw)
    else:
        await target.answer(text, **kw)

async def bot_send_photo(bot, uid, text, reply_markup=None, parse_mode="HTML"):
    """То же самое но через bot объект"""
    photo = get_photo()
    kw = {"parse_mode": parse_mode}
    if reply_markup:
        kw["reply_markup"] = reply_markup
    if photo:
        await bot.send_photo(uid, get_photo(), caption=text, **kw)
    else:
        await bot.send_message(uid, text, **kw)


# ── КЛАВИАТУРЫ ────────────────────────────────────────────────
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="5 Букв 🔍"), KeyboardButton(text="6 Букв 🔍")],
        [KeyboardButton(text="🌪️ Маска"), KeyboardButton(text="🎲 Фильтр")],
        [KeyboardButton(text="🎯 Ловушка"), KeyboardButton(text="🏪 Маркет")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🌟 Подписка")],
    ], resize_keyboard=True)

def channel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Наш канал 🤍", url="https://t.me/" + CHANNEL_USERNAME)]
    ])

def plans_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день – 95₽ / 1.2$ / 65 ⭐", callback_data="plan_1d")],
        [InlineKeyboardButton(text="3 дня – 145₽ / 1.8$ / 100 ⭐", callback_data="plan_3d")],
        [InlineKeyboardButton(text="7 дней – 315₽ / 3.8$ / 215 ⭐", callback_data="plan_7d")],
        [InlineKeyboardButton(text="30 дней – 769₽ / 9.5$ / 530 ⭐", callback_data="plan_30d")],
    ])

def pay_method_kb(plan_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="СБП / РФ Карты / Звёзды", callback_data="pay_manual_" + plan_id)],
        [InlineKeyboardButton(text="CryptoBot (@send)", callback_data="pay_crypto_" + plan_id)],
        [InlineKeyboardButton(text="‹ Назад", callback_data="sub_menu")],
    ])

def back_kb(cb="back_main"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ Назад", callback_data=cb)]])

def build_trap_text(traps, max_traps, has_sub, hide_list=False):
    lines = [
        "🎯 <b>Ловушка</b>",
        "",
        "<b>Ловушка моментально уведомит вас, как только отслеживаемый юзернейм освободится.</b>",
        "",
        "<b>⚡ Слотов: " + str(len(traps)) + " из " + str(max_traps) + ("" if has_sub else " (С подпиской: 10 🌟)") + "</b>",
        "",
        "<b>Активные ловушки:</b>",
    ]
    return "\n".join(lines)

def traps_kb(traps, max_traps=10):
    del_btns = [InlineKeyboardButton(text="🗑️ @" + u, callback_data="trap_del_" + str(tid)) for tid, u in traps]
    # Если ловушек много — по 2 в ряд, иначе по 1
    if len(traps) > 5:
        btns = [del_btns[i:i+2] for i in range(0, len(del_btns), 2)]
    else:
        btns = [[b] for b in del_btns]
    if len(traps) < max_traps:
        btns.append([InlineKeyboardButton(text="➕ Добавить ник", callback_data="trap_add")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Рулетка", callback_data="roulette_open")],
        [InlineKeyboardButton(text="👥 Реферальная система", callback_data="ref_menu")],
        [InlineKeyboardButton(text="🎫 Активировать промокод", callback_data="promo_input")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu")],
    ])

def admin_kb():
    import time as _time
    ft_active = freetime_until > _time.time()
    ft_btn_txt = "🟢 Фришно АКТИВНО — отменить" if ft_active else "🎉 Сделать фришно"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👤 Юзер по ID", callback_data="admin_user_info")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"),
         InlineKeyboardButton(text="🔄 Рассылка меню", callback_data="admin_broadcast_menu")],
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="admin_block"),
         InlineKeyboardButton(text="✅ Разбанить", callback_data="admin_unblock")],
        [InlineKeyboardButton(text="🎫 Создать промо", callback_data="admin_promo_create"),
         InlineKeyboardButton(text="🗑 Удалить промо", callback_data="admin_promo_list")],
        [InlineKeyboardButton(text="⭐ Выдать подписку", callback_data="admin_give_sub")],
        [InlineKeyboardButton(text="🎰 Выдать крутки", callback_data="admin_give_spins")],
        [InlineKeyboardButton(text="🎁 Выдать запросы", callback_data="admin_give_bonus")],
        [InlineKeyboardButton(text=ft_btn_txt, callback_data="admin_freetime")],
        [InlineKeyboardButton(text="⏱ Изменить КД поиска", callback_data="admin_set_cooldown")],
        [InlineKeyboardButton(text="💧 Водяной знак", callback_data="admin_watermark")],
        [InlineKeyboardButton(text="🗑 Удалить отзыв", callback_data="admin_del_review")],
        [InlineKeyboardButton(text="🗑 ОТОЗВАТЬ ВСЕ ПОДПИСКИ", callback_data="admin_revoke_all_subs_1")],
    ])

def stats_period_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="День", callback_data="stats_day"),
         InlineKeyboardButton(text="Неделя", callback_data="stats_week"),
         InlineKeyboardButton(text="Всё время", callback_data="stats_all")],
        [InlineKeyboardButton(text="‹ Назад", callback_data="admin_panel")],
    ])


# ── FSM ───────────────────────────────────────────────────────
class S(StatesGroup):
    trap_uname = State()
    promo = State()
    broadcast = State()
    broadcast_menu = State()
    admin_del_review_username = State()
    block_id = State()
    unblock_id = State()
    promo_code = State()
    promo_days = State()
    promo_uses = State()
    give_sub_id = State()
    give_sub_type = State()
    give_sub_days = State()
    give_bonus_id = State()
    give_bonus_count = State()
    give_spins_id = State()
    give_spins_count = State()
    filter_input = State()
    mask_input = State()
    freetime_duration = State()
    freetime_types_pick = State()
    freetime_cooldown_set = State()
    watermark_add_id = State()
    watermark_remove_id = State()
    user_info_id = State()
    blocked_letter_input = State()
    market_sell_username = State()
    market_sell_price = State()
    market_review_stars = State()
    market_review_text = State()


# ── CRYPTOBOT ─────────────────────────────────────────────────
async def get_ton_rate() -> float:
    """Возвращает курс TON/USDT через CryptoBot API."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                CRYPTOBOT_API + "/getExchangeRates",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
            ) as r:
                data = await r.json()
                for rate in data.get("result", []):
                    if rate["source"] == "TON" and rate["target"] == "USDT":
                        return float(rate["rate"])
    except Exception as e:
        logger.error("getExchangeRates: " + str(e))
    return 1.25  # fallback если API недоступен


async def create_invoice(amount, currency, uid, plan_id):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                CRYPTOBOT_API + "/createInvoice",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                json={
                    "asset": currency, "amount": str(amount),
                    "description": "Подписка " + PLANS[plan_id]["label"] + " - Hermes Search",
                    "payload": str(uid) + ":" + plan_id, "expires_in": 3600
                }
            ) as r:
                data = await r.json()
                return data["result"] if data.get("ok") else None
    except Exception as e:
        logger.error("CryptoBot: " + str(e))
        return None

async def check_payments(bot):
    while True:
        await asyncio.sleep(30)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                rows = await (await db.execute(
                    "SELECT invoice_id,user_id,plan FROM payments WHERE method='crypto' AND status='pending'"
                )).fetchall()
            for inv_id, uid, plan_id in rows:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        CRYPTOBOT_API + "/getInvoices",
                        headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                        params={"invoice_ids": inv_id}
                    ) as r:
                        data = await r.json()
                        items = data.get("result", {}).get("items", [])
                        if items and items[0]["status"] == "paid":
                            async with aiosqlite.connect(DB_PATH) as db:
                                await db.execute("UPDATE payments SET status='paid' WHERE invoice_id=?", (inv_id,))
                                await db.commit()
                            await add_subscription(uid, PLANS[plan_id]["days"] * 24 * 60)
                            plan = PLANS[plan_id]
                            await bot_send_photo(
                                bot, uid,
                                "✅ <b>Оплата получена!</b>\nПодписка <b>" + plan["label"] + "</b> активирована!"
                            )
                            # Уведомление админам
                            try:
                                user_info = await bot.get_chat(uid)
                                uname = user_info.username or ""
                            except Exception:
                                uname = ""
                            uname_txt = ("@" + uname) if uname else str(uid)
                            currency = items[0].get("asset", "CRYPTO")
                            amount = items[0].get("amount", "?")
                            notif = (
                                "💰 <b>Новая покупка!</b>\n\n"
                                "👤 Юзер: " + uname_txt + " (<code>" + str(uid) + "</code>)\n"
                                "📦 Тариф: <b>" + plan["label"] + "</b>\n"
                                "💳 Метод: CryptoBot (" + str(currency) + ")\n"
                                "💵 Сумма: " + str(amount) + " " + str(currency)
                            )
                            for admin_id in ADMIN_IDS:
                                try:
                                    await bot.send_message(admin_id, notif, parse_mode="HTML")
                                except Exception:
                                    pass
        except Exception as e:
            logger.error("Payment check: " + str(e))


async def is_subbed(bot, uid):
    try:
        m = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=uid)
        return m.status not in ("left", "kicked", "banned")
    except Exception:
        return False


async def build_watermark_suffix(uid):
    """Возвращает строку водяного знака для пользователя или пустую строку."""
    wm = await get_watermark(uid)
    if not wm:
        return ""
    return "\n\n<b>" + wm + "</b>"


# ── MAIN ──────────────────────────────────────────────────────
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext):
        await state.clear()
        uid = message.from_user.id
        uname = message.from_user.username or ""
        fname = message.from_user.full_name or ""
        ref_by = None
        parts = message.text.split()
        if len(parts) > 1 and parts[1].startswith("ref"):
            try:
                rb = int(parts[1][3:])
                if rb != uid:
                    ref_by = rb
            except ValueError:
                pass
        is_new = await ensure_user(uid, uname, fname, ref_by)
        if await is_blocked(uid):
            await message.answer("<b>❌ Вы заблокированы.</b>", parse_mode="HTML")
            return
        if not await is_subbed(bot, uid):
            await state.update_data(ref_by=ref_by, is_new=is_new)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Подписаться", url="https://t.me/" + CHANNEL_USERNAME)],
                [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
            ])
            await send_photo(message, "👋 Привет!\n\nДля использования бота подпишись на @" + CHANNEL_USERNAME, reply_markup=kb)
            return
        if is_new and ref_by:
            await add_bonus_searches(ref_by, 2)
            await add_bonus_filter(ref_by, 1)
            await add_bonus_mask(ref_by, 1)
            try:
                await bot.send_message(ref_by, "🎁 <b>+2 поиска, +1 фильтр, +1 маска за реферала!</b>", parse_mode="HTML")
            except Exception:
                pass
        fname = message.from_user.first_name or message.from_user.username or ""
        await message.answer("👋", reply_markup=main_kb())
        await send_photo(
            message,
            "👋 <b>Привет, " + fname + "!\n\nС помощью нашего бота ты можешь искать красивые, а самое главное свободные 5-6 буквенные юзернеймы для себя или продажи ⚡</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Наш канал 🤍", url="https://t.me/" + CHANNEL_USERNAME)]
            ])
        )

    @dp.callback_query(F.data == "check_sub")
    async def check_sub_cb(call: CallbackQuery, state: FSMContext):
        if not await is_subbed(bot, call.from_user.id):
            await call.answer("❌ Ты ещё не подписан!", show_alert=True)
            return
        await call.message.delete()
        uid = call.from_user.id
        fname = call.from_user.first_name or call.from_user.username or ""
        data = await state.get_data()
        ref_by = data.get("ref_by")
        is_new = data.get("is_new", False)
        await state.clear()
        await ensure_user(uid, call.from_user.username or "", call.from_user.full_name or "", ref_by)
        if is_new and ref_by:
            await add_bonus_searches(ref_by, 2)
            await add_bonus_filter(ref_by, 1)
            await add_bonus_mask(ref_by, 1)
            try:
                await bot.send_message(ref_by, "🎁 <b>+2 поиска, +1 фильтр, +1 маска за реферала!</b>", parse_mode="HTML")
            except Exception:
                pass
        await bot.send_message(uid, "👋", reply_markup=main_kb())
        await bot_send_photo(
            bot, uid,
            "👋 <b>Привет, " + fname + "!\n\nС помощью нашего бота ты можешь искать красивые, а самое главное свободные 5-6 буквенные юзернеймы для себя или продажи ⚡</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Наш канал 🤍", url="https://t.me/" + CHANNEL_USERNAME)]
            ])
        )

    @dp.callback_query(F.data == "back_main")
    async def back_main_cb(call: CallbackQuery):
        await call.message.delete()

    # ── ПОИСК ────────────────────────────────────────────────
    @dp.message(F.text.in_({"5 Букв 🔍", "6 Букв 🔍"}))
    async def search_h(message: Message):
        uid = message.from_user.id
        if await is_blocked(uid):
            return
        if not await is_subbed(bot, uid):
            await message.answer("❌ Подпишись на @" + CHANNEL_USERNAME)
            return
        now = time.time()
        # КД: если фришно активно — используем freetime_cooldown, иначе SEARCH_COOLDOWN
        active_cd = freetime_cooldown if (freetime_until > now and freetime_cooldown is not None) else SEARCH_COOLDOWN
        rem = active_cd - (now - search_cooldowns.get(uid, 0))
        if rem > 0:
            await message.answer("<b>⏱️ Подожди " + str(int(rem) + 1) + " сек.</b>", parse_mode="HTML")
            return
        search_cooldowns[uid] = now
        has_sub = await has_subscription(uid)
        search_is_free = freetime_until > now and "search" in freetime_types
        if not has_sub and not search_is_free:
            bonus = await get_bonus_searches(uid)
            usage, last_used = await get_daily_usage(uid)
            if bonus <= 0 and usage >= FREE_DAILY_LIMIT:
                if last_used:
                    reset_at = datetime.fromisoformat(last_used.replace(" ", "T")) + timedelta(hours=24)
                    diff = reset_at - datetime.now()
                    total_mins = max(0, int(diff.total_seconds() // 60))
                    hrs = total_mins // 60
                    mins = total_mins % 60
                    time_txt = (str(hrs) + " ч. " if hrs else "") + str(mins) + " мин."
                else:
                    time_txt = "24 ч."
                await message.answer(
                    "❌ <b>Бесплатные запросы закончились, приходите через "
                    + time_txt + " или приобретите подписку для безлимитных поисков</b>",
                    parse_mode="HTML"
                )
                return
        length = 5 if "5" in message.text else 6
        sc = await get_search_count(uid)
        if not has_sub:
            sc = 1  # бесплатные всегда 1
        blocked = await get_blocked_letters(uid)
        search_txt = "🔍 Ищу свободные ники..." if sc > 1 else "🔍 Ищу свободный ник..."
        wait_msg = await message.answer("<b>" + search_txt + "</b>", parse_mode="HTML")
        results = []
        for _ in range(sc):
            r = await search_free(length, blocked)
            if r:
                results.append(r)
        await wait_msg.delete()
        if not results:
            await message.answer("<b>😔 Не удалось найти свободный ник. Попробуй ещё раз!</b>", parse_mode="HTML")
            return
        if not has_sub and not search_is_free:
            used_bonus = await use_bonus_search(uid)
            if not used_bonus:
                usage_now, _ = await get_daily_usage(uid)
                if usage_now < FREE_DAILY_LIMIT:
                    await inc_daily_usage(uid)
        for r in results:
            await inc_total_found(uid)
            await log_search(uid, r["username"], length)
        if len(results) == 1:
            u = results[0]["username"]
            liq = results[0]["liquidity"]
            if has_sub:
                remaining_txt = "∞"
            else:
                bonus = await get_bonus_searches(uid)
                usage, _ = await get_daily_usage(uid)
                remaining_txt = str(max(0, FREE_DAILY_LIMIT - usage) + bonus) + " из " + str(FREE_DAILY_LIMIT)
            wm_suffix = await build_watermark_suffix(uid)
            if wm_suffix:
                text = (
                    "<b>Ник найден!</b> ✅\n\n"
                    "<b>Ник -</b> @" + u + " › <code>" + u + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(liq) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>"
                    + wm_suffix
                )
            else:
                text = (
                    "<b>Ник найден!</b> ✅\n\n"
                    "<b>Ник -</b> @" + u + " › <code>" + u + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(liq) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>\n\n"
                    "<b>🤍 Осталось запросов на сегодня - " + remaining_txt + "</b>"
                )
        else:
            lines = ["<b>Ники найдены!</b> ✅"]
            for r in results:
                lines.append(
                    "\n<b>Ник -</b> @" + r["username"] + " › <code>" + r["username"] + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(r["liquidity"]) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>"
                )
            wm_suffix = await build_watermark_suffix(uid)
            text = "\n".join(lines) + wm_suffix
        await send_photo(message, text, reply_markup=channel_kb())

    # ── ФИЛЬТР ───────────────────────────────────────────────
    @dp.message(F.text == "🎲 Фильтр")
    async def filter_h(message: Message, state: FSMContext):
        uid = message.from_user.id
        if await is_blocked(uid):
            return
        has_sub = await has_subscription(uid)
        usage, last_used = await get_filter_usage(uid)
        if not has_sub:
            remaining = max(0, FREE_FILTER_LIMIT - usage)
            limit_txt = str(remaining) + " из " + str(FREE_FILTER_LIMIT)
        else:
            limit_txt = "∞"
        history = await get_filter_history(uid)
        text = (
            "🔎 <b>Фильтр</b>\n\n"
            "<b>Введите фильтр (5-10 символов)\n"
            "? = Согласная буква\n"
            "! = Гласная буква</b>\n\n"
            "<b>Примеры:</b> <code>a!c?e</code> | <code>s?!?a</code> | <code>?afas</code>\n\n"
            "⚡️ <b>Осталось запросов за сегодня: " + limit_txt + "</b>"
        )
        btns = []
        if history:
            text += "\n\n<b>Последние фильтры:</b>  <i>(Нажмите чтобы получить юз с этим фильтром)</i>"
            for p in history:
                btns.append([InlineKeyboardButton(text=p, callback_data="use_filter_" + p)])
        kb = InlineKeyboardMarkup(inline_keyboard=btns) if btns else None
        await state.set_state(S.filter_input)
        await send_photo(message, text, reply_markup=kb)

    async def process_filter(uid, pattern, send_fn):
        """Общая логика поиска по фильтру."""
        has_sub = await has_subscription(uid)
        filter_is_free = freetime_until > time.time() and "filter" in freetime_types
        if not has_sub and not filter_is_free:
            usage, last_used = await get_filter_usage(uid)
            if usage >= FREE_FILTER_LIMIT:
                if last_used:
                    reset_at = datetime.fromisoformat(last_used) + timedelta(hours=24)
                    diff = reset_at - datetime.now()
                    total_mins = max(0, int(diff.total_seconds() // 60))
                    hrs = total_mins // 60
                    mins = total_mins % 60
                    time_txt = (str(hrs) + " ч. " if hrs else "") + str(mins) + " мин."
                else:
                    time_txt = "24 ч."
                await send_fn("❌ <b>Запросы фильтра закончились, приходите через " + time_txt + " или приобретите подписку</b>")
                return

        pattern = pattern.lower().strip()
        if not (5 <= len(pattern) <= 10) or not all(c in "abcdefghijklmnopqrstuvwxyz?!" for c in pattern):
            await send_fn("❌ <b>Неверный формат. Используй 5-10 символов: буквы, ? (согласная), ! (гласная)</b>")
            return

        result = await search_by_filter(pattern, await get_blocked_letters(uid))
        if not result:
            await send_fn("😔 <b>Не удалось найти ник по этому фильтру. Попробуй другой!</b>")
            return

        if not has_sub and not filter_is_free:
            used_bf = await use_bonus_filter(uid)
            if not used_bf:
                await inc_filter_usage(uid)
        await save_filter_history(uid, pattern)
        await inc_total_found(uid)

        u = result["username"]
        liq = result["liquidity"]
        wm_suffix = await build_watermark_suffix(uid)
        text = (
            "<b>Ник найден!</b> ✅\n\n"
            "<b>Ник -</b> @" + u + " › <code>" + u + "</code>\n"
            "<b>├ Ликвидность -</b> " + str(liq) + " из 10 ⭐\n"
            "<b>╰ Свободен ⚡</b>"
            + wm_suffix
        )
        await send_fn(text, is_result=True)

    @dp.message(S.filter_input)
    async def filter_input_h(message: Message, state: FSMContext):
        uid = message.from_user.id
        MENU_BUTTONS = {"5 Букв 🔍", "6 Букв 🔍", "🌪️ Маска", "🎲 Фильтр", "🎯 Ловушка", "🏪 Маркет", "👤 Профиль", "🌟 Подписка"}
        if message.text and message.text.strip() in MENU_BUTTONS:
            await state.clear()
            # Вызываем нужный хендлер напрямую по тексту кнопки
            btn = message.text.strip()
            if btn in ("5 Букв 🔍", "6 Букв 🔍"):
                await search_h(message)
            elif btn == "🌪️ Маска":
                await mask_h(message, state)
            elif btn == "🎲 Фильтр":
                await filter_h(message, state)
            elif btn == "🎯 Ловушка":
                await trap_h(message)
            elif btn == "🏪 Маркет":
                await market_h(message)
            elif btn == "👤 Профиль":
                await profile_h(message)
            elif btn == "🌟 Подписка":
                await sub_h(message)
            return
        pattern = message.text.strip() if message.text else ""
        wait_msg = await message.answer("<b>🔍 Ищу по фильтру...</b>", parse_mode="HTML")

        success = False

        async def send_fn(text, is_result=False):
            nonlocal success
            await wait_msg.delete()
            if is_result:
                success = True
                await send_photo(message, text, reply_markup=channel_kb())
            else:
                await message.answer(text, parse_mode="HTML")

        await process_filter(uid, pattern, send_fn)
        if success:
            await state.clear()

    @dp.callback_query(F.data.startswith("use_filter_"))
    async def use_filter_cb(call: CallbackQuery, state: FSMContext):
        await state.clear()
        uid = call.from_user.id
        pattern = call.data[11:]
        await call.answer()
        wait_msg = await call.message.answer("<b>🔍 Ищу по фильтру " + pattern + "...</b>", parse_mode="HTML")

        async def send_fn(text, is_result=False):
            await wait_msg.delete()
            if is_result:
                photo = get_photo()
                if photo:
                    await call.message.answer_photo(photo, caption=text, parse_mode="HTML", reply_markup=channel_kb())
                else:
                    await call.message.answer(text, parse_mode="HTML", reply_markup=channel_kb())
            else:
                await call.message.answer(text, parse_mode="HTML")

        await process_filter(uid, pattern, send_fn)

    # ── ЛОВУШКА ───────────────────────────────────────────────
    @dp.message(F.text == "🎯 Ловушка")
    async def trap_h(message: Message):
        uid = message.from_user.id
        if await is_blocked(uid):
            return
        has_sub = await has_subscription(uid)
        max_traps = 10 if has_sub else 2
        traps = await get_traps(uid)
        text = build_trap_text(traps, max_traps, has_sub)
        await send_photo(message, text, reply_markup=traps_kb(traps, max_traps))

    @dp.callback_query(F.data == "trap_add")
    async def trap_add_cb(call: CallbackQuery, state: FSMContext):
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        max_traps = 10 if has_sub else 2
        if len(await get_traps(uid)) >= max_traps:
            if has_sub:
                await call.answer("❌ Максимум 10 ловушек!", show_alert=True)
            else:
                await call.answer("❌ Бесплатно доступна только 1 ловушка. Купи подписку для 5 слотов!", show_alert=True)
            return
        await state.set_state(S.trap_uname)
        await call.message.answer(
            "🎯 <b>Введи юзернейм для отслеживания (без @):</b>",
            parse_mode="HTML",
            reply_markup=back_kb("trap_back")
        )
        await call.answer()

    @dp.callback_query(F.data == "trap_back")
    async def trap_back_cb(call: CallbackQuery, state: FSMContext):
        await state.clear()
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        max_traps = 10 if has_sub else 2
        traps = await get_traps(uid)
        text = build_trap_text(traps, max_traps, has_sub)
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_photo(call.message, text, reply_markup=traps_kb(traps, max_traps))

    @dp.message(S.trap_uname)
    async def trap_uname_h(message: Message, state: FSMContext):
        await state.clear()
        uname = message.text.strip().lower().replace("@", "")
        uid = message.from_user.id
        if not uname.isalpha() or len(uname) < 5 or len(uname) > 32:
            try:
                await message.delete()
            except Exception:
                pass
            uid2 = message.from_user.id
            has_sub2 = await has_subscription(uid2)
            max_traps2 = 10 if has_sub2 else 2
            traps2 = await get_traps(uid2)
            kb = traps_kb(traps2, max_traps2)
            photo = get_photo()
            err_text = "<b>❌ Некорректный юзернейм.</b>\n\n" + build_trap_text(traps2, max_traps2, has_sub2, hide_list=True)
            if photo:
                await message.answer_photo(photo, caption=err_text, parse_mode="HTML", reply_markup=kb)
            else:
                await message.answer(err_text, parse_mode="HTML", reply_markup=kb)
            return
        traps = await get_traps(uid)
        if any(u == uname for _, u in traps):
            await message.answer("<b>⚠️ Ловушка на @" + uname + " уже активна!</b>", parse_mode="HTML")
            return
        async with aiohttp.ClientSession() as session:
            taken = await tme_taken(uname, session)
            on_frag = await frag_on_auction(uname, session) if not taken else False
        if not taken and not on_frag:
            has_sub_f = await has_subscription(uid)
            max_traps_f = 10 if has_sub_f else 2
            traps_f = await get_traps(uid)
            photo = get_photo()
            err_text_f = "<b>❌ Этот юзернейм уже свободен - просто зарегай его!</b>\n\n" + build_trap_text(traps_f, max_traps_f, has_sub_f, hide_list=True)
            if photo:
                await message.answer_photo(photo, caption=err_text_f, parse_mode="HTML", reply_markup=traps_kb(traps_f, max_traps_f))
            else:
                await message.answer(err_text_f, parse_mode="HTML", reply_markup=traps_kb(traps_f, max_traps_f))
            return
        if on_frag:
            await message.answer(
                "<b>❌ Этот юзернейм стоит на продаже в Fragment</b>",
                parse_mode="HTML",
                reply_markup=back_kb("trap_back")
            )
            return
        await add_trap(uid, uname)
        await message.answer(
            "✅ <b>Ловушка установлена!</b>\n\nСлежу за: @" + uname + " ⚡",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎯 Мои ловушки", callback_data="my_traps")]
            ])
        )

    @dp.callback_query(F.data.startswith("trap_del_"))
    async def trap_del_cb(call: CallbackQuery):
        tid = int(call.data.split("_")[-1])
        await del_trap(tid)
        await call.answer()
        uid2 = call.from_user.id
        has_sub2 = await has_subscription(uid2)
        max_traps2 = 10 if has_sub2 else 2
        traps = await get_traps(uid2)
        text = build_trap_text(traps, max_traps2, has_sub2)
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_photo(call.message, text, reply_markup=traps_kb(traps, max_traps2))

    @dp.callback_query(F.data == "my_traps")
    async def my_traps_cb(call: CallbackQuery):
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        max_traps = 10 if has_sub else 2
        traps = await get_traps(uid)
        text = build_trap_text(traps, max_traps, has_sub)
        await call.answer()
        await bot_send_photo(bot, uid, text, reply_markup=traps_kb(traps, max_traps))

    # ── МАСКА ────────────────────────────────────────────────
    @dp.message(F.text == "🌪️ Маска")
    async def mask_h(message: Message, state: FSMContext):
        uid = message.from_user.id
        if await is_blocked(uid):
            return
        has_sub = await has_subscription(uid)
        usage, _ = await get_mask_usage(uid)
        if not has_sub:
            remaining = max(0, FREE_MASK_LIMIT - usage)
            limit_txt = str(remaining) + " из " + str(FREE_MASK_LIMIT)
        else:
            limit_txt = "∞"
        history = await get_mask_history(uid)
        text = (
            "🌪️ <b>Маска</b>\n\n"
            "<b>Введите слово на английском:</b>\n\n"
            "Вы можете ввести любое слово, например <code>dark</code> и вам выдаст похожие свободные юзернеймы: <code>darkis</code>, <code>darkun</code>, <code>lodark</code>\n\n"
            "⚡️ <b>Осталось запросов за сегодня: " + limit_txt + "</b>"
        )
        btns = []
        if history:
            text += "\n\n<b>Последние слова:</b>  <i>(Нажмите чтобы получить юз с этим словом)</i>"
            for w in history:
                btns.append([InlineKeyboardButton(text=w, callback_data="use_mask_" + w)])
        kb = InlineKeyboardMarkup(inline_keyboard=btns) if btns else None
        await state.set_state(S.mask_input)
        await send_photo(message, text, reply_markup=kb)

    @dp.message(S.mask_input)
    async def mask_input_h(message: Message, state: FSMContext):
        uid = message.from_user.id
        MENU_BUTTONS = {"5 Букв 🔍", "6 Букв 🔍", "🌪️ Маска", "🎲 Фильтр", "🎯 Ловушка", "🏪 Маркет", "👤 Профиль", "🌟 Подписка"}
        if message.text and message.text.strip() in MENU_BUTTONS:
            await state.clear()
            btn = message.text.strip()
            if btn in ("5 Букв 🔍", "6 Букв 🔍"):
                await search_h(message)
            elif btn == "🌪️ Маска":
                await mask_h(message, state)
            elif btn == "🎲 Фильтр":
                await filter_h(message, state)
            elif btn == "🎯 Ловушка":
                await trap_h(message)
            elif btn == "🏪 Маркет":
                await market_h(message)
            elif btn == "👤 Профиль":
                await profile_h(message)
            elif btn == "🌟 Подписка":
                await sub_h(message)
            return
        lock = get_mask_lock(uid)
        if lock.locked():
            await message.answer("<b>⏳ Подожди, поиск уже идёт...</b>", parse_mode="HTML")
            return
        word = message.text.strip().lower() if message.text else ""
        if not word.isalpha() or not word.isascii() or len(word) < 3 or len(word) > 10:
            await message.answer("<b>❌ Введи английское слово от 3 до 10 букв.</b>", parse_mode="HTML")
            return
        has_sub = await has_subscription(uid)
        mask_is_free = freetime_until > time.time() and "mask" in freetime_types
        if not has_sub and not mask_is_free:
            usage, _ = await get_mask_usage(uid)
            if usage >= FREE_MASK_LIMIT:
                await message.answer(
                    "❌ <b>Запросы маски закончились на сегодня. Приходите через 24ч или купите подписку.</b>",
                    parse_mode="HTML"
                )
                return
        async with lock:
            wait_msg = await message.answer("<b>🔍 Ищу похожие ники...</b>", parse_mode="HTML")
            sc = await get_search_count(uid)
            if not has_sub:
                sc = 1
            results = await search_by_mask(word, sc, await get_blocked_letters(uid))
        await wait_msg.delete()
        if not results:
            await message.answer("<b>😔 Не нашёл свободных ников похожих на это слово. Попробуй другое!</b>", parse_mode="HTML")
            return
        if not has_sub and not mask_is_free:
            used_bm = await use_bonus_mask(uid)
            if not used_bm:
                await inc_mask_usage(uid)
        await save_mask_history(uid, word)
        for r in results:
            await inc_total_found(uid)
        if has_sub:
            remaining_txt = "∞"
        else:
            bonus = await get_bonus_mask(uid)
            usage, _ = await get_mask_usage(uid)
            remaining_txt = str(max(0, FREE_MASK_LIMIT - usage) + bonus) + " из " + str(FREE_MASK_LIMIT)
        if len(results) == 1:
            u = results[0]["username"]
            liq = results[0]["liquidity"]
            wm_suffix = await build_watermark_suffix(uid)
            if wm_suffix:
                text = (
                    "<b>Ник найден!</b> ✅\n\n"
                    "<b>Ник -</b> @" + u + " › <code>" + u + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(liq) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>"
                    + wm_suffix
                )
            else:
                text = (
                    "<b>Ник найден!</b> ✅\n\n"
                    "<b>Ник -</b> @" + u + " › <code>" + u + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(liq) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>\n\n"
                    "<b>🤍 Осталось запросов на сегодня - " + remaining_txt + "</b>"
                )
        else:
            lines = ["<b>Ники найдены!</b> ✅"]
            for r in results:
                lines.append(
                    "\n<b>Ник -</b> @" + r["username"] + " › <code>" + r["username"] + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(r["liquidity"]) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>"
                )
            wm_suffix = await build_watermark_suffix(uid)
            text = "\n".join(lines) + wm_suffix
        await send_photo(message, text, reply_markup=channel_kb())
        await state.clear()
    async def use_mask_cb(call: CallbackQuery, state: FSMContext):
        await state.clear()
        word = call.data[9:]
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        if not has_sub:
            usage, _ = await get_mask_usage(uid)
            if usage >= FREE_MASK_LIMIT:
                await call.answer("❌ Запросы маски закончились!", show_alert=True)
                return
        lock = get_mask_lock(uid)
        if lock.locked():
            await call.answer("⏳ Уже ищу для тебя ники!", show_alert=True)
            return
        await call.answer()
        async with lock:
            wait_msg = await call.message.answer("<b>🔍 Ищу похожие ники...</b>", parse_mode="HTML")
            sc = await get_search_count(uid)
            if not has_sub:
                sc = 1
            results = await search_by_mask(word, sc, await get_blocked_letters(uid))
            await wait_msg.delete()
        if not results:
            await call.message.answer("<b>😔 Не нашёл. Попробуй другое слово!</b>", parse_mode="HTML")
            return
        if not has_sub:
            await inc_mask_usage(uid)
        for r in results:
            await inc_total_found(uid)
        if has_sub:
            remaining_txt = "∞"
        else:
            bonus = await get_bonus_mask(uid)
            usage, _ = await get_mask_usage(uid)
            remaining_txt = str(max(0, FREE_MASK_LIMIT - usage) + bonus) + " из " + str(FREE_MASK_LIMIT)
        if len(results) == 1:
            u = results[0]["username"]
            liq = results[0]["liquidity"]
            wm_suffix = await build_watermark_suffix(uid)
            if wm_suffix:
                text = (
                    "<b>Ник найден!</b> ✅\n\n"
                    "<b>Ник -</b> @" + u + " › <code>" + u + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(liq) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>"
                    + wm_suffix
                )
            else:
                text = (
                    "<b>Ник найден!</b> ✅\n\n"
                    "<b>Ник -</b> @" + u + " › <code>" + u + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(liq) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>\n\n"
                    "<b>🤍 Осталось запросов на сегодня - " + remaining_txt + "</b>"
                )
        else:
            lines = ["<b>Ники найдены!</b> ✅"]
            for r in results:
                lines.append(
                    "\n<b>Ник -</b> @" + r["username"] + " › <code>" + r["username"] + "</code>\n"
                    "<b>├ Ликвидность -</b> " + str(r["liquidity"]) + " из 10 ⭐\n"
                    "<b>╰ Свободен ⚡</b>"
                )
            wm_suffix = await build_watermark_suffix(uid)
            text = "\n".join(lines) + wm_suffix
        await send_photo(call.message, text, reply_markup=channel_kb())

    # ── НАСТРОЙКИ ─────────────────────────────────────────────
    @dp.callback_query(F.data == "settings_menu")
    async def settings_menu_cb(call: CallbackQuery):
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        sc = await get_search_count(uid)
        def sc_btn(n):
            label = ("✅ " if sc == n else "") + str(n)
            if n > 1 and not has_sub:
                return InlineKeyboardButton(text=label + " 🔒", callback_data="settings_sc_locked")
            return InlineKeyboardButton(text=label, callback_data="settings_sc_" + str(n))
        text = "⚙️ <b>Настройки</b>\n\nЗдесь вы можете настроить бота и его поиск"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Кол-во поисков", callback_data="settings_sc_menu")],
            [InlineKeyboardButton(text="🚫 Заблокированные буквы", callback_data="settings_blocked_letters")],
            [InlineKeyboardButton(text="‹ Назад", callback_data="back_profile")],
        ])
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    @dp.callback_query(F.data == "settings_sc_info")
    async def settings_sc_info_cb(call: CallbackQuery):
        await call.answer(
            "Кол-во ников за 1 поиск.\nОбычные: только 1.\nС подпиской: 1, 5 или 10.\nБольше ников = дольше поиск.",
            show_alert=True
        )

    @dp.callback_query(F.data == "settings_sc_menu")
    async def settings_sc_menu_cb(call: CallbackQuery):
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        sc = await get_search_count(uid)
        def sc_btn(n):
            label = ("✅ " if sc == n else "") + str(n)
            if n > 1 and not has_sub:
                return InlineKeyboardButton(text=label + " 🔒", callback_data="settings_sc_locked")
            return InlineKeyboardButton(text=label, callback_data="settings_sc_" + str(n))
        text = (
            "<b>🔎 Кол-во поисков</b>\n\n"
            "<b>Здесь вы можете изменить количество выдачи юзернеймов за 1 поиск\n"
            "А именно: 1, 5, 10 🤍\n\n"
            "При включении более 1 количества поисков время нахождения будет увеличиваться ⚡</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [sc_btn(1), sc_btn(5), sc_btn(10)],
            [InlineKeyboardButton(text="‹ Назад", callback_data="settings_menu")],
        ])
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    @dp.callback_query(F.data == "settings_sc_locked")
    async def settings_sc_locked_cb(call: CallbackQuery):
        await call.answer("🔒 Доступно только с подпиской!", show_alert=True)

    @dp.callback_query(F.data.startswith("settings_sc_"))
    async def settings_sc_cb(call: CallbackQuery):
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        try:
            n = int(call.data[12:])
        except Exception:
            return
        if n > 1 and not has_sub:
            await call.answer("🔒 Доступно только с подпиской!", show_alert=True)
            return
        await set_search_count(uid, n)
        sc = n
        def sc_btn(x):
            label = ("✅ " if sc == x else "") + str(x)
            if x > 1 and not has_sub:
                return InlineKeyboardButton(text=label + " 🔒", callback_data="settings_sc_locked")
            return InlineKeyboardButton(text=label, callback_data="settings_sc_" + str(x))
        text = (
            "<b>🔎 Кол-во поисков</b>\n\n"
            "<b>Здесь вы можете изменить количество выдачи юзернеймов за 1 поиск\n"
            "А именно: 1, 5, 10 🤍\n\n"
            "При включении более 1 количества поисков время нахождения будет увеличиваться ⚡</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [sc_btn(1), sc_btn(5), sc_btn(10)],
            [InlineKeyboardButton(text="‹ Назад", callback_data="settings_menu")],
        ])
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer("✅ Сохранено!")

    @dp.callback_query(F.data == "back_profile")
    async def back_profile_cb(call: CallbackQuery):
        uid = call.from_user.id
        user = await get_user(uid)
        traps = await get_traps(uid)
        sub_exp = await get_sub_expires(uid)
        has_sub = await has_subscription(uid)
        if sub_exp:
            diff = sub_exp - datetime.now()
            sub_txt = str(diff.days) + " дн. " + str(diff.seconds // 3600) + " ч."
        else:
            sub_txt = "Подписка отсутствует"
        usage, _ = await get_daily_usage(uid)
        bonus = await get_bonus_searches(uid)
        lim_txt = "∞" if has_sub else (str(FREE_DAILY_LIMIT - usage) + "/" + str(FREE_DAILY_LIMIT))
        text = (
            "👤 <b>Информация</b>\n"
            "├ ID: <code>" + str(uid) + "</code>\n"
            "╰ Найдено ников: " + str(user[5]) + "\n\n"
            "🌟 <b>Подписка</b>\n"
            "├ Осталось: " + sub_txt + "\n"
            "├ Осталось запросов на сегодня: " + lim_txt + "\n"
            "╰ Бонусных запросов с рефералов: " + str(bonus) + "\n\n"
            "🎯 <b>Ловушки</b>\n"
            "├ Активных: " + str(len(traps)) + "\n"
            "╰ Сработало: 0\n\n"
            "<i>Ловушки - сообщают вам сразу после освобождения юзернейма.</i>"
        )
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=profile_kb())
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=profile_kb())
        await call.answer()

    # ── РУЛЕТКА ──────────────────────────────────────────────
    async def show_roulette(uid, send_fn, answer_fn=None):
        has_sub = await has_subscription(uid)
        last = await get_roulette_last(uid)
        can_spin = True
        time_txt = ""
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace(" ", "T"))
                if datetime.now() - last_dt < timedelta(hours=24):
                    can_spin = False
                    diff = last_dt + timedelta(hours=24) - datetime.now()
                    hrs = int(diff.total_seconds() // 3600)
                    mins = int((diff.total_seconds() % 3600) // 60)
                    time_txt = (str(hrs) + " ч. " if hrs else "") + str(mins) + " мин."
            except Exception:
                can_spin = True
        extra = await get_extra_spins(uid)
        prizes_text = (
            "🎰 <b>Рулетка</b>\n\n"
            "<b>Крутите рулетку раз в сутки и выигрывайте призы!</b>\n\n"
            "🤍 <b>Возможные награды:</b>\n"
            "├ 1–3 запроса для обычного поиска\n"
            "├ 1–3 запроса для фильтра\n"
            "├ 1–3 запроса для маски\n"
            "╰ Подписка на 10 мин / 1 ч / 1 день\n\n"
            "🌟 <b>Бонусные призы с подпиской:</b>\n"
            "├ Подарок за 15 ⭐\n"
            "├ Подарок за 50 ⭐\n"
            "╰ Подарок за 100 ⭐"
        )
        if extra > 0:
            prizes_text += "\n\n🎟 <b>Дополнительных круток: " + str(extra) + "</b>"
        if not can_spin and extra == 0:
            prizes_text += "\n\n⏳ <b>Следующий спин через: " + time_txt + "</b>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎰 Крутить!", callback_data="roulette_spin")],
            [InlineKeyboardButton(text="‹ Назад", callback_data="back_profile")],
        ])
        await send_fn(prizes_text, kb)

    @dp.callback_query(F.data == "roulette_open")
    async def roulette_open_cb(call: CallbackQuery):
        uid = call.from_user.id
        await call.answer()
        async def send_fn(text, kb):
            try:
                await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                try:
                    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
                except Exception:
                    await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await show_roulette(uid, send_fn)

    @dp.callback_query(F.data == "roulette_spin")
    async def roulette_spin_cb(call: CallbackQuery):
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        last = await get_roulette_last(uid)
        extra = await get_extra_spins(uid)
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace(" ", "T"))
                if datetime.now() - last_dt < timedelta(hours=24):
                    if extra > 0:
                        await use_extra_spin(uid)
                    else:
                        diff = last_dt + timedelta(hours=24) - datetime.now()
                        hrs = int(diff.total_seconds() // 3600)
                        mins = int((diff.total_seconds() % 3600) // 60)
                        time_txt = (str(hrs) + " ч. " if hrs else "") + str(mins) + " мин."
                        await call.answer("⏳ Следующий спин через " + time_txt, show_alert=True)
                        return
            except Exception:
                pass
        else:
            if extra > 0:
                await use_extra_spin(uid)
        await set_roulette_last(uid)
        await call.answer()

        # Призы с вероятностями
        prizes = [
            ("search", 1, 29.5),
            ("search", 2, 29.5),
            ("search", 3, 28.0),
            ("filter", 1, 29.5 / 3),
            ("filter", 2, 29.5 / 3),
            ("filter", 3, 28.0 / 3),
            ("mask",   1, 29.5 / 3),
            ("mask",   2, 29.5 / 3),
            ("mask",   3, 28.0 / 3),
            ("sub",    10,   10.0),
            ("sub",    60,    2.5),
            ("sub",    1440,  0.05),
        ]
        if has_sub:
            prizes.append(("stars", 15,  0.005))
            prizes.append(("stars", 50,  0.005))
            prizes.append(("stars", 100, 0.005))

        all_prizes = prizes
        types = [p[0] for p in all_prizes]
        vals = [p[1] for p in all_prizes]
        weights = [p[2] for p in all_prizes]

        chosen_idx = random.choices(range(len(all_prizes)), weights=weights, k=1)[0]
        prize_type, prize_val = types[chosen_idx], vals[chosen_idx]

        # Выдаём приз
        if prize_type == "search":
            await add_bonus_searches(uid, prize_val)
            result_text = "🎉 <b>Вы выиграли " + str(prize_val) + " запрос(а) для обычного поиска!</b>"
        elif prize_type == "filter":
            await add_bonus_filter(uid, prize_val)
            result_text = "🎉 <b>Вы выиграли " + str(prize_val) + " запрос(а) для фильтра!</b>"
        elif prize_type == "mask":
            await add_bonus_mask(uid, prize_val)
            result_text = "🎉 <b>Вы выиграли " + str(prize_val) + " запрос(а) для маски!</b>"
        elif prize_type == "sub":
            await add_subscription(uid, prize_val)
            if prize_val == 10:
                sub_str = "10 минут"
            elif prize_val == 60:
                sub_str = "1 час"
            else:
                sub_str = "1 день"
            result_text = "🎉 <b>Вы выиграли подписку на " + sub_str + "!</b>"
        elif prize_type == "stars":
            result_text = "🎉 <b>Вы выиграли подарок за " + str(prize_val) + " звёзд!</b>\n\nНапишите администратору для получения."
        else:
            result_text = "🎉 Приз!"

        # Анимация рулетки
        frames = ["🎰 Крутим...", "🎲 Крутим...", "🎰 Крутим...", "🎲 Крутим...", "🎰 Крутим..."]
        anim_msg = await call.message.answer("<b>🎰 Крутим рулетку...</b>", parse_mode="HTML")
        for frame in frames:
            await asyncio.sleep(0.5)
            try:
                await anim_msg.edit_text("<b>" + frame + "</b>", parse_mode="HTML")
            except Exception:
                pass
        await asyncio.sleep(0.5)
        await anim_msg.delete()
        await call.message.answer(result_text, parse_mode="HTML")

    # ── МАРКЕТ ЮЗОВ ───────────────────────────────────────────
    MARKET_PER_PAGE = 5

    def market_page_kb(current_page, total_pages, cb_prefix="market_page_"):
        """
        Пагинация: 7 кнопок всегда.
        [1]⏮️  X-2  X-1  [X]  X+1  X+2  ⏭️[17]
        Крайние кнопки всегда кликабельны.
        Если текущая = 0: [1]⏮️ вместо 1⏮️
        Если текущая = last: ⏭️[N] вместо ⏭️N
        """
        if total_pages <= 1:
            return []

        last = total_pages - 1  # индекс последней страницы (0-based)

        # Вычисляем окно из 5 страниц вокруг текущей
        win_size = 5
        win_start = max(0, current_page - win_size // 2)
        win_end = min(last, win_start + win_size - 1)
        if win_end - win_start < win_size - 1:
            win_start = max(0, win_end - win_size + 1)
        window = list(range(win_start, win_end + 1))

        row = []

        def page_btn(p):
            label = "[" + str(p + 1) + "]" if p == current_page else str(p + 1)
            return InlineKeyboardButton(text=label, callback_data=cb_prefix + str(p))

        # Левая кнопка: 1⏮️ или [1]⏮️
        first_label = "[1]⏮️" if current_page == 0 else "1⏮️"
        row.append(InlineKeyboardButton(text=first_label, callback_data=cb_prefix + "0"))

        # 5 кнопок окна (но пропускаем страницу 0 и last — они уже в крайних кнопках)
        for p in window:
            if p == 0 or p == last:
                continue
            row.append(page_btn(p))

        # Если окно не дотягивает до 5 средних — добираем справа или слева
        # (на случай когда last совсем близко)
        # Считаем сколько средних кнопок получилось
        mid_count = len(row) - 1  # минус левая крайняя
        # Нужно ровно 5 средних кнопок
        while mid_count < 5 and win_end + 1 < last:
            win_end += 1
            p = win_end
            if p != 0 and p != last:
                row.append(page_btn(p))
                mid_count += 1

        # Правая кнопка: ⏭️N или ⏭️[N]
        last_label = "⏭️[" + str(last + 1) + "]" if current_page == last else "⏭️" + str(last + 1)
        row.append(InlineKeyboardButton(text=last_label, callback_data=cb_prefix + str(last)))

        return [row]

    MARKET_MAX_LOTS = 5  # максимум лотов на одного пользователя

    async def market_edit_photo(call, text, kb):
        """Редактирует сообщение с фото если возможно, иначе удаляет и шлёт новое с фото."""
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
        # Если edit_caption не сработал — пробуем удалить и прислать с фото
        try:
            await call.message.delete()
        except Exception:
            pass
        await bot_send_photo(bot, call.from_user.id, text, reply_markup=kb)

    async def show_market_buy(call, page=0):
        rows, total = await market_get_lots(page, MARKET_PER_PAGE)
        total_pages = max(1, (total + MARKET_PER_PAGE - 1) // MARKET_PER_PAGE)
        if not rows:
            text = (
                "🏪 <b>Маркет юзов</b>\n\n"
                "😔 <b>Пока нет активных лотов.</b>\n\n"
                "<i>Стань первым — выстави свой юзернейм на продажу!</i>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="‹ Назад", callback_data="market_main")]
            ])
        else:
            lines = ["🏪 <b>Маркет юзов</b> — <i>Выберите лот для просмотра:</i>"]
            text = "\n".join(lines)
            lot_btns = []
            for lot in rows:
                lot_id, seller_id, seller_uname, username, price, _ = lot
                lot_btns.append([InlineKeyboardButton(
                    text="@" + username + " — " + str(price) + " ⭐",
                    callback_data="market_lot_" + str(lot_id)
                )])
            page_rows = market_page_kb(page, total_pages)
            lot_btns.extend(page_rows)
            lot_btns.append([InlineKeyboardButton(text="‹ Назад", callback_data="market_main")])
            kb = InlineKeyboardMarkup(inline_keyboard=lot_btns)
        await market_edit_photo(call, text, kb)

    async def show_my_lots(call):
        uid = call.from_user.id
        lots = await market_get_my_lots(uid)
        if not lots:
            text = (
                "📋 <b>Мои лоты</b>\n\n"
                "У вас нет активных лотов.\n\n"
                "<i>Выставьте юзернейм на продажу через кнопку «Продать».</i>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💸 Продать", callback_data="market_sell")],
                [InlineKeyboardButton(text="‹ Назад", callback_data="market_main")],
            ])
        else:
            text = "📋 <b>Мои активные лоты</b>  <i>(" + str(len(lots)) + "/" + str(MARKET_MAX_LOTS) + ")</i>\n\n"
            btns = []
            for lot in lots:
                lot_id, username, price, created_at = lot
                date_str = created_at[:10] if created_at else ""
                text += "┌ <b>@" + username + "</b> — " + str(price) + " ⭐\n└ <code>" + date_str + "</code>\n\n"
                btns.append([InlineKeyboardButton(
                    text="🗑 Снять @" + username,
                    callback_data="market_del_" + str(lot_id)
                )])
            if len(lots) < MARKET_MAX_LOTS:
                btns.append([InlineKeyboardButton(text="➕ Добавить лот", callback_data="market_sell")])
            btns.append([InlineKeyboardButton(text="‹ Назад", callback_data="market_main")])
            kb = InlineKeyboardMarkup(inline_keyboard=btns)
        await market_edit_photo(call, text, kb)

    @dp.message(F.text == "🏪 Маркет")
    async def market_h(message: Message):
        uid = message.from_user.id
        if await is_blocked(uid):
            return
        count, avg = await market_get_user_rating(uid)
        text = (
            "🏪 <b>Маркет юзов</b>\n\n"
            "⭐ <b>Ваш рейтинг:</b> " + stars_display(avg, count) + "\n\n"
            "<i>Покупайте и продавайте Telegram-юзернеймы напрямую через бота.</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить", callback_data="market_buy_0"),
             InlineKeyboardButton(text="💸 Продать", callback_data="market_sell")],
            [InlineKeyboardButton(text="📋 Мои лоты", callback_data="market_my_lots")],
        ])
        await send_photo(message, text, reply_markup=kb)

    @dp.callback_query(F.data == "market_main")
    async def market_main_cb(call: CallbackQuery):
        uid = call.from_user.id
        await call.answer()
        count, avg = await market_get_user_rating(uid)
        text = (
            "🏪 <b>Маркет юзов</b>\n\n"
            "⭐ <b>Ваш рейтинг:</b> " + stars_display(avg, count) + "\n\n"
            "<i>Покупайте и продавайте Telegram-юзернеймы напрямую через бота.</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить", callback_data="market_buy_0"),
             InlineKeyboardButton(text="💸 Продать", callback_data="market_sell")],
            [InlineKeyboardButton(text="📋 Мои лоты", callback_data="market_my_lots")],
        ])
        await market_edit_photo(call, text, kb)

    @dp.callback_query(F.data == "market_open_new")
    async def market_open_new_cb(call: CallbackQuery):
        uid = call.from_user.id
        await call.answer()
        count, avg = await market_get_user_rating(uid)
        text = (
            "🏪 <b>Маркет юзов</b>\n\n"
            "⭐ <b>Ваш рейтинг:</b> " + stars_display(avg, count) + "\n\n"
            "<i>Покупайте и продавайте Telegram-юзернеймы напрямую через бота.</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить", callback_data="market_buy_0"),
             InlineKeyboardButton(text="💸 Продать", callback_data="market_sell")],
            [InlineKeyboardButton(text="📋 Мои лоты", callback_data="market_my_lots")],
        ])
        await send_photo(call.message, text, reply_markup=kb)

    @dp.callback_query(F.data.startswith("market_buy_"))
    async def market_buy_cb(call: CallbackQuery):
        await call.answer()
        page = int(call.data[11:])
        await show_market_buy(call, page=page)

    @dp.callback_query(F.data.startswith("market_page_"))
    async def market_page_cb(call: CallbackQuery):
        await call.answer()
        page = int(call.data[12:])
        await show_market_buy(call, page=page)

    @dp.callback_query(F.data.startswith("market_lot_"))
    async def market_lot_cb(call: CallbackQuery):
        await call.answer()
        lot_id = int(call.data[11:])
        lot = await market_get_lot(lot_id)
        if not lot:
            await call.answer("❌ Лот не найден или уже продан.", show_alert=True)
            return
        lot_id, seller_id, seller_uname, username, price, created_at = lot
        count, avg = await market_get_user_rating(seller_id)
        date_str = created_at[:10] if created_at else "—"
        time_str = created_at[11:16] if created_at and len(created_at) > 10 else ""
        seller_link = "@" + seller_uname if seller_uname else "ID " + str(seller_id)
        text = (
            "📦 <b>Лот #" + str(lot_id) + "</b>\n"
            "├ 🏷 <b>Username:</b> @" + username + "\n"
            "╰ 💰 <b>Цена:</b> " + str(price) + " ⭐\n\n"
            "👤 <b>Продавец:</b> " + seller_link + "\n"
            "⭐ <b>Рейтинг:</b> " + stars_display(avg, count)
        )
        buy_url = "https://t.me/" + seller_uname if seller_uname else "https://t.me/fresei"
        uid = call.from_user.id
        kb_rows = [
            [InlineKeyboardButton(text="💰 Купить за " + str(price) + " ⭐", url=buy_url)],
            [InlineKeyboardButton(text="⭐ Отзывы (" + str(count) + ")", callback_data="market_reviews_" + str(seller_id) + "_" + str(lot_id))],
        ]
        if uid in ADMIN_IDS:
            kb_rows.append([InlineKeyboardButton(text="🗑 Удалить лот", callback_data="admin_lot_del_" + str(lot_id) + "_" + str(seller_id))])
            kb_rows.append([InlineKeyboardButton(text="🗑 Удалить + заблокировать маркет", callback_data="admin_lot_del_block_" + str(lot_id) + "_" + str(seller_id))])
        kb_rows.append([InlineKeyboardButton(text="‹ Назад к списку", callback_data="market_buy_0")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await market_edit_photo(call, text, kb)

    @dp.callback_query(F.data.startswith("market_reviews_"))
    async def market_reviews_cb(call: CallbackQuery):
        await call.answer()
        parts = call.data[15:].split("_")
        seller_id = int(parts[0])
        lot_id = int(parts[1]) if len(parts) > 1 else 0
        reviews, count, avg = await market_get_reviews(seller_id)
        try:
            seller_info = await bot.get_chat(seller_id)
            seller_uname = seller_info.username or str(seller_id)
        except Exception:
            seller_uname = str(seller_id)
        text = (
            "⭐️ <b>Отзывы @" + seller_uname + "</b>\n"
            "╰ 📊 <b>Рейтинг:</b> " + stars_display(avg, count)
        )
        back_cb = "market_lot_" + str(lot_id) if lot_id else "market_buy_0"
        uid = call.from_user.id
        kb_rows = []
        if uid != seller_id:
            kb_rows.append([InlineKeyboardButton(text="⚡ Написать отзыв", callback_data="market_review_start_" + str(lot_id) + "_" + str(seller_id))])
        kb_rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=back_cb)])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await market_edit_photo(call, text, kb)

    # ── НАПИСАТЬ ОТЗЫВ ───────────────────────────────────────────
    @dp.callback_query(F.data.startswith("market_review_start_"))
    async def market_review_start_cb(call: CallbackQuery, state: FSMContext):
        await call.answer()
        uid = call.from_user.id
        parts = call.data[20:].split("_")
        lot_id = int(parts[0])
        seller_id = int(parts[1])
        # Проверка: 1 отзыв одному продавцу
        async with aiosqlite.connect(DB_PATH) as db:
            already = await (await db.execute(
                "SELECT 1 FROM market_reviews WHERE buyer_id=? AND seller_id=?", (uid, seller_id)
            )).fetchone()
        if already:
            await call.answer("❌ Вы уже оставляли отзыв этому продавцу.", show_alert=True)
            return
        # Проверка: не более 3 отзывов в день
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            day_count = (await (await db.execute(
                "SELECT COUNT(*) FROM market_reviews WHERE buyer_id=? AND created_at>=?", (uid, today_start)
            )).fetchone())[0]
        if day_count >= 3:
            await call.answer("❌ Вы можете оставлять не более 3 отзывов в день.", show_alert=True)
            return
        await state.update_data(review_lot_id=lot_id, review_seller_id=seller_id)
        await state.set_state(S.market_review_stars)
        text = (
            "⭐ <b>Оценка продавца</b>\n\n"
            "Выберите оценку от 1 до 5 звёзд:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="1 ★", callback_data="market_review_stars_1"),
                InlineKeyboardButton(text="2 ★★", callback_data="market_review_stars_2"),
                InlineKeyboardButton(text="3 ★★★", callback_data="market_review_stars_3"),
                InlineKeyboardButton(text="4 ★★★★", callback_data="market_review_stars_4"),
                InlineKeyboardButton(text="5 ★★★★★", callback_data="market_review_stars_5"),
            ],
            [InlineKeyboardButton(text="‹ Отмена", callback_data="market_buy_0")],
        ])
        await market_edit_photo(call, text, kb)

    @dp.callback_query(F.data.startswith("market_review_stars_"))
    async def market_review_stars_cb(call: CallbackQuery, state: FSMContext):
        await call.answer()
        rating = int(call.data[20:])
        data = await state.get_data()
        await state.clear()
        uid = call.from_user.id
        lot_id = data.get("review_lot_id")
        seller_id = data.get("review_seller_id")
        if not lot_id or not seller_id:
            await call.answer("❌ Ошибка. Попробуйте снова.", show_alert=True)
            return
        await market_add_review(lot_id, uid, seller_id, rating, "")
        stars_str = "★" * rating + "☆" * (5 - rating)
        await market_edit_photo(
            call,
            "✅ <b>Отзыв опубликован!</b>\n\n"
            "Оценка: <b>" + stars_str + "</b>\n\n"
            "<i>Спасибо за обратную связь ⚡</i>",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="‹ В маркет", callback_data="market_main")]
            ])
        )

    # ── ПРОДАЖА ──────────────────────────────────────────────────
    @dp.callback_query(F.data == "market_sell")
    async def market_sell_cb(call: CallbackQuery, state: FSMContext):
        await call.answer()
        uid = call.from_user.id
        if await is_market_blocked(uid):
            await call.answer("🚫 Ваш доступ к маркету заблокирован.\nНапишите в поддержку: @hermessupports", show_alert=True)
            return
        lots = await market_get_my_lots(uid)
        if len(lots) >= MARKET_MAX_LOTS:
            await call.answer(
                "❌ У вас уже " + str(MARKET_MAX_LOTS) + " активных лотов — максимум!\n\nСнимите лот чтобы добавить новый.",
                show_alert=True
            )
            return
        await state.set_state(S.market_sell_username)
        text = (
            "💸 <b>Выставить юзернейм на продажу</b>\n\n"
            "<b>⚠️ От 4 до 8 символов — только буквы и цифры.\n"
            "Убедитесь, что ник принадлежит вам, иначе маркет для вас будет заблокирован.</b>\n\n"
            "<b>🔤 Введите юзернейм:</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="‹ Отмена", callback_data="market_main")]
        ])
        await market_edit_photo(call, text, kb)

    @dp.message(S.market_sell_username)
    async def market_sell_username_h(message: Message, state: FSMContext):
        MENU_BUTTONS = {"5 Букв 🔍", "6 Букв 🔍", "🌪️ Маска", "🎲 Фильтр", "🎯 Ловушка", "🏪 Маркет", "👤 Профиль", "🌟 Подписка"}
        if message.text and message.text.strip() in MENU_BUTTONS:
            await state.clear()
            btn = message.text.strip()
            if btn in ("5 Букв 🔍", "6 Букв 🔍"):
                await search_h(message)
            elif btn == "🌪️ Маска":
                await mask_h(message, state)
            elif btn == "🎲 Фильтр":
                await filter_h(message, state)
            elif btn == "🎯 Ловушка":
                await trap_h(message)
            elif btn == "🏪 Маркет":
                await market_h(message)
            elif btn == "👤 Профиль":
                await profile_h(message)
            elif btn == "🌟 Подписка":
                await sub_h(message)
            return
        username = message.text.strip().lstrip("@").lower() if message.text else ""
        if not username.isalnum() or len(username) < 4 or len(username) > 8:
            await message.answer(
                "❌ <b>Неверный формат</b>\n\n"
                "Юзернейм должен быть <b>от 4 до 8 символов</b>, только буквы и цифры",
                parse_mode="HTML"
            )
            return
        await state.update_data(sell_username=username)
        await state.set_state(S.market_sell_price)
        await send_photo(
            message,
            "💰 <b>Введите цену в звёздах (⭐)</b>\n\n"
            "<i>Только цифры, например: </i><code>100</code>",
        )

    @dp.message(S.market_sell_price)
    async def market_sell_price_h(message: Message, state: FSMContext):
        MENU_BUTTONS = {"5 Букв 🔍", "6 Букв 🔍", "🌪️ Маска", "🎲 Фильтр", "🎯 Ловушка", "🏪 Маркет", "👤 Профиль", "🌟 Подписка"}
        if message.text and message.text.strip() in MENU_BUTTONS:
            await state.clear()
            btn = message.text.strip()
            if btn in ("5 Букв 🔍", "6 Букв 🔍"):
                await search_h(message)
            elif btn == "🌪️ Маска":
                await mask_h(message, state)
            elif btn == "🎲 Фильтр":
                await filter_h(message, state)
            elif btn == "🎯 Ловушка":
                await trap_h(message)
            elif btn == "🏪 Маркет":
                await market_h(message)
            elif btn == "👤 Профиль":
                await profile_h(message)
            elif btn == "🌟 Подписка":
                await sub_h(message)
            return
        uid = message.from_user.id
        uname = message.from_user.username or ""
        try:
            price = int(message.text.strip())
            if price <= 0:
                raise ValueError
        except ValueError:
            await send_photo(message, "❌ <b>Введите целое число больше 0.</b>")
            return
        if price > 10000:
            await send_photo(message, "❌ <b>Максимальная цена — 10 000 ⭐</b>")
            return
        data = await state.get_data()
        await state.clear()
        username = data.get("sell_username")
        if not username:
            await message.answer("❌ Ошибка. Попробуйте снова через маркет.", parse_mode="HTML")
            return
        # Проверяем лимит ещё раз
        lots = await market_get_my_lots(uid)
        if len(lots) >= MARKET_MAX_LOTS:
            await send_photo(
                message,
                "❌ <b>Достигнут лимит лотов (" + str(MARKET_MAX_LOTS) + ").</b>\n\nСначала снимите один из активных лотов."
            )
            return
        # Проверяем что юзернейм занят (т.е. принадлежит кому-то)
        check_msg = await message.answer("🔍 <b>Проверяю юзернейм...</b>", parse_mode="HTML")
        async with aiohttp.ClientSession() as session:
            taken = await tme_taken(username, session)
        await check_msg.delete()
        if not taken:
            await send_photo(
                message,
                "❌ <b>Юзернейм @" + username + " не занят.</b>\n\n"
                "Вы не можете продавать свободный юзернейм.\n"
                "<i>Убедитесь, что ник зарегистрирован на вашем аккаунте.</i>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🏪 В маркет", callback_data="market_main")]
                ])
            )
            return
        await market_add_lot(uid, uname, username, price)
        await send_photo(
            message,
            "✅ <b>Лот выставлен!</b>\n\n"
            "🏷 <b>Username:</b> @" + username + "\n"
            "💰 <b>Цена:</b> " + str(price) + " ⭐\n\n"
            "<i>Покупатели смогут найти ваш лот в маркете и написать вам напрямую.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои лоты", callback_data="market_my_lots")],
                [InlineKeyboardButton(text="🏪 В маркет", callback_data="market_main")],
            ])
        )

    # ── МОИ ЛОТЫ ─────────────────────────────────────────────────
    @dp.callback_query(F.data == "market_my_lots")
    async def market_my_lots_cb(call: CallbackQuery):
        await call.answer()
        await show_my_lots(call)

    @dp.callback_query(F.data.startswith("market_del_"))
    async def market_del_cb(call: CallbackQuery):
        uid = call.from_user.id
        lot_id = int(call.data[11:])
        await market_delete_lot(lot_id, seller_id=uid)
        await call.answer("✅ Лот снят с продажи.")
        await show_my_lots(call)

    @dp.callback_query(F.data.startswith("admin_lot_del_") & ~F.data.startswith("admin_lot_del_block_"))
    async def admin_lot_del_cb(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.answer()
        # format: admin_lot_del_{lot_id}_{seller_id}
        parts = call.data[14:].split("_")
        lot_id = int(parts[0])
        seller_id = int(parts[1])
        await market_delete_lot(lot_id)
        try:
            await bot.send_message(
                seller_id,
                "⚠️ <b>Ваш лот был удалён администратором.</b>\n\n"
                "<i>Если вы считаете это ошибкой — напишите в поддержку: @hermessupports 🤍</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await market_edit_photo(
            call,
            "✅ <b>Лот удалён.</b> Продавец уведомлён.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ К списку", callback_data="market_buy_0")]])
        )

    @dp.callback_query(F.data.startswith("admin_lot_del_block_"))
    async def admin_lot_del_block_cb(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.answer()
        # format: admin_lot_del_block_{lot_id}_{seller_id}
        parts = call.data[20:].split("_")
        lot_id = int(parts[0])
        seller_id = int(parts[1])
        await market_delete_lot(lot_id)
        await market_block_user(seller_id)
        try:
            await bot.send_message(
                seller_id,
                "🚫 <b>Ваш лот был удалён, а доступ к маркету заблокирован.</b>\n\n"
                "<i>Если вы считаете это ошибкой — напишите в поддержку: @HermSup_Bot 🤍</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await market_edit_photo(
            call,
            "✅ <b>Лот удалён, маркет заблокирован.</b> Продавец уведомлён.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ К списку", callback_data="market_buy_0")]])
        )


    @dp.message(F.text == "👤 Профиль")
    async def profile_h(message: Message):
        uid = message.from_user.id
        user = await get_user(uid)
        if not user:
            await message.answer("Напиши /start")
            return
        traps = await get_traps(uid)
        sub_exp = await get_sub_expires(uid)
        has_sub = await has_subscription(uid)
        if sub_exp:
            diff = sub_exp - datetime.now()
            sub_txt = str(diff.days) + " дн. " + str(diff.seconds // 3600) + " ч."
        else:
            sub_txt = "Подписка отсутствует"
        usage, _ = await get_daily_usage(uid)
        bonus_s = await get_bonus_searches(uid)
        bonus_f = await get_bonus_filter(uid)
        bonus_m = await get_bonus_mask(uid)
        lim_txt = "∞" if has_sub else (str(FREE_DAILY_LIMIT - usage) + "/" + str(FREE_DAILY_LIMIT))
        text = (
            "👤 <b>Информация</b>\n"
            "├ ID: <code>" + str(uid) + "</code>\n"
            "├ Подписка: " + sub_txt + "\n"
            "╰ Найдено ников: " + str(user[5]) + "\n\n"
            "🎁 <b>Бонусные запросы с рефералов</b>\n"
            "├ Обычный поиск: " + str(bonus_s) + "\n"
            "├ Поиск по фильтру: " + str(bonus_f) + "\n"
            "╰ Маска: " + str(bonus_m) + "\n\n"
            "🎯 <b>Ловушки</b>\n"
            "├ Активных: " + str(len(traps)) + "\n"
            "╰ Сработало: 0\n\n"
            "<i>Ловушки - сообщают вам сразу после освобождения юзернейма.</i>"
        )
        await send_photo(message, text, reply_markup=profile_kb())

    @dp.callback_query(F.data == "ref_menu")
    async def ref_menu_cb(call: CallbackQuery):
        uid = call.from_user.id
        rc = await get_ref_count(uid)
        me = await bot.get_me()
        ref_link = "https://t.me/" + me.username + "?start=ref" + str(uid)
        text = (
            "👥 <b>Реферальная система</b>\n\n"
            "<b>С каждого приглашения вы и ваш реферал будете получать бесплатные 2 запроса на обычный поиск, 1 поиск по фильтру и 1 поиск по маске в боте 🤍</b>\n\n"
            "<b>Реферал засчитывается после подписки на канал!</b>\n\n"
            "ℹ️ <b>Информация</b>\n"
            "╰ Количество рефералов: " + str(rc) + "\n\n"
            "🔗 Ссылка: " + ref_link
        )
        share_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Поделиться ссылкой", switch_inline_query="Присоединяйся! " + ref_link)],
            [InlineKeyboardButton(text="‹ Назад", callback_data="back_profile")],
        ])
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=share_kb)
        except Exception:
            try:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=share_kb)
            except Exception:
                await call.message.answer(text, parse_mode="HTML", reply_markup=share_kb)

    @dp.callback_query(F.data == "back_profile")
    async def back_profile_cb(call: CallbackQuery):
        uid = call.from_user.id
        user = await get_user(uid)
        if not user:
            return
        traps = await get_traps(uid)
        sub_exp = await get_sub_expires(uid)
        has_sub = await has_subscription(uid)
        if sub_exp:
            diff = sub_exp - datetime.now()
            sub_txt = str(diff.days) + " дн. " + str(diff.seconds // 3600) + " ч."
        else:
            sub_txt = "Подписка отсутствует"
        usage, _ = await get_daily_usage(uid)
        bonus = await get_bonus_searches(uid)
        lim_txt = "∞" if has_sub else (str(FREE_DAILY_LIMIT - usage) + "/" + str(FREE_DAILY_LIMIT))
        text = (
            "👤 <b>Информация</b>\n"
            "├ ID: <code>" + str(uid) + "</code>\n"
            "╰ Найдено ников: " + str(user[5]) + "\n\n"
            "🌟 <b>Подписка</b>\n"
            "├ Осталось: " + sub_txt + "\n"
            "├ Осталось запросов на сегодня: " + lim_txt + "\n"
            "╰ Бонусных запросов с рефералов: " + str(bonus) + "\n\n"
            "🎯 <b>Ловушки</b>\n"
            "├ Активных: " + str(len(traps)) + "\n"
            "╰ Сработало: 0\n\n"
            "<i>Ловушки - сообщают вам сразу после освобождения юзернейма.</i>"
        )
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=profile_kb())
        except Exception:
            try:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=profile_kb())
            except Exception:
                await call.message.answer(text, parse_mode="HTML", reply_markup=profile_kb())
        await call.answer()

    @dp.callback_query(F.data == "promo_input")
    async def promo_input_cb(call: CallbackQuery, state: FSMContext):
        await state.set_state(S.promo)
        text = "🎫 <b>Активация промокода</b>\n\nВведи промокод:"
        kb = back_kb("back_profile")
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            try:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                photo = get_photo()
                if photo:
                    await call.message.answer_photo(photo, caption=text, parse_mode="HTML", reply_markup=kb)
                else:
                    await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    @dp.message(S.promo)
    async def promo_h(message: Message, state: FSMContext):
        await state.clear()
        code = message.text.strip().upper()
        uid = message.from_user.id
        async with aiosqlite.connect(DB_PATH) as db:
            p = await (await db.execute("SELECT days,max_uses,used FROM promos WHERE code=?", (code,))).fetchone()
            if not p:
                await message.answer("<b>❌ Промокод не найден.</b>", parse_mode="HTML")
                return
            days, max_uses, used = p
            if used >= max_uses:
                await message.answer("<b>❌ Промокод исчерпан.</b>", parse_mode="HTML")
                return
            al = await (await db.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (uid, code))).fetchone()
            if al:
                await message.answer("<b>❌ Вы уже использовали этот промокод.</b>", parse_mode="HTML")
                return
            await db.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
            await db.execute("INSERT INTO promo_uses (user_id,code) VALUES (?,?)", (uid, code))
            await db.commit()
        if days < 0:
            # Бонусные запросы (days отрицательный = количество запросов)
            bonus_count = abs(days)
            await add_bonus_searches(uid, bonus_count)
            await message.answer("✅ <b>Промокод активирован!</b>\nПолучено: <b>" + str(bonus_count) + " бонусных запросов</b>", parse_mode="HTML")
        else:
            # Подписка (days = минуты)
            await add_subscription(uid, days)
            await message.answer("✅ <b>Промокод активирован!</b>\nПолучено: <b>" + minutes_to_str(days) + " подписки</b>", parse_mode="HTML")

    # ── ПОДПИСКА ──────────────────────────────────────────────
    @dp.message(F.text == "🌟 Подписка")
    async def sub_h(message: Message):
        text = "⭐️ <b>Доступные тарифы подписки.</b>\n\n<b>После покупки подписки можно ставить 10 ловушек на ник, использовать безлимитный поиск для всех функций а так-же иметь возможность крутить рулетку с дополнительными призами.</b>"
        await send_photo(message, text, reply_markup=plans_kb())

    @dp.callback_query(F.data == "sub_menu")
    async def sub_menu_cb(call: CallbackQuery):
        text = "⭐️ <b>Доступные тарифы подписки.</b>\n\n<b>После покупки подписки можно ставить 10 ловушек на ник, использовать безлимитный поиск для всех функций а так-же иметь возможность крутить рулетку с дополнительными призами.</b>"
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=plans_kb())
        except Exception:
            try:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=plans_kb())
            except Exception:
                await call.message.answer(text, parse_mode="HTML", reply_markup=plans_kb())

    @dp.callback_query(F.data.startswith("plan_"))
    async def plan_cb(call: CallbackQuery):
        plan_id = call.data[5:]
        plan = PLANS.get(plan_id)
        if not plan:
            return
        text = "💳 Выберите способ оплаты для подписки на <b>" + plan["label"] + "</b> за <b>" + str(plan["rub"]) + "₽</b>"
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=pay_method_kb(plan_id))
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=pay_method_kb(plan_id))

    @dp.callback_query(F.data.startswith("pay_manual_"))
    async def pay_manual_cb(call: CallbackQuery):
        plan_id = call.data[11:]
        plan = PLANS.get(plan_id)
        if not plan:
            return
        text = (
            "<b>Свяжитесь с @fresei для покупки Тарифа за СБП / РФ Карты / Звёзды ⭐\n\n"
            "Сообщение: Привет, хочу купить " + plan["label"] + " тарифа за СБП / РФ Карты / Звёзды 🤍\n\n"
            "Если же у вас SpamBan, пишите в чат нашего канала ⚡</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Написать 💬", url="https://t.me/fresei")],
            [InlineKeyboardButton(text="‹ Назад", callback_data="plan_" + plan_id)],
        ])
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    @dp.callback_query(F.data.startswith("pay_stars_"))
    async def pay_stars_cb(call: CallbackQuery):
        plan_id = call.data[10:]
        plan = PLANS[plan_id]
        await call.message.answer_invoice(
            title="Подписка " + plan["label"],
            description="Hermes Search - безлимитный поиск на " + plan["label"],
            payload="sub:" + plan_id, currency="XTR",
            prices=[LabeledPrice(label="Подписка " + plan["label"], amount=plan["stars"])]
        )
        await call.answer()

    @dp.pre_checkout_query()
    async def pre_checkout(q):
        await bot.answer_pre_checkout_query(q.id, ok=True)

    @dp.message(F.successful_payment)
    async def success_pay(message: Message):
        payload = message.successful_payment.invoice_payload
        if payload.startswith("sub:"):
            plan_id = payload[4:]
            plan = PLANS.get(plan_id)
            if plan:
                uid = message.from_user.id
                uname = message.from_user.username or ""
                await add_subscription(uid, plan["days"] * 24 * 60)
                await message.answer(
                    "✅ <b>Оплата получена!</b>\nПодписка <b>" + plan["label"] + "</b> активирована!",
                    parse_mode="HTML"
                )
                # Уведомление админам
                uname_txt = ("@" + uname) if uname else str(uid)
                notif = (
                    "💰 <b>Новая покупка!</b>\n\n"
                    "👤 Юзер: " + uname_txt + " (<code>" + str(uid) + "</code>)\n"
                    "📦 Тариф: <b>" + plan["label"] + "</b>\n"
                    "💳 Метод: Telegram Stars (" + str(plan["stars"]) + " ⭐)\n"
                    "💵 Сумма: " + str(plan["rub"]) + "₽ / $" + str(plan["usd"])
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, notif, parse_mode="HTML")
                    except Exception:
                        pass

    @dp.callback_query(F.data.startswith("pay_crypto_"))
    async def pay_crypto_cb(call: CallbackQuery):
        plan_id = call.data[11:]
        plan = PLANS[plan_id]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="USDT", callback_data="crypto_USDT_" + plan_id),
             InlineKeyboardButton(text="TON", callback_data="crypto_TON_" + plan_id)],
            [InlineKeyboardButton(text="‹ Назад", callback_data="plan_" + plan_id)],
        ])
        text = "💳 Выберите валюту для оплаты <b>" + plan["label"] + "</b>"
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @dp.callback_query(F.data.startswith("crypto_"))
    async def crypto_pay_cb(call: CallbackQuery):
        parts = call.data.split("_")
        currency = parts[1]
        plan_id = parts[2]
        plan = PLANS[plan_id]
        if currency == "USDT":
            amount = plan["usd"]
        else:
            rate = await get_ton_rate()
            amount = round(plan["usd"] / rate, 2)
        inv = await create_invoice(amount, currency, call.from_user.id, plan_id)
        if not inv:
            await call.answer("❌ Ошибка создания инвойса", show_alert=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO payments (invoice_id,user_id,plan,method) VALUES (?,?,?,?)",
                (str(inv["invoice_id"]), call.from_user.id, plan_id, "crypto")
            )
            await db.commit()
        pay_url = inv.get("pay_url") or inv.get("bot_invoice_url", "")
        text = "⌛️ <b>Ссылка для оплаты: " + pay_url + "</b>"
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=back_kb("plan_" + plan_id))
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb("plan_" + plan_id))

    # ── АДМИН ─────────────────────────────────────────────────
    @dp.message(Command("admin"))
    async def admin_cmd(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return
        await send_photo(message, "⚙️ <b>Админ-панель</b>", reply_markup=admin_kb())

    @dp.callback_query(F.data == "admin_panel")
    async def admin_panel_cb(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        try:
            await call.message.edit_caption("⚙️ <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_kb())
        except Exception:
            await call.message.edit_text("⚙️ <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_kb())

    @dp.callback_query(F.data == "admin_stats")
    async def admin_stats_cb(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        try:
            await call.message.edit_caption("📊 <b>Статистика - выбери период:</b>", parse_mode="HTML", reply_markup=stats_period_kb())
        except Exception:
            await call.message.answer("📊 <b>Статистика - выбери период:</b>", parse_mode="HTML", reply_markup=stats_period_kb())

    @dp.callback_query(F.data.startswith("stats_"))
    async def stats_cb(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        period = call.data[6:]
        s = await get_stats(period)
        pl = {"day": "День", "week": "Неделя", "all": "Всё время"}.get(period, "")
        conv = round(s["paid_ever"] / s["total_users"] * 100, 1) if s["total_users"] else 0
        top_lines = ""
        for i, (tuid, tuname, tfound) in enumerate(s["top_users"], 1):
            name = ("@" + tuname) if tuname else str(tuid)
            top_lines += "\n" + str(i) + ". " + name + " — " + str(tfound) + " ников"
        text = (
            "📊 <b>Статистика — " + pl + "</b>\n\n"
            "👥 <b>Пользователи</b>\n"
            "├ Всего: " + str(s["total_users"]) + "\n"
            "├ Новых за период: " + str(s["new_users"]) + "\n"
            "├ Заблокированных: " + str(s["blocked"]) + "\n"
            "╰ Платили хоть раз: " + str(s["paid_ever"]) + " (" + str(conv) + "%)\n\n"
            "🔍 <b>Поиск</b>\n"
            "├ Поисков за период: " + str(s["searches"]) + "\n"
            "╰ Найдено ников всего: " + str(s["total_found"]) + "\n\n"
            "⭐ <b>Монетизация</b>\n"
            "├ Активных подписок: " + str(s["active_subs"]) + "\n"
            "╰ Конверсия: " + str(conv) + "%\n\n"
            "🎯 Активных ловушек: " + str(s["active_traps"]) + "\n"
            "💧 Водяных знаков: " + str(s["wm_count"]) + "\n\n"
            "🏆 <b>Топ по поискам:</b>" + top_lines
        )
        try:
            await call.message.edit_caption(text, parse_mode="HTML", reply_markup=stats_period_kb())
        except Exception:
            await call.message.answer(text, parse_mode="HTML", reply_markup=stats_period_kb())

    @dp.callback_query(F.data == "admin_user_info")
    async def admin_user_info_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.set_state(S.user_info_id)
        await call.message.answer("👤 <b>Введи ID или @юзернейм пользователя:</b>", parse_mode="HTML")
        await call.answer()

    @dp.message(S.user_info_id)
    async def user_info_h(message: Message, state: FSMContext):
        if message.from_user.id not in ADMIN_IDS:
            return
        await state.clear()
        inp = message.text.strip().lstrip("@")
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                tid = int(inp)
                user = await (await db.execute("SELECT * FROM users WHERE user_id=?", (tid,))).fetchone()
            except ValueError:
                user = await (await db.execute("SELECT * FROM users WHERE username=?", (inp,))).fetchone()
        if not user:
            await message.answer("❌ <b>Пользователь не найден.</b>", parse_mode="HTML")
            return
        uid = user[0]
        uname = user[1] or ""
        fname = user[2] or ""
        ref_count = user[4]
        total_found = user[5]
        is_bl = user[6]
        created_at = user[7]
        sub_exp = await get_sub_expires(uid)
        has_sub = await has_subscription(uid)
        traps = await get_traps(uid)
        wm = await get_watermark(uid)
        bonus_s = await get_bonus_searches(uid)
        bonus_f = await get_bonus_filter(uid)
        bonus_m = await get_bonus_mask(uid)
        usage_s, _ = await get_daily_usage(uid)
        # Последний поиск
        async with aiosqlite.connect(DB_PATH) as db:
            last_search = await (await db.execute(
                "SELECT created_at FROM search_log WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
            )).fetchone()
            payments_count = (await (await db.execute(
                "SELECT COUNT(*) FROM payments WHERE user_id=? AND status='paid'", (uid,)
            )).fetchone())[0]
        if sub_exp:
            diff = sub_exp - datetime.now()
            sub_txt = str(diff.days) + " дн. " + str(diff.seconds // 3600) + " ч."
        else:
            sub_txt = "❌ Нет"
        last_seen = last_search[0][:16] if last_search else "нет данных"
        reg_date = created_at[:10] if created_at else "?"
        uname_txt = ("@" + uname) if uname else "—"
        wm_txt = wm if wm else "❌ Нет"
        text = (
            "👤 <b>Информация о пользователе</b>\n\n"
            "├ ID: <code>" + str(uid) + "</code>\n"
            "├ Username: " + uname_txt + "\n"
            "├ Имя: " + fname + "\n"
            "├ Зарегистрирован: " + reg_date + "\n"
            "╰ Статус: " + ("🚫 Заблокирован" if is_bl else "✅ Активен") + "\n\n"
            "🔍 <b>Активность</b>\n"
            "├ Найдено ников: " + str(total_found) + "\n"
            "├ Последний поиск: " + last_seen + "\n"
            "├ Поисков сегодня: " + str(usage_s) + "\n"
            "╰ Рефералов: " + str(ref_count) + "\n\n"
            "⭐ <b>Подписка</b>\n"
            "├ Активна: " + sub_txt + "\n"
            "╰ Покупок всего: " + str(payments_count) + "\n\n"
            "🎁 <b>Бонусы</b>\n"
            "├ Поиск: " + str(bonus_s) + "\n"
            "├ Фильтр: " + str(bonus_f) + "\n"
            "╰ Маска: " + str(bonus_m) + "\n\n"
            "🎯 Активных ловушек: " + str(len(traps)) + "\n"
            "💧 Водяной знак: " + wm_txt
        )
        await message.answer(text, parse_mode="HTML")

    @dp.callback_query(F.data == "admin_broadcast")
    async def admin_broadcast_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.set_state(S.broadcast)
        await call.message.answer(
            "📢 <b>Обычная рассылка</b>\n\nОтправь текст или фото с подписью.\n"
            "<i>Сообщение придёт без каких-либо кнопок.</i>",
            parse_mode="HTML"
        )
        await call.answer()

    @dp.message(S.broadcast)
    async def broadcast_h(message: Message, state: FSMContext):
        await state.clear()
        uids = await get_all_uids()
        ok = fail = 0
        for uid in uids:
            try:
                if message.photo:
                    await bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption or "", parse_mode="HTML")
                else:
                    await bot.send_message(uid, message.text, parse_mode="HTML")
                ok += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1
        await message.answer("✅ <b>Рассылка завершена</b>\n\nУспешно: " + str(ok) + "\nОшибок: " + str(fail), parse_mode="HTML")

    @dp.callback_query(F.data == "admin_broadcast_menu")
    async def admin_broadcast_menu_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.set_state(S.broadcast_menu)
        await call.message.answer(
            "🔄 <b>Рассылка с обновлением меню</b>\n\n"
            "Отправь текст или фото с подписью.\n"
            "<i>К сообщению автоматически добавится кнопка «🔄 Обновить меню» — "
            "при нажатии пользователь получит актуальную клавиатуру.</i>",
            parse_mode="HTML"
        )
        await call.answer()

    @dp.message(S.broadcast_menu)
    async def broadcast_menu_h(message: Message, state: FSMContext):
        await state.clear()
        uids = await get_all_uids()
        ok = fail = 0
        update_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить меню", callback_data="force_menu_update")]
        ])
        for uid in uids:
            try:
                if message.photo:
                    await bot.send_photo(
                        uid, message.photo[-1].file_id,
                        caption=message.caption or "", parse_mode="HTML",
                        reply_markup=update_kb
                    )
                else:
                    await bot.send_message(
                        uid, message.text, parse_mode="HTML",
                        reply_markup=update_kb
                    )
                ok += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1
        await message.answer("✅ <b>Рассылка завершена</b>\n\nУспешно: " + str(ok) + "\nОшибок: " + str(fail), parse_mode="HTML")

    @dp.callback_query(F.data == "force_menu_update")
    async def force_menu_update_cb(call: CallbackQuery):
        uid = call.from_user.id
        await call.answer("✅ Меню обновлено!")
        await bot.send_message(
            uid,
            "✅ <b>Меню обновлено!</b>\n\n<i>Используй кнопки ниже для работы с ботом.</i>",
            parse_mode="HTML",
            reply_markup=main_kb()
        )

    @dp.callback_query(F.data == "admin_block")
    async def admin_block_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.set_state(S.block_id)
        await call.message.answer("🚫 <b>Введи ID для блокировки:</b>", parse_mode="HTML")

    @dp.message(S.block_id)
    async def block_h(message: Message, state: FSMContext):
        await state.clear()
        inp = message.text.strip().lstrip("@")
        async with aiosqlite.connect(DB_PATH) as db:
            # Ищем по ID или username
            try:
                tid = int(inp)
                r = await (await db.execute("SELECT user_id, username FROM users WHERE user_id=?", (tid,))).fetchone()
            except ValueError:
                r = await (await db.execute("SELECT user_id, username FROM users WHERE username=?", (inp,))).fetchone()
            if not r:
                await message.answer("❌ <b>Пользователь не найден.</b>", parse_mode="HTML")
                return
            tid, uname = r
            await db.execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (tid,))
            await db.commit()
        uname_txt = ("@" + uname) if uname else str(tid)
        await message.answer("🚫 <b>" + uname_txt + " забанен.</b>", parse_mode="HTML")
        try:
            await bot.send_message(tid, "🚫 <b>Вы заблокированы в боте.</b>", parse_mode="HTML")
        except Exception:
            pass

    @dp.callback_query(F.data == "admin_unblock")
    async def admin_unblock_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.set_state(S.unblock_id)
        await call.message.answer("✅ <b>Введи ID для разблокировки:</b>", parse_mode="HTML")

    @dp.message(S.unblock_id)
    async def unblock_h(message: Message, state: FSMContext):
        await state.clear()
        inp = message.text.strip().lstrip("@")
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                tid = int(inp)
                r = await (await db.execute("SELECT user_id, username, is_blocked FROM users WHERE user_id=?", (tid,))).fetchone()
            except ValueError:
                r = await (await db.execute("SELECT user_id, username, is_blocked FROM users WHERE username=?", (inp,))).fetchone()
            if not r:
                await message.answer("❌ <b>Пользователь не найден.</b>", parse_mode="HTML")
                return
            tid, uname, is_bl = r
            if not is_bl:
                await message.answer("ℹ️ <b>Пользователь не забанен.</b>", parse_mode="HTML")
                return
            await db.execute("UPDATE users SET is_blocked=0 WHERE user_id=?", (tid,))
            await db.commit()
        uname_txt = ("@" + uname) if uname else str(tid)
        await message.answer("✅ <b>" + uname_txt + " разбанен.</b>", parse_mode="HTML")
        try:
            await bot.send_message(tid, "✅ <b>Вы разблокированы в боте.</b>", parse_mode="HTML")
        except Exception:
            pass

    @dp.callback_query(F.data == "admin_promo_create")
    async def admin_promo_create_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.set_state(S.promo_code)
        await call.message.answer("🎫 <b>Введи код промокода:</b>", parse_mode="HTML")

    @dp.message(S.promo_code)
    async def promo_code_h(message: Message, state: FSMContext):
        await state.update_data(code=message.text.strip().upper())
        await message.answer(
            "🎫 <b>Что даёт промокод?</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Подписку", callback_data="promo_type_sub")],
                [InlineKeyboardButton(text="🎁 Бонусные запросы", callback_data="promo_type_bonus")],
            ])
        )

    @dp.callback_query(F.data == "promo_type_sub")
    async def promo_type_sub_cb(call: CallbackQuery, state: FSMContext):
        await state.update_data(promo_type="sub")
        await state.set_state(S.promo_days)
        await call.message.answer("🎫 <b>На сколько времени?</b>\n\nФормат: <code>1д</code>, <code>12ч</code>, <code>30м</code>", parse_mode="HTML")
        await call.answer()

    @dp.callback_query(F.data == "promo_type_bonus")
    async def promo_type_bonus_cb(call: CallbackQuery, state: FSMContext):
        await state.update_data(promo_type="bonus")
        await state.set_state(S.promo_days)
        await call.message.answer("🎁 <b>Сколько бонусных запросов даёт промокод?</b>", parse_mode="HTML")
        await call.answer()

    @dp.message(S.promo_days)
    async def promo_days_h(message: Message, state: FSMContext):
        data = await state.get_data()
        promo_type = data.get("promo_type", "sub")
        if promo_type == "bonus":
            try:
                count = int(message.text.strip())
                if count <= 0:
                    raise ValueError
                await state.update_data(minutes=count)  # храним в minutes как количество
                await state.set_state(S.promo_uses)
                await message.answer("🎫 <b>Сколько активаций?</b>", parse_mode="HTML")
            except ValueError:
                await message.answer("<b>❌ Введи число больше 0.</b>", parse_mode="HTML")
        else:
            minutes = parse_duration(message.text.strip())
            if minutes is None or minutes <= 0:
                await message.answer("<b>❌ Неверный формат. Используй: 1д, 12ч, 30м</b>", parse_mode="HTML")
                return
            await state.update_data(minutes=minutes)
            await state.set_state(S.promo_uses)
            await message.answer("🎫 <b>Сколько активаций?</b>", parse_mode="HTML")

    @dp.message(S.promo_uses)
    async def promo_uses_h(message: Message, state: FSMContext):
        try:
            uses = int(message.text.strip())
            if uses <= 0:
                raise ValueError
            data = await state.get_data()
            await state.clear()
            minutes = data["minutes"]
            promo_type = data.get("promo_type", "sub")
            # days: положительное = минуты подписки, отрицательное = бонусные запросы
            days_val = minutes if promo_type == "sub" else -minutes
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO promos (code,days,max_uses) VALUES (?,?,?)",
                    (data["code"], days_val, uses)
                )
                await db.commit()
            if promo_type == "sub":
                info_txt = "Время: " + minutes_to_str(minutes)
            else:
                info_txt = "Запросов: " + str(minutes)
            await message.answer(
                "✅ <b>Промокод создан!</b>\n\nКод: <code>" + data["code"] + "</code>\n" + info_txt + "\nАктиваций: " + str(uses),
                parse_mode="HTML"
            )
        except ValueError:
            await message.answer("<b>❌ Введи число.</b>", parse_mode="HTML")

    @dp.callback_query(F.data == "admin_promo_list")
    async def admin_promo_list_cb(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            promos = await (await db.execute("SELECT code,days,max_uses,used FROM promos")).fetchall()
        if not promos:
            await call.answer("Нет промокодов", show_alert=True)
            return
        btns = [
            [InlineKeyboardButton(text=c + " (" + str(u) + "/" + str(m) + ") - " + (minutes_to_str(d) if d > 0 else str(-d) + " запросов"), callback_data="promo_info_" + c)]
            for c, d, m, u in promos
        ]
        btns.append([InlineKeyboardButton(text="‹ Назад", callback_data="admin_panel")])
        await call.message.answer("🗑 <b>Промокоды:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

    @dp.callback_query(F.data.startswith("promo_info_"))
    async def promo_info_cb(call: CallbackQuery):
        code = call.data[11:]
        async with aiosqlite.connect(DB_PATH) as db:
            p = await (await db.execute("SELECT code,days,max_uses,used,created_at FROM promos WHERE code=?", (code,))).fetchone()
        if not p:
            await call.answer("Не найден")
            return
        text = "🎫 <b>" + p[0] + "</b>\n" + ("Время: " + minutes_to_str(p[1]) if p[1] > 0 else "Запросов: " + str(-p[1])) + "\nАктиваций: " + str(p[3]) + "/" + str(p[2]) + "\nСоздан: " + p[4]
        await call.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="promo_del_" + code)],
            [InlineKeyboardButton(text="‹ Назад", callback_data="admin_promo_list")],
        ]))

    @dp.callback_query(F.data.startswith("promo_del_"))
    async def promo_del_cb(call: CallbackQuery):
        code = call.data[10:]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM promos WHERE code=?", (code,))
            await db.commit()
        await call.answer("✅ Удалён")
        await call.message.edit_text("🗑 <b>Промокод " + code + " удалён.</b>", parse_mode="HTML")

    @dp.callback_query(F.data == "admin_give_sub")
    async def admin_give_sub_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.update_data(give_type="sub")
        await state.set_state(S.give_sub_id)
        await call.message.answer("⭐ <b>Введи ID или @username:</b>", parse_mode="HTML")
        await call.answer()

    @dp.callback_query(F.data == "admin_give_bonus")
    async def admin_give_bonus_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.update_data(give_type="bonus")
        await state.set_state(S.give_sub_id)
        await call.message.answer("🎁 <b>Введи ID или @username:</b>", parse_mode="HTML")
        await call.answer()

    @dp.message(S.give_bonus_count)
    async def give_bonus_count_h(message: Message, state: FSMContext):
        try:
            count = int(message.text.strip())
            if count <= 0:
                raise ValueError
            data = await state.get_data()
            await state.clear()
            tid = data["tid"]
            uname = data["uname"]
            await add_bonus_searches(tid, count)
            uname_txt = ("@" + uname) if uname else str(tid)
            await message.answer("✅ <b>" + str(count) + " запросов выдано " + uname_txt + ".</b>", parse_mode="HTML")
            try:
                await bot.send_message(tid, "🎁 <b>Вам выдано " + str(count) + " бонусных запросов!</b>", parse_mode="HTML")
            except Exception:
                pass
        except ValueError:
            await message.answer("<b>❌ Введи число больше 0.</b>", parse_mode="HTML")

    @dp.message(S.give_sub_id)
    async def give_sub_id_h(message: Message, state: FSMContext):
        inp = message.text.strip().lstrip("@")
        data = await state.get_data()
        give_type = data.get("give_type", "sub")
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                tid = int(inp)
                r = await (await db.execute("SELECT user_id, username FROM users WHERE user_id=?", (tid,))).fetchone()
            except ValueError:
                r = await (await db.execute("SELECT user_id, username FROM users WHERE username=?", (inp,))).fetchone()
        if not r:
            await message.answer("❌ <b>Пользователь не найден.</b>", parse_mode="HTML")
            return
        await state.update_data(tid=r[0], uname=r[1] or "")
        await state.set_state(S.give_sub_days)
        if give_type == "bonus":
            await message.answer("🎁 <b>Сколько бонусных запросов выдать?</b>", parse_mode="HTML")
        else:
            await message.answer("⭐ <b>На сколько времени?</b>\n\nФормат: <code>1д</code>, <code>12ч</code>, <code>30м</code>", parse_mode="HTML")

    @dp.message(S.give_sub_days)
    async def give_sub_days_h(message: Message, state: FSMContext):
        data = await state.get_data()
        give_type = data.get("give_type", "sub")
        tid = data["tid"]
        uname = data.get("uname", "")
        uname_txt = ("@" + uname) if uname else str(tid)
        await state.clear()
        if give_type == "bonus":
            try:
                count = int(message.text.strip())
                if count <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("<b>❌ Введи число больше 0.</b>", parse_mode="HTML")
                return
            await add_bonus_searches(tid, count)
            await message.answer("✅ <b>" + str(count) + " запросов выдано " + uname_txt + ".</b>", parse_mode="HTML")
            try:
                await bot.send_message(tid, "🎁 <b>Вам выдано " + str(count) + " бонусных запросов!</b>", parse_mode="HTML")
            except Exception:
                pass
        else:
            minutes = parse_duration(message.text.strip())
            if minutes is None or minutes <= 0:
                await message.answer("<b>❌ Неверный формат. Используй: 1д, 12ч, 30м</b>", parse_mode="HTML")
                return
            await add_subscription(tid, minutes)
            dur_str = minutes_to_str(minutes)
            await message.answer("✅ <b>Подписка на " + dur_str + " выдана " + uname_txt + ".</b>", parse_mode="HTML")
            try:
                await bot.send_message(tid, "🎁 <b>Вам выдана подписка на " + dur_str + "!</b>", parse_mode="HTML")
            except Exception:
                pass

    # ── ОТЗЫВ ПОДПИСОК ────────────────────────────────────────
    @dp.callback_query(F.data == "admin_give_spins")
    async def admin_give_spins_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await state.set_state(S.give_spins_id)
        await call.message.answer("🎰 <b>Введи ID пользователя:</b>", parse_mode="HTML")
        await call.answer()

    @dp.message(S.give_spins_id)
    async def give_spins_id_h(message: Message, state: FSMContext):
        try:
            await state.update_data(tid=int(message.text.strip()))
            await state.set_state(S.give_spins_count)
            await message.answer("🎰 <b>Сколько круток выдать?</b>", parse_mode="HTML")
        except ValueError:
            await message.answer("<b>❌ Неверный ID.</b>", parse_mode="HTML")

    @dp.message(S.give_spins_count)
    async def give_spins_count_h(message: Message, state: FSMContext):
        try:
            count = int(message.text.strip())
            if count <= 0:
                raise ValueError
            data = await state.get_data()
            await state.clear()
            tid = data["tid"]
            await add_extra_spins(tid, count)
            await message.answer("✅ <b>Выдано " + str(count) + " круток пользователю " + str(tid) + ".</b>", parse_mode="HTML")
            try:
                await bot.send_message(
                    tid,
                    "🎰 <b>Вам выдали " + str(count) + " " + ("крутку" if count == 1 else "крутки" if count < 5 else "круток") + " рулетки!</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        except ValueError:
            await message.answer("<b>❌ Введи число больше 0.</b>", parse_mode="HTML")

    # ── ОТЗЫВ ПОДПИСОК (оригинал) ─────────────────────────────
    @dp.callback_query(F.data == "admin_del_review")
    async def admin_del_review_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.answer()
        await state.set_state(S.admin_del_review_username)
        await call.message.answer(
            "🗑 <b>Удаление отзыва</b>\n\nВведи юзернейм продавца (без @):",
            parse_mode="HTML"
        )

    @dp.message(S.admin_del_review_username)
    async def admin_del_review_username_h(message: Message, state: FSMContext):
        if message.from_user.id not in ADMIN_IDS:
            return
        await state.clear()
        uname = message.text.strip().lstrip("@")
        try:
            seller_info = await bot.get_chat("@" + uname)
            seller_id = seller_info.id
        except Exception:
            await message.answer("❌ <b>Пользователь не найден.</b>", parse_mode="HTML")
            return
        await show_admin_reviews(message, seller_id, uname, page=0)

    async def show_admin_reviews(msg_or_call, seller_id, seller_uname, page=0):
        per_page = 5
        rows, total = await market_get_reviews_admin(seller_id, page, per_page)
        if not rows:
            text = "📭 <b>У @" + seller_uname + " нет отзывов.</b>"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ Назад", callback_data="admin_panel")]])
        else:
            text = "🗑 <b>Отзывы @" + seller_uname + "</b>  (всего: " + str(total) + ")\n\nВыберите отзыв для удаления:"
            btns = []
            for rev_id, rating, created_at in rows:
                stars = "★" * rating + "☆" * (5 - rating)
                date = created_at[:16].replace("T", " ") if created_at else "—"
                btns.append([InlineKeyboardButton(
                    text=stars + " — " + date,
                    callback_data="admin_rev_del_" + str(rev_id) + "_" + str(seller_id) + "_" + seller_uname + "_" + str(page)
                )])
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="‹ Назад", callback_data="admin_rev_page_" + str(seller_id) + "_" + seller_uname + "_" + str(page - 1)))
            if (page + 1) * per_page < total:
                nav.append(InlineKeyboardButton(text="Вперёд ›", callback_data="admin_rev_page_" + str(seller_id) + "_" + seller_uname + "_" + str(page + 1)))
            if nav:
                btns.append(nav)
            btns.append([InlineKeyboardButton(text="‹ В админку", callback_data="admin_panel")])
            kb = InlineKeyboardMarkup(inline_keyboard=btns)
        if isinstance(msg_or_call, CallbackQuery):
            try:
                await msg_or_call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                await msg_or_call.message.answer(text, parse_mode="HTML", reply_markup=kb)
        else:
            await msg_or_call.answer(text, parse_mode="HTML", reply_markup=kb)

    @dp.callback_query(F.data.startswith("admin_rev_page_"))
    async def admin_rev_page_cb(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.answer()
        parts = call.data[15:].split("_")
        seller_id = int(parts[0])
        seller_uname = parts[1]
        page = int(parts[2])
        await show_admin_reviews(call, seller_id, seller_uname, page)

    @dp.callback_query(F.data.startswith("admin_rev_del_"))
    async def admin_rev_del_cb(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.answer()
        # format: admin_rev_del_{rev_id}_{seller_id}_{seller_uname}_{page}
        data = call.data[14:]
        parts = data.split("_")
        rev_id = int(parts[0])
        seller_id = int(parts[1])
        seller_uname = parts[2]
        page = int(parts[3])
        await market_delete_review(rev_id)
        await show_admin_reviews(call, seller_id, seller_uname, page)

    @dp.callback_query(F.data == "admin_revoke_all_subs_1")
    async def admin_revoke_1(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.message.answer(
            "⚠️ <b>Ты собираешься УДАЛИТЬ ВСЕ подписки у всех пользователей.</b>\n\nТы уверен?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, продолжить", callback_data="admin_revoke_all_subs_2")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
            ])
        )
        await call.answer()

    @dp.callback_query(F.data == "admin_revoke_all_subs_2")
    async def admin_revoke_2(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.message.answer(
            "🚨 <b>ВТОРОЕ ПОДТВЕРЖДЕНИЕ</b>\n\nЭто действие необратимо. Все подписки будут удалены. Продолжить?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, я понимаю", callback_data="admin_revoke_all_subs_3")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
            ])
        )
        await call.answer()

    @dp.callback_query(F.data == "admin_revoke_all_subs_3")
    async def admin_revoke_3(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.message.answer(
            "☠️ <b>ФИНАЛЬНОЕ ПОДТВЕРЖДЕНИЕ</b>\n\nНажми кнопку ниже чтобы удалить ВСЕ подписки прямо сейчас.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="☠️ УДАЛИТЬ ВСЕ ПОДПИСКИ", callback_data="admin_revoke_all_subs_do")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
            ])
        )
        await call.answer()

    @dp.callback_query(F.data == "admin_revoke_all_subs_do")
    async def admin_revoke_do(call: CallbackQuery):
        if call.from_user.id not in ADMIN_IDS:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            count = (await (await db.execute("SELECT COUNT(*) FROM subscriptions")).fetchone())[0]
            await db.execute("DELETE FROM subscriptions")
            await db.commit()
        await call.message.answer(
            "✅ <b>Готово. Удалено подписок: " + str(count) + "</b>",
            parse_mode="HTML"
        )
        await call.answer()

    # ── ФРИШНЫЙ РЕЖИМ ─────────────────────────────────────────
    @dp.callback_query(F.data == "admin_freetime")
    async def admin_freetime_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        global freetime_until, freetime_types, freetime_cooldown
        if freetime_until > time.time():
            freetime_until = 0
            freetime_types = set()
            freetime_cooldown = None
            await call.answer("✅ Фришный режим отменён", show_alert=True)
            try:
                await call.message.edit_reply_markup(reply_markup=admin_kb())
            except Exception:
                pass
            return
        await state.set_state(S.freetime_duration)
        await call.message.answer(
            "🎉 <b>Фришный режим</b>\n\nНа сколько времени?\n\nФормат: <code>30м</code>, <code>1ч</code>, <code>2ч</code>",
            parse_mode="HTML"
        )
        await call.answer()

    @dp.message(S.freetime_duration)
    async def freetime_duration_h(message: Message, state: FSMContext):
        minutes = parse_duration(message.text.strip())
        if not minutes or minutes <= 0:
            await message.answer("<b>❌ Неверный формат. Используй: 30м, 1ч, 2ч</b>", parse_mode="HTML")
            return
        await state.update_data(ft_minutes=minutes)
        await state.set_state(S.freetime_types_pick)
        await message.answer(
            "🎉 <b>Что делаем фришным?</b>\n\nМожно выбрать несколько — нажимай кнопки, потом жми Готово.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Поиск (5-6 букв)", callback_data="ft_toggle_search")],
                [InlineKeyboardButton(text="🎲 Фильтры", callback_data="ft_toggle_filter")],
                [InlineKeyboardButton(text="🌪️ Маски", callback_data="ft_toggle_mask")],
                [InlineKeyboardButton(text="✅ Готово", callback_data="ft_confirm")],
            ])
        )

    @dp.callback_query(F.data.startswith("ft_toggle_"))
    async def ft_toggle_cb(call: CallbackQuery, state: FSMContext):
        key = call.data[10:]  # search / filter / mask
        data = await state.get_data()
        selected = set(data.get("ft_selected", []))
        if key in selected:
            selected.discard(key)
        else:
            selected.add(key)
        await state.update_data(ft_selected=list(selected))
        labels = {"search": "🔍 Поиск", "filter": "🎲 Фильтры", "mask": "🌪️ Маски"}
        def btn(k):
            tick = "✅ " if k in selected else ""
            return InlineKeyboardButton(text=tick + labels[k], callback_data="ft_toggle_" + k)
        await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("search")], [btn("filter")], [btn("mask")],
            [InlineKeyboardButton(text="✅ Готово", callback_data="ft_confirm")],
        ]))
        await call.answer()

    @dp.callback_query(F.data == "ft_confirm")
    async def ft_confirm_cb(call: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        selected = set(data.get("ft_selected", []))
        if not selected:
            await call.answer("❌ Выбери хотя бы одно!", show_alert=True)
            return
        await state.update_data(ft_selected=list(selected))
        await state.set_state(S.freetime_cooldown_set)
        await call.message.answer(
            "⏱ <b>КД поиска на время фришного режима?</b>\n\nВведи секунды или 0 чтобы не менять\n(сейчас " + str(SEARCH_COOLDOWN) + " сек)",
            parse_mode="HTML"
        )
        await call.answer()

    @dp.message(S.freetime_cooldown_set)
    async def freetime_cooldown_h(message: Message, state: FSMContext):
        global freetime_until, freetime_types, freetime_cooldown
        try:
            cd = int(message.text.strip())
            if cd < 0:
                raise ValueError
        except ValueError:
            await message.answer("<b>❌ Введи число секунд (0 = не менять)</b>", parse_mode="HTML")
            return
        data = await state.get_data()
        await state.clear()
        if data.get("just_cd"):
            # Просто меняем глобальный КД
            global SEARCH_COOLDOWN
            if cd > 0:
                SEARCH_COOLDOWN = cd
                await message.answer("✅ <b>КД изменён на " + str(cd) + " сек</b>", parse_mode="HTML")
            else:
                await message.answer("ℹ️ КД не изменён", parse_mode="HTML")
            return
        minutes = data["ft_minutes"]
        selected = set(data.get("ft_selected", []))
        freetime_until = time.time() + minutes * 60
        freetime_types = selected
        freetime_cooldown = cd if cd > 0 else None
        labels = {"search": "Поиск", "filter": "Фильтры", "mask": "Маски"}
        types_str = ", ".join(labels[k] for k in selected if k in labels)
        cd_str = str(cd) + " сек" if cd > 0 else "без изменений"
        await message.answer(
            "✅ <b>Фришный режим активирован!</b>\n\n"
            "⏱ Длительность: " + minutes_to_str(minutes) + "\n"
            "🎯 Фришно: " + types_str + "\n"
            "⚡ КД: " + cd_str,
            parse_mode="HTML"
        )

    # ── ВОДЯНОЙ ЗНАК ──────────────────────────────────────────
    @dp.callback_query(F.data == "admin_watermark")
    async def admin_watermark_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.answer()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить водяной знак", callback_data="admin_wm_add")],
            [InlineKeyboardButton(text="🗑 Убрать водяной знак", callback_data="admin_wm_remove")],
            [InlineKeyboardButton(text="‹ Назад", callback_data="admin_panel")],
        ])
        await call.message.answer(
            "💧 <b>Водяной знак</b>\n\n"
            "Водяной знак добавляется в конец каждого сообщения с результатом поиска у выбранного пользователя.\n\n"
            "Например:\n"
            "<b>Ник найден! ✅\n\n"
            "Ник - @utmuz › utmuz\n"
            "├ Ликвидность - 8 из 10 ⭐\n"
            "╰ Свободен ⚡\n\n"
            "🤍 Поиск свободных юзов: @HeSearch_Bot</b>",
            parse_mode="HTML",
            reply_markup=kb
        )

    @dp.callback_query(F.data == "admin_wm_add")
    async def admin_wm_add_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.answer()
        await state.set_state(S.watermark_add_id)
        await call.message.answer(
            "💧 <b>Добавить водяной знак</b>\n\n"
            "Введи ID пользователя и текст водяного знака через пробел:\n\n"
            "<code>123456789 🤍 Поиск свободных юзов: @HeSearch_Bot</code>",
            parse_mode="HTML"
        )

    @dp.message(S.watermark_add_id)
    async def wm_add_id_h(message: Message, state: FSMContext):
        if message.from_user.id not in ADMIN_IDS:
            return
        await state.clear()
        parts = message.text.strip().split(None, 1)
        if len(parts) < 2:
            await message.answer("❌ <b>Формат: ID текст_водяного_знака</b>", parse_mode="HTML")
            return
        try:
            target_uid = int(parts[0])
        except ValueError:
            await message.answer("❌ <b>Неверный ID пользователя</b>", parse_mode="HTML")
            return
        wm_text = parts[1].strip()
        await set_watermark(target_uid, wm_text)
        await message.answer(
            "✅ <b>Водяной знак установлен!</b>\n\n"
            "Пользователь: <code>" + str(target_uid) + "</code>\n"
            "Текст: " + wm_text,
            parse_mode="HTML"
        )

    @dp.callback_query(F.data == "admin_wm_remove")
    async def admin_wm_remove_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.answer()
        await state.set_state(S.watermark_remove_id)
        await call.message.answer(
            "🗑 <b>Убрать водяной знак</b>\n\n"
            "Введи ID пользователя:",
            parse_mode="HTML"
        )

    @dp.message(S.watermark_remove_id)
    async def wm_remove_id_h(message: Message, state: FSMContext):
        if message.from_user.id not in ADMIN_IDS:
            return
        await state.clear()
        try:
            target_uid = int(message.text.strip())
        except ValueError:
            await message.answer("❌ <b>Неверный ID пользователя</b>", parse_mode="HTML")
            return
        wm = await get_watermark(target_uid)
        if not wm:
            await message.answer("⚠️ <b>У этого пользователя нет водяного знака</b>", parse_mode="HTML")
            return
        await remove_watermark(target_uid)
        await message.answer(
            "✅ <b>Водяной знак удалён у пользователя</b> <code>" + str(target_uid) + "</code>",
            parse_mode="HTML"
        )

    # ── ЗАБЛОКИРОВАННЫЕ БУКВЫ ────────────────────────────────
    async def blocked_letters_menu(uid, message_obj):
        has_sub = await has_subscription(uid)
        letters = await get_blocked_letters(uid)
        max_letters = 12 if has_sub else 2
        if has_sub:
            count_txt = str(len(letters)) + " из 12 🌟"
        else:
            count_txt = str(len(letters)) + " из 2 (С подпиской: 12 🌟)"
        text = (
            "<b>🚫 Заблокированные буквы\n\n"
            "Здесь вы можете заблокировать любые буквы, и они перестанут попадаться во время поиска, фильтра и маски 🤍\n\n"
            "Кол-во букв: " + count_txt + "\n\n"
            "Напишите букву, которую хотите внести в чёрный список ⚡</b>"
        )
        btns = []
        btns.append([InlineKeyboardButton(text="🚫 Мои заблокированные буквы", callback_data="settings_bl_list")])
        btns.append([InlineKeyboardButton(text="‹ Назад", callback_data="settings_menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=btns)
        try:
            await message_obj.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            try:
                await message_obj.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                await message_obj.answer(text, parse_mode="HTML", reply_markup=kb)

    @dp.callback_query(F.data == "settings_blocked_letters")
    async def settings_blocked_letters_cb(call: CallbackQuery, state: FSMContext):
        uid = call.from_user.id
        has_sub = await has_subscription(uid)
        letters = await get_blocked_letters(uid)
        max_letters = 12 if has_sub else 2
        if len(letters) >= max_letters:
            if not has_sub:
                await call.answer("🔒 Бесплатно можно заблокировать только 2 буквы. Купи подписку для 12!", show_alert=True)
            else:
                await call.answer("❌ Максимум 12 заблокированных букв!", show_alert=True)
            await blocked_letters_menu(uid, call.message)
            return
        if state:
            await state.set_state(S.blocked_letter_input)
        await call.answer()
        await blocked_letters_menu(uid, call.message)

    @dp.message(S.blocked_letter_input)
    async def blocked_letter_input_h(message: Message, state: FSMContext):
        uid = message.from_user.id
        has_sub = await has_subscription(uid)
        letter = message.text.strip().lower()
        if len(letter) != 1 or not letter.isalpha() or not letter.isascii():
            await message.answer("<b>❌ Введи одну английскую букву</b>", parse_mode="HTML")
            return
        letters = await get_blocked_letters(uid)
        max_letters = 12 if has_sub else 2
        if letter in letters:
            await message.answer("<b>⚠️ Буква " + letter.upper() + " уже заблокирована</b>", parse_mode="HTML")
            return
        if len(letters) >= max_letters:
            await state.clear()
            await message.answer(
                "<b>❌ Достигнут лимит букв (" + str(max_letters) + ")</b>" +
                ("" if has_sub else "\n\n🔒 С подпиской можно заблокировать до 12 букв"),
                parse_mode="HTML"
            )
            return
        await add_blocked_letter(uid, letter)
        await message.answer("<b>✅ Буква " + letter.upper() + " заблокирована</b>", parse_mode="HTML")
        # Оставляем FSM активным — можно сразу писать следующую букву

    @dp.callback_query(F.data == "settings_bl_list")
    async def settings_bl_list_cb(call: CallbackQuery):
        uid = call.from_user.id
        letters = await get_blocked_letters(uid)
        await call.answer()
        if not letters:
            text = "<b>🚫 Ваши заблокированные буквы\n\nУ вас нет заблокированных букв</b>"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="‹ Назад", callback_data="settings_blocked_letters")]
            ])
            try:
                await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        sorted_letters = sorted(letters)
        rows = []
        for i in range(0, len(sorted_letters), 3):
            row = [
                InlineKeyboardButton(text=l.upper(), callback_data="settings_bl_remove_" + l)
                for l in sorted_letters[i:i+3]
            ]
            rows.append(row)
        rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="settings_blocked_letters")])
        text = "<b>🚫 Ваши заблокированные буквы\n\n🤍 Нажмите на букву чтобы вытащить её с чёрного списка:</b>"
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.callback_query(F.data.startswith("settings_bl_remove_"))
    async def settings_bl_remove_cb(call: CallbackQuery):
        uid = call.from_user.id
        letter = call.data[19:]
        await remove_blocked_letter(uid, letter)
        await call.answer("✅ Буква " + letter.upper() + " разблокирована")
        letters = await get_blocked_letters(uid)
        if not letters:
            await blocked_letters_menu(uid, call.message)
            return
        sorted_letters = sorted(letters)
        rows = []
        for i in range(0, len(sorted_letters), 3):
            row = [
                InlineKeyboardButton(text=l.upper(), callback_data="settings_bl_remove_" + l)
                for l in sorted_letters[i:i+3]
            ]
            rows.append(row)
        rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="settings_blocked_letters")])
        text = (
            "🚫 <b>Ваши заблокированные буквы</b>\n\n"
            "🤍 Нажмите на букву чтобы вытащить её с чёрного списка:"
        )
        try:
            await call.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    # ── КД ПОИСКА ─────────────────────────────────────────────
    @dp.callback_query(F.data == "admin_set_cooldown")
    async def admin_set_cooldown_cb(call: CallbackQuery, state: FSMContext):
        if call.from_user.id not in ADMIN_IDS:
            return
        await call.message.answer(
            "⏱ <b>Новый КД поиска (сек)?</b>\n\nСейчас: " + str(SEARCH_COOLDOWN) + " сек",
            parse_mode="HTML"
        )
        await state.set_state(S.freetime_cooldown_set)
        # Временно помечаем что это просто смена КД
        await state.update_data(ft_minutes=0, ft_selected=[], just_cd=True)
        await call.answer()

    # ── ФОНОВЫЕ ЗАДАЧИ ────────────────────────────────────────
    async def trap_checker():
        semaphore = asyncio.Semaphore(20)  # максимум 20 параллельных запросов

        async def check_trap(session, tid, uid, uname):
            async with semaphore:
                try:
                    taken = await tme_taken(uname, session)
                    if not taken:
                        on_frag = await frag_on_auction(uname, session)
                        if not on_frag:
                            await trigger_trap(tid)
                            text = (
                                "🎉 <b>Ловушка сработала!</b>\n\n"
                                "<b>Ник -</b> @" + uname + "\n"
                                "<b>╰ Свободен ⚡</b>\n\n"
                                "🔗 <b>Ссылка - https://t.me/" + uname + "</b>\n\n"
                                "⚡ <b>Действуй быстро!</b>"
                            )
                            await bot_send_photo(bot, uid, text)
                except Exception as e:
                    logger.error("Trap: " + str(e))

        while True:
            await asyncio.sleep(30)
            traps = await get_all_traps()
            if not traps:
                continue
            async with aiohttp.ClientSession() as session:
                tasks = [check_trap(session, tid, uid, uname) for tid, uid, uname in traps]
                await asyncio.gather(*tasks)

    async def trap_limit_checker():
        """Каждые 20 сек проверяет у кого истекла подписка и удаляет лишние ловушки."""
        while True:
            await asyncio.sleep(20)
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    # Все юзеры у кого есть активные ловушки
                    rows = await (await db.execute(
                        "SELECT DISTINCT user_id FROM traps WHERE active=1"
                    )).fetchall()
                for (uid,) in rows:
                    has_sub = await has_subscription(uid)
                    max_traps = 10 if has_sub else 2
                    traps = await get_traps(uid)
                    if len(traps) > max_traps:
                        # Удаляем лишние (самые старые)
                        to_delete = traps[max_traps:]
                        async with aiosqlite.connect(DB_PATH) as db:
                            for tid, uname in to_delete:
                                await db.execute("UPDATE traps SET active=0 WHERE id=?", (tid,))
                            await db.commit()
                        try:
                            deleted_names = ", ".join("@" + u for _, u in to_delete)
                            await bot.send_message(
                                uid,
                                "⚠️ <b>Подписка истекла.</b> Лишние ловушки удалены: " + deleted_names,
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.error("Trap limit checker: " + str(e))

    async def on_startup():
        asyncio.create_task(trap_checker())
        asyncio.create_task(check_payments(bot))
        asyncio.create_task(trap_limit_checker())

    dp.startup.register(on_startup)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
