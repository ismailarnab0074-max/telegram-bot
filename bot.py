import os
import sqlite3
import hashlib
import logging
import aiohttp
import asyncio
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN"8666514015:AAGxGAB5nuYEG4ckBCAgX3TcHkuZE2gTfZw "")
ADMIN_IDS_RAW = os.environ.get("ADMIN_TELEGRAM_IDS"792556370, "")
ADMIN_IDS = set(int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit())
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")

# ─── Conversation States ─────────────────────────────────────────────────────
(
    LOGIN_USERNAME, LOGIN_PIN,
    MAIN_MENU,
    NUM_LIST, NUM_DETAIL,
    TRANSFER_USER, TRANSFER_NUM, TRANSFER_CONFIRM,
    CHANGE_PIN_OLD, CHANGE_PIN_NEW, CHANGE_PIN_CONFIRM,
    # Admin states
    ADM_MENU,
    ADM_CREATE_USER, ADM_CREATE_USER_PIN,
    ADM_DEL_USER,
    ADM_ADD_NUM_USER, ADM_ADD_NUM_PHONE, ADM_ADD_NUM_API,
    ADM_BULK_PASTE,
    ADM_DEL_NUM,
    ADM_XFER_FROM, ADM_XFER_NUM, ADM_XFER_TO,
    ADM_LOGS,
    ADM_ALL_USERS, ADM_USER_DETAIL,
    # Moderator states
    MOD_MENU,
    ADM_ADD_MOD, ADM_DEL_MOD,
) = range(29)

# ─── Database ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            pin_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            api_link TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS otp_fetches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number_id INTEGER NOT NULL,
            fetched_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(number_id) REFERENCES numbers(id)
        );
        CREATE TABLE IF NOT EXISTS moderators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            added_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            logged_in_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            telegram_username TEXT,
            telegram_first_name TEXT,
            bot_username TEXT,
            action TEXT,
            ts TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    # Migration: create otp_fetches table if missing
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS otp_fetches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number_id INTEGER NOT NULL,
            fetched_at TEXT DEFAULT (datetime('now'))
        )""")
        conn.commit()
        logger.info("Migrated: created otp_fetches table")
    except Exception:
        pass
    # Migration: add telegram_first_name to logs
    try:
        conn.execute("ALTER TABLE logs ADD COLUMN telegram_first_name TEXT")
        conn.commit()
        logger.info("Migrated: added telegram_first_name column to logs")
    except Exception:
        pass  # column already exists
    conn.close()
    logger.info("Database ready at %s", DB_PATH)

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode()).hexdigest()

# ─── DB helpers ─────────────────────────────────────────────────────────────
def db_get_user_by_name(username: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_get_session(telegram_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_create_session(telegram_id: int, username: str, user_id: int):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO sessions(telegram_id, username, user_id, logged_in_at) VALUES(?,?,?,datetime('now'))",
        (telegram_id, username, user_id)
    )
    conn.commit()
    conn.close()

def db_delete_session(telegram_id: int):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()

def db_log(telegram_id, telegram_username, telegram_first_name, bot_username, action):
    conn = get_db()
    conn.execute(
        "INSERT INTO logs(telegram_id, telegram_username, telegram_first_name, bot_username, action) VALUES(?,?,?,?,?)",
        (telegram_id, telegram_username, telegram_first_name, bot_username, action)
    )
    conn.commit()
    conn.close()

OTP_LIMIT = 7
NUMBER_EXPIRY_DAYS = 7

def db_get_numbers(user_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM numbers WHERE user_id=? AND created_at >= datetime('now', ?)",
        (user_id, f"-{NUMBER_EXPIRY_DAYS} days")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def days_until_expiry(created_at_str: str) -> int:
    created = datetime.strptime(created_at_str[:19], "%Y-%m-%d %H:%M:%S")
    expires = created + timedelta(days=NUMBER_EXPIRY_DAYS)
    delta = expires - datetime.utcnow()
    return max(0, delta.days)

def db_otp_check_and_log(num_id: int) -> bool:
    """Returns True and logs fetch if under limit. Returns False if limit exceeded. Single connection."""
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM otp_fetches WHERE number_id=? AND fetched_at >= datetime('now', '-24 hours')",
        (num_id,)
    ).fetchone()
    used = row["cnt"] if row else 0
    if used >= OTP_LIMIT:
        conn.close()
        return False
    conn.execute("INSERT INTO otp_fetches(number_id) VALUES(?)", (num_id,))
    conn.commit()
    conn.close()
    return True

def db_otp_used_today(num_id: int) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM otp_fetches WHERE number_id=? AND fetched_at >= datetime('now', '-24 hours')",
        (num_id,)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0

def db_otp_used_batch(num_ids: list) -> dict:
    """Returns {num_id: used_count} for all ids in one query."""
    if not num_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(num_ids))
    rows = conn.execute(
        f"SELECT number_id, COUNT(*) as cnt FROM otp_fetches WHERE number_id IN ({placeholders}) AND fetched_at >= datetime('now', '-24 hours') GROUP BY number_id",
        num_ids
    ).fetchall()
    conn.close()
    result = {nid: 0 for nid in num_ids}
    for r in rows:
        result[r["number_id"]] = r["cnt"]
    return result

def db_otp_reset_today(num_id: int):
    conn = get_db()
    conn.execute(
        "DELETE FROM otp_fetches WHERE number_id=? AND fetched_at >= datetime('now', '-24 hours')",
        (num_id,)
    )
    conn.commit()
    conn.close()

def db_get_number_by_id(num_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM numbers WHERE id=?", (num_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

# ─── Keyboards ───────────────────────────────────────────────────────────────
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Numbers", callback_data="menu_numbers")],
        [InlineKeyboardButton("🔄 Transfer Number", callback_data="menu_transfer")],
        [InlineKeyboardButton("🔑 Change PIN", callback_data="menu_change_pin")],
        [InlineKeyboardButton("🚪 Logout", callback_data="menu_logout")],
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users", callback_data="adm_all_users")],
        [InlineKeyboardButton("👤 Create User", callback_data="adm_create_user"),
         InlineKeyboardButton("🗑 Delete User", callback_data="adm_del_user")],
        [InlineKeyboardButton("➕ Add Number", callback_data="adm_add_num"),
         InlineKeyboardButton("🗑 Delete Number", callback_data="adm_del_num")],
        [InlineKeyboardButton("📦 Bulk Add Numbers", callback_data="adm_bulk")],
        [InlineKeyboardButton("🔄 Transfer Number", callback_data="adm_transfer")],
        [InlineKeyboardButton("📋 View Logs", callback_data="adm_logs")],
        [InlineKeyboardButton("👮 Manage Moderators", callback_data="adm_manage_mods")],
        [InlineKeyboardButton("🚪 Close Panel", callback_data="adm_close")],
    ])

def mod_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Create User", callback_data="adm_create_user"),
         InlineKeyboardButton("🗑 Delete User", callback_data="adm_del_user")],
        [InlineKeyboardButton("➕ Add Number", callback_data="adm_add_num"),
         InlineKeyboardButton("🗑 Delete Number", callback_data="adm_del_num")],
        [InlineKeyboardButton("📦 Bulk Add Numbers", callback_data="adm_bulk")],
        [InlineKeyboardButton("🔄 Transfer Number", callback_data="adm_transfer")],
        [InlineKeyboardButton("🚪 Close Panel", callback_data="adm_close")],
    ])

def back_kb(data="back_to_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data=data)]])

# ─── Helpers ─────────────────────────────────────────────────────────────────
async def fetch_api_message(api_link: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_link, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                return text.strip() if text.strip() else "(No message returned)"
    except Exception as e:
        return f"⚠️ Error fetching message: {e}"

def is_admin(update: Update) -> bool:
    uid = update.effective_user.id
    return uid in ADMIN_IDS

def is_moderator(telegram_id: int) -> bool:
    conn = get_db()
    row = conn.execute("SELECT id FROM moderators WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return row is not None

def panel_menu_kb(context):
    if context.user_data.get("panel_mode") == "mod":
        return mod_menu_kb()
    return admin_menu_kb()

def panel_menu_state(context):
    if context.user_data.get("panel_mode") == "mod":
        return MOD_MENU
    return ADM_MENU

def panel_title(context):
    if context.user_data.get("panel_mode") == "mod":
        return "👮 *Moderator Panel*"
    return "🔐 *Admin Panel*"

async def require_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = db_get_session(update.effective_user.id)
    if not session:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text("⚠️ Session expired. Use /start to login again.")
        else:
            await update.message.reply_text("⚠️ Session expired. Use /start to login again.")
        return None
    return session

# ═══════════════════════════════════════════════════════════════════════════
# /start — entry point
# ═══════════════════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()

    # Already logged in?
    session = db_get_session(user.id)
    if session:
        await update.message.reply_text(
            f"👋 Welcome back, *{session['username']}*!\n\nWhat would you like to do?",
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )
        return MAIN_MENU

    # Admin shortcut — go straight to admin panel if ADMIN_IDS set and no login needed
    if is_admin(update):
        context.user_data["panel_mode"] = "admin"
        await update.message.reply_text(
            "🔐 *Admin Panel*\n\nYou are recognised as an admin. Choose an action:",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb()
        )
        return ADM_MENU

    # Moderator shortcut
    if is_moderator(update.effective_user.id):
        context.user_data["panel_mode"] = "mod"
        await update.message.reply_text(
            "👮 *Moderator Panel*\n\nYou are recognised as a moderator. Choose an action:",
            parse_mode="Markdown",
            reply_markup=mod_menu_kb()
        )
        return MOD_MENU

    await update.message.reply_text(
        "👋 Welcome!\n\nPlease enter your *username*:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return LOGIN_USERNAME

# ─── Login flow ──────────────────────────────────────────────────────────────
async def login_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    user_rec = db_get_user_by_name(username)
    if not user_rec:
        await update.message.reply_text("❌ Username not found. Try again or contact admin.")
        return LOGIN_USERNAME
    context.user_data["pending_user"] = user_rec
    await update.message.reply_text("🔒 Enter your *PIN*:", parse_mode="Markdown")
    return LOGIN_PIN

async def login_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pin = update.message.text.strip()
    user_rec = context.user_data.get("pending_user")
    if not user_rec or hash_pin(pin) != user_rec["pin_hash"]:
        await update.message.reply_text("❌ Incorrect PIN. Try again:")
        return LOGIN_PIN

    tg_user = update.effective_user
    db_create_session(tg_user.id, user_rec["username"], user_rec["id"])
    db_log(tg_user.id, tg_user.username, tg_user.first_name, user_rec["username"], "login")
    context.user_data.clear()

    await update.message.reply_text(
        f"✅ Logged in as *{user_rec['username']}*!\n\nWhat would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )
    return MAIN_MENU

# ═══════════════════════════════════════════════════════════════════════════
# Main Menu callbacks
# ═══════════════════════════════════════════════════════════════════════════
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    session = await require_login(update, context)
    if not session:
        return ConversationHandler.END

    if data == "menu_logout":
        db_delete_session(update.effective_user.id)
        db_log(update.effective_user.id, update.effective_user.username, update.effective_user.first_name, session["username"], "logout")
        await query.message.edit_text("👋 Logged out. Use /start to login again.")
        return ConversationHandler.END

    if data == "menu_numbers":
        numbers = db_get_numbers(session["user_id"])
        if not numbers:
            await query.message.edit_text(
                "📭 You have no numbers assigned yet.",
                reply_markup=back_kb("back_to_menu")
            )
            return NUM_LIST
        buttons = [[InlineKeyboardButton(f"📞 {n['phone']}", callback_data=f"num_{n['id']}")] for n in numbers]
        buttons.append([InlineKeyboardButton("⬅️ Menu", callback_data="back_to_menu")])
        await query.message.edit_text(
            "📱 *Your Numbers:*\nSelect a number to view its message.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return NUM_LIST

    if data == "menu_transfer":
        await query.message.edit_text(
            "🔄 *Transfer Number*\n\nEnter the *username* of the user you want to transfer a number to:",
            parse_mode="Markdown",
            reply_markup=back_kb("back_to_menu")
        )
        return TRANSFER_USER

    if data == "menu_change_pin":
        await query.message.edit_text(
            "🔑 *Change PIN*\n\nEnter your *current PIN*:",
            parse_mode="Markdown",
            reply_markup=back_kb("back_to_menu")
        )
        return CHANGE_PIN_OLD

    if data == "back_to_menu":
        await query.message.edit_text(
            "🏠 *Main Menu*",
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )
        return MAIN_MENU

    return MAIN_MENU

# ═══════════════════════════════════════════════════════════════════════════
# Numbers
# ═══════════════════════════════════════════════════════════════════════════
async def num_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    session = await require_login(update, context)
    if not session:
        return ConversationHandler.END

    if data == "back_to_menu":
        await query.message.edit_text("🏠 *Main Menu*", parse_mode="Markdown", reply_markup=main_menu_kb())
        return MAIN_MENU

    if data.startswith("num_"):
        num_id = int(data.split("_")[1])
        num = db_get_number_by_id(num_id)
        if not num or num["user_id"] != session["user_id"]:
            await query.message.edit_text("❌ Number not found.", reply_markup=back_kb("back_to_menu"))
            return NUM_LIST
        context.user_data["viewing_num_id"] = num_id

        allowed = db_otp_check_and_log(num_id)
        if not allowed:
            await query.message.edit_text(
                f"📞 *{num['phone']}*\n\n📵 *Message receiving limit exceeded.*\nPlease try again after 24 hours.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Menu", callback_data="back_to_menu_from_num"),
                     InlineKeyboardButton("🚪 Logout", callback_data="logout_from_num")],
                ])
            )
            return NUM_DETAIL

        msg = await fetch_api_message(num["api_link"])
        days_left = days_until_expiry(num["created_at"])
        expiry_line = f"\n\n⏳ *Expires in {days_left} day(s)*" if days_left > 0 else "\n\n⚠️ *This number expires today*"
        await query.message.edit_text(
            f"📞 *{num['phone']}*\n\n📨 *Your Message:*\n`{msg}`{expiry_line}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{num_id}")],
                [InlineKeyboardButton("⬅️ Menu", callback_data="back_to_menu_from_num"),
                 InlineKeyboardButton("🚪 Logout", callback_data="logout_from_num")],
            ])
        )
        return NUM_DETAIL

    return NUM_LIST

async def num_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    session = await require_login(update, context)
    if not session:
        return ConversationHandler.END

    if data.startswith("refresh_"):
        num_id = int(data.split("_")[1])
        num = db_get_number_by_id(num_id)
        if not num or num["user_id"] != session["user_id"]:
            await query.message.edit_text("❌ Number not found.")
            return NUM_LIST

        allowed = db_otp_check_and_log(num_id)
        if not allowed:
            await query.answer("📵 Message receiving limit exceeded. Try again after 24 hours.", show_alert=True)
            return NUM_DETAIL

        await query.answer("⏳ Fetching…")
        msg = await fetch_api_message(num["api_link"])
        days_left = days_until_expiry(num["created_at"])
        expiry_line = f"\n\n⏳ *Expires in {days_left} day(s)*" if days_left > 0 else "\n\n⚠️ *This number expires today*"
        await query.message.edit_text(
            f"📞 *{num['phone']}*\n\n📨 *Your Message:*\n`{msg}`{expiry_line}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{num_id}")],
                [InlineKeyboardButton("⬅️ Menu", callback_data="back_to_menu_from_num"),
                 InlineKeyboardButton("🚪 Logout", callback_data="logout_from_num")],
            ])
        )
        return NUM_DETAIL

    if data == "back_to_menu_from_num":
        await query.message.edit_text("🏠 *Main Menu*", parse_mode="Markdown", reply_markup=main_menu_kb())
        return MAIN_MENU

    if data == "logout_from_num":
        db_delete_session(update.effective_user.id)
        await query.message.edit_text("👋 Logged out. Use /start to login again.")
        return ConversationHandler.END

    return NUM_DETAIL

# ═══════════════════════════════════════════════════════════════════════════
# Transfer Number (User side)
# ═══════════════════════════════════════════════════════════════════════════
async def transfer_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text("🏠 *Main Menu*", parse_mode="Markdown", reply_markup=main_menu_kb())
        return MAIN_MENU

    session = await require_login(update, context)
    if not session:
        return ConversationHandler.END

    target_username = update.message.text.strip()
    target = db_get_user_by_name(target_username)
    if not target:
        await update.message.reply_text("❌ User not found. Enter a valid username or type /cancel:")
        return TRANSFER_USER
    if target["id"] == session["user_id"]:
        await update.message.reply_text("❌ You can't transfer to yourself. Enter another username:")
        return TRANSFER_USER

    context.user_data["transfer_target"] = target
    numbers = db_get_numbers(session["user_id"])
    if not numbers:
        await update.message.reply_text("📭 You have no numbers to transfer.", reply_markup=main_menu_kb())
        return MAIN_MENU

    buttons = [[InlineKeyboardButton(f"📞 {n['phone']}", callback_data=f"txnum_{n['id']}")] for n in numbers]
    buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_menu")])
    await update.message.reply_text(
        f"Select a number to transfer to *{target['username']}*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return TRANSFER_NUM

async def transfer_num_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    session = await require_login(update, context)
    if not session:
        return ConversationHandler.END

    if data == "back_to_menu":
        await query.message.edit_text("🏠 *Main Menu*", parse_mode="Markdown", reply_markup=main_menu_kb())
        return MAIN_MENU

    if data.startswith("txnum_"):
        num_id = int(data.split("_")[1])
        num = db_get_number_by_id(num_id)
        if not num or num["user_id"] != session["user_id"]:
            await query.message.edit_text("❌ Number not found.")
            return TRANSFER_NUM
        target = context.user_data.get("transfer_target")
        context.user_data["transfer_num_id"] = num_id
        await query.message.edit_text(
            f"⚠️ *Confirm Transfer*\n\nTransfer *{num['phone']}* to *{target['username']}*?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes", callback_data="tx_confirm_yes"),
                 InlineKeyboardButton("❌ No", callback_data="tx_confirm_no")],
            ])
        )
        return TRANSFER_CONFIRM

    return TRANSFER_NUM

async def transfer_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = await require_login(update, context)
    if not session:
        return ConversationHandler.END

    if query.data == "tx_confirm_yes":
        num_id = context.user_data.get("transfer_num_id")
        target = context.user_data.get("transfer_target")
        if num_id and target:
            conn = get_db()
            conn.execute("UPDATE numbers SET user_id=? WHERE id=?", (target["id"], num_id))
            conn.commit()
            conn.close()
            db_log(update.effective_user.id, update.effective_user.username, update.effective_user.first_name, session["username"],
                   f"transferred number id={num_id} to user {target['username']}")
            await query.message.edit_text(
                f"✅ Number transferred to *{target['username']}* successfully!",
                parse_mode="Markdown",
                reply_markup=back_kb("back_to_menu")
            )
        return MAIN_MENU

    await query.message.edit_text("↩️ Transfer cancelled.", reply_markup=main_menu_kb())
    return MAIN_MENU

# ═══════════════════════════════════════════════════════════════════════════
# Change PIN
# ═══════════════════════════════════════════════════════════════════════════
async def change_pin_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text("🏠 *Main Menu*", parse_mode="Markdown", reply_markup=main_menu_kb())
        return MAIN_MENU

    session = await require_login(update, context)
    if not session:
        return ConversationHandler.END

    pin = update.message.text.strip()
    user_rec = db_get_user_by_name(session["username"])
    if not user_rec or hash_pin(pin) != user_rec["pin_hash"]:
        await update.message.reply_text("❌ Incorrect PIN. Try again or /cancel:")
        return CHANGE_PIN_OLD

    context.user_data["change_pin_verified"] = True
    await update.message.reply_text("✅ Verified! Enter your *new PIN*:", parse_mode="Markdown")
    return CHANGE_PIN_NEW

async def change_pin_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pin = update.message.text.strip()
    if len(pin) < 4:
        await update.message.reply_text("❌ PIN must be at least 4 characters. Try again:")
        return CHANGE_PIN_NEW
    context.user_data["new_pin"] = pin
    await update.message.reply_text("🔁 Confirm your new PIN:")
    return CHANGE_PIN_CONFIRM

async def change_pin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = await require_login(update, context)
    if not session:
        return ConversationHandler.END

    pin = update.message.text.strip()
    new_pin = context.user_data.get("new_pin")
    if pin != new_pin:
        await update.message.reply_text("❌ PINs don't match. Enter new PIN again:")
        return CHANGE_PIN_NEW

    conn = get_db()
    conn.execute("UPDATE users SET pin_hash=? WHERE id=?", (hash_pin(pin), session["user_id"]))
    conn.commit()
    conn.close()
    db_log(update.effective_user.id, update.effective_user.username, update.effective_user.first_name, session["username"], "changed PIN")
    context.user_data.pop("new_pin", None)
    await update.message.reply_text("✅ PIN changed successfully!", reply_markup=main_menu_kb())
    return MAIN_MENU

# ═══════════════════════════════════════════════════════════════════════════
# /admin — Admin Panel (Telegram-ID gated)
# ═══════════════════════════════════════════════════════════════════════════
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END
    context.user_data["panel_mode"] = "admin"
    await update.message.reply_text(
        "🔐 *Admin Panel*\n\nChoose an action:",
        parse_mode="Markdown",
        reply_markup=admin_menu_kb()
    )
    return ADM_MENU

async def mod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if not is_admin(update) and not is_moderator(tid):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END
    context.user_data["panel_mode"] = "mod"
    await update.message.reply_text(
        "👮 *Moderator Panel*\n\nChoose an action:",
        parse_mode="Markdown",
        reply_markup=mod_menu_kb()
    )
    return MOD_MENU

async def adm_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_close":
        await query.message.edit_text("✅ Admin panel closed.")
        return ConversationHandler.END

    # ── Create User ──────────────────────────────────────────────────────────
    if data == "adm_create_user":
        await query.message.edit_text(
            "👤 *Create User*\n\nEnter the new username:",
            parse_mode="Markdown",
            reply_markup=back_kb("adm_back")
        )
        return ADM_CREATE_USER

    # ── Delete User ──────────────────────────────────────────────────────────
    if data == "adm_del_user":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users found.", reply_markup=back_kb("adm_back"))
            return ADM_MENU
        buttons = [[InlineKeyboardButton(f"🗑 {u['username']}", callback_data=f"delusr_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select a user to delete:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_DEL_USER

    # ── Add Number ───────────────────────────────────────────────────────────
    if data == "adm_add_num":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users found. Create a user first.", reply_markup=back_kb("adm_back"))
            return ADM_MENU
        buttons = [[InlineKeyboardButton(u["username"], callback_data=f"addnum_usr_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select user to assign number to:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_ADD_NUM_USER

    # ── Delete Number ────────────────────────────────────────────────────────
    if data == "adm_del_num":
        conn = get_db()
        rows = conn.execute(
            "SELECT n.id, n.phone, u.username FROM numbers n JOIN users u ON n.user_id=u.id"
        ).fetchall()
        conn.close()
        if not rows:
            await query.message.edit_text("📭 No numbers found.", reply_markup=back_kb("adm_back"))
            return ADM_MENU
        buttons = [[InlineKeyboardButton(f"🗑 {r['phone']} ({r['username']})", callback_data=f"delnum_{r['id']}")] for r in rows]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select a number to delete:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_DEL_NUM

    # ── Bulk Add ─────────────────────────────────────────────────────────────
    if data == "adm_bulk":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users found.", reply_markup=back_kb("adm_back"))
            return ADM_MENU
        buttons = [[InlineKeyboardButton(u["username"], callback_data=f"bulk_usr_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text(
            "📦 *Bulk Add Numbers*\n\nFirst, select which user to assign them to:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return ADM_BULK_PASTE

    # ── Transfer (admin) ─────────────────────────────────────────────────────
    if data == "adm_transfer":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users found.", reply_markup=back_kb("adm_back"))
            return ADM_MENU
        buttons = [[InlineKeyboardButton(u["username"], callback_data=f"adm_xfr_from_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("🔄 Select *source* user (who currently owns the number):", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_XFER_FROM

    # ── All Users ─────────────────────────────────────────────────────────────
    if data == "adm_all_users":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users yet.", reply_markup=back_kb("adm_back"))
            return ADM_MENU
        buttons = [[InlineKeyboardButton(f"👤 {u['username']}", callback_data=f"adm_view_user_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text(
            f"👥 *All Users* ({len(users)} total)\n\nTap a user to see their numbers:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return ADM_ALL_USERS

    # ── Logs ─────────────────────────────────────────────────────────────────
    if data == "adm_logs":
        conn = get_db()
        rows = conn.execute("SELECT * FROM logs ORDER BY ts DESC LIMIT 50").fetchall()
        conn.close()
        if not rows:
            await query.message.edit_text("📋 No logs yet.", reply_markup=back_kb("adm_back"))
            return ADM_MENU
        text = "📋 *Recent Logs (last 50):*\n\n"
        for r in rows:
            tg_id = r['telegram_id'] or '—'
            tg_uname = f"@{r['telegram_username']}" if r['telegram_username'] else '(no @handle)'
            tg_name = r['telegram_first_name'] or '—'
            text += f"• `{r['ts']}`\n  🆔 ID: `{tg_id}`\n  👤 Name: {tg_name} | {tg_uname}\n  📌 Bot user: *{r['bot_username']}* → _{r['action']}_\n\n"
        if len(text) > 4000:
            text = text[:4000] + "\n…(truncated)"
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb("adm_back"))
        return ADM_MENU

    # ── Manage Moderators ─────────────────────────────────────────────────────
    if data == "adm_manage_mods":
        conn = get_db()
        mods = conn.execute("SELECT * FROM moderators ORDER BY added_at DESC").fetchall()
        conn.close()
        text = "👮 *Manage Moderators*\n\n"
        if mods:
            for m in mods:
                text += f"• Telegram ID: `{m['telegram_id']}` (added {m['added_at'][:10]})\n"
        else:
            text += "_No moderators yet._\n"
        buttons = [
            [InlineKeyboardButton("➕ Add Moderator", callback_data="adm_add_mod")],
            [InlineKeyboardButton("🗑 Remove Moderator", callback_data="adm_del_mod")],
            [InlineKeyboardButton("⬅️ Back", callback_data="adm_back")],
        ]
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_MENU

    if data == "adm_add_mod":
        await query.message.edit_text(
            "👮 *Add Moderator*\n\nEnter the *Telegram ID* (numeric) of the person to add as moderator:",
            parse_mode="Markdown",
            reply_markup=back_kb("adm_back")
        )
        return ADM_ADD_MOD

    if data == "adm_del_mod":
        conn = get_db()
        mods = conn.execute("SELECT * FROM moderators ORDER BY added_at DESC").fetchall()
        conn.close()
        if not mods:
            await query.message.edit_text("📭 No moderators to remove.", reply_markup=back_kb("adm_back"))
            return ADM_MENU
        buttons = [[InlineKeyboardButton(f"🗑 ID: {m['telegram_id']}", callback_data=f"delmod_{m['telegram_id']}")] for m in mods]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select a moderator to remove:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_DEL_MOD

    if data == "adm_back":
        await query.message.edit_text("🔐 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_kb())
        return ADM_MENU

    return ADM_MENU

# ── Moderator Panel ───────────────────────────────────────────────────────────
async def mod_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_close":
        await query.message.edit_text("✅ Moderator panel closed.")
        return ConversationHandler.END

    if data == "adm_create_user":
        await query.message.edit_text(
            "👤 *Create User*\n\nEnter the new username:",
            parse_mode="Markdown",
            reply_markup=back_kb("adm_back")
        )
        return ADM_CREATE_USER

    if data == "adm_del_user":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users found.", reply_markup=back_kb("adm_back"))
            return MOD_MENU
        buttons = [[InlineKeyboardButton(f"🗑 {u['username']}", callback_data=f"delusr_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select a user to delete:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_DEL_USER

    if data == "adm_add_num":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users found. Create a user first.", reply_markup=back_kb("adm_back"))
            return MOD_MENU
        buttons = [[InlineKeyboardButton(u["username"], callback_data=f"addnum_usr_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select user to assign number to:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_ADD_NUM_USER

    if data == "adm_del_num":
        conn = get_db()
        rows = conn.execute(
            "SELECT n.id, n.phone, u.username FROM numbers n JOIN users u ON n.user_id=u.id"
        ).fetchall()
        conn.close()
        if not rows:
            await query.message.edit_text("📭 No numbers found.", reply_markup=back_kb("adm_back"))
            return MOD_MENU
        buttons = [[InlineKeyboardButton(f"🗑 {r['phone']} ({r['username']})", callback_data=f"delnum_{r['id']}")] for r in rows]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select a number to delete:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_DEL_NUM

    if data == "adm_bulk":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users found.", reply_markup=back_kb("adm_back"))
            return MOD_MENU
        buttons = [[InlineKeyboardButton(u["username"], callback_data=f"bulk_usr_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text(
            "📦 *Bulk Add Numbers*\n\nFirst, select which user to assign them to:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return ADM_BULK_PASTE

    if data == "adm_transfer":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        if not users:
            await query.message.edit_text("📭 No users found.", reply_markup=back_kb("adm_back"))
            return MOD_MENU
        buttons = [[InlineKeyboardButton(u["username"], callback_data=f"adm_xfr_from_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text(
            "🔄 Select *source* user (who currently owns the number):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return ADM_XFER_FROM

    if data == "adm_back":
        await query.message.edit_text("👮 *Moderator Panel*", parse_mode="Markdown", reply_markup=mod_menu_kb())
        return MOD_MENU

    return MOD_MENU

# ── Add/Remove Moderator steps ────────────────────────────────────────────────
async def adm_add_mod_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text("🔐 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_kb())
        return ADM_MENU
    tid_str = update.message.text.strip()
    if not tid_str.lstrip("-").isdigit():
        await update.message.reply_text("❌ Please enter a valid numeric Telegram ID:")
        return ADM_ADD_MOD
    tid = int(tid_str)
    conn = get_db()
    existing = conn.execute("SELECT id FROM moderators WHERE telegram_id=?", (tid,)).fetchone()
    if existing:
        conn.close()
        await update.message.reply_text("⚠️ This Telegram ID is already a moderator.", reply_markup=admin_menu_kb())
    else:
        conn.execute("INSERT INTO moderators(telegram_id) VALUES(?)", (tid,))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Telegram ID `{tid}` added as moderator!\n\nThey can now use /mod to access the moderator panel.",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb()
        )
    return ADM_MENU

async def adm_del_mod_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "adm_back":
        await query.message.edit_text("🔐 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_kb())
        return ADM_MENU
    if data.startswith("delmod_"):
        tid = int(data.split("_")[1])
        conn = get_db()
        conn.execute("DELETE FROM moderators WHERE telegram_id=?", (tid,))
        conn.commit()
        conn.close()
        await query.message.edit_text(f"✅ Moderator `{tid}` removed.", parse_mode="Markdown", reply_markup=admin_menu_kb())
        return ADM_MENU
    return ADM_DEL_MOD

# ── Create User steps ────────────────────────────────────────────────────────
async def adm_create_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(panel_title(context), parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    username = update.message.text.strip()
    if db_get_user_by_name(username):
        await update.message.reply_text("❌ Username already exists. Enter a different username:")
        return ADM_CREATE_USER
    context.user_data["new_user_name"] = username
    await update.message.reply_text(f"Set a PIN for *{username}*:", parse_mode="Markdown")
    return ADM_CREATE_USER_PIN

async def adm_create_user_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pin = update.message.text.strip()
    username = context.user_data.get("new_user_name")
    if not username:
        await update.message.reply_text("Something went wrong. Use /admin again.")
        return ConversationHandler.END
    try:
        conn = get_db()
        conn.execute("INSERT INTO users(username, pin_hash) VALUES(?,?)", (username, hash_pin(pin)))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ User *{username}* created!\n\nPIN: `{pin}` (share this with the user)",
            parse_mode="Markdown",
            reply_markup=panel_menu_kb(context)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=panel_menu_kb(context))
    context.user_data.pop("new_user_name", None)
    return panel_menu_state(context)

# ── Delete User ──────────────────────────────────────────────────────────────
async def adm_del_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back":
        await query.message.edit_text(panel_title(context), parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    if data.startswith("delusr_"):
        uid = int(data.split("_")[1])
        conn = get_db()
        user = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        conn.execute("DELETE FROM numbers WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        conn.close()
        name = user["username"] if user else uid
        await query.message.edit_text(f"✅ User *{name}* and their numbers deleted.", parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    return ADM_DEL_USER

# ── Add Number steps ─────────────────────────────────────────────────────────
async def adm_add_num_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back":
        await query.message.edit_text(panel_title(context), parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    if data.startswith("addnum_usr_"):
        uid = int(data.split("_")[2])
        context.user_data["addnum_user_id"] = uid
        await query.message.edit_text("📞 Enter the phone number:")
        return ADM_ADD_NUM_PHONE

    return ADM_ADD_NUM_USER

async def adm_add_num_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data["addnum_phone"] = phone
    await update.message.reply_text(f"🔗 Now enter the *API link* for `{phone}`:", parse_mode="Markdown")
    return ADM_ADD_NUM_API

async def adm_add_num_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_link = update.message.text.strip()
    uid = context.user_data.get("addnum_user_id")
    phone = context.user_data.get("addnum_phone")
    try:
        conn = get_db()
        conn.execute("INSERT INTO numbers(phone, api_link, user_id) VALUES(?,?,?)", (phone, api_link, uid))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Number *{phone}* added successfully!",
            parse_mode="Markdown",
            reply_markup=panel_menu_kb(context)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=panel_menu_kb(context))
    context.user_data.pop("addnum_user_id", None)
    context.user_data.pop("addnum_phone", None)
    return panel_menu_state(context)

# ── Delete Number ────────────────────────────────────────────────────────────
async def adm_del_num_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back":
        await query.message.edit_text(panel_title(context), parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    if data.startswith("delnum_"):
        num_id = int(data.split("_")[1])
        conn = get_db()
        num = conn.execute("SELECT phone FROM numbers WHERE id=?", (num_id,)).fetchone()
        conn.execute("DELETE FROM numbers WHERE id=?", (num_id,))
        conn.commit()
        conn.close()
        phone = num["phone"] if num else num_id
        await query.message.edit_text(f"✅ Number *{phone}* deleted.", parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    return ADM_DEL_NUM

# ── Bulk Add Numbers ─────────────────────────────────────────────────────────
async def adm_bulk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back":
        await query.message.edit_text(panel_title(context), parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    if data.startswith("bulk_usr_"):
        uid = int(data.split("_")[2])
        context.user_data["bulk_user_id"] = uid
        conn = get_db()
        user = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        conn.close()
        uname = user["username"] if user else uid
        await query.message.edit_text(
            f"📦 *Bulk Add Numbers for {uname}*\n\n"
            "Paste numbers and API links. Each line: `phone|api_link`\n\nExample:\n"
            "`+1234567890|https://api.example.com/msg1`\n`+9876543210|https://api.example.com/msg2`",
            parse_mode="Markdown"
        )
        return ADM_BULK_PASTE

    return ADM_BULK_PASTE

async def adm_bulk_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = context.user_data.get("bulk_user_id")
    if not uid:
        await update.message.reply_text("Something went wrong. Use /admin again.")
        return ConversationHandler.END

    lines = update.message.text.strip().split("\n")
    added, errors = 0, []
    conn = get_db()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            errors.append(f"Bad format: `{line}`")
            continue
        phone, api_link = parts[0].strip(), parts[1].strip()
        try:
            conn.execute("INSERT INTO numbers(phone, api_link, user_id) VALUES(?,?,?)", (phone, api_link, uid))
            added += 1
        except Exception as e:
            errors.append(f"`{phone}`: {e}")
    conn.commit()
    conn.close()

    msg = f"✅ Added *{added}* number(s)."
    if errors:
        msg += "\n\n⚠️ Errors:\n" + "\n".join(errors[:10])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=panel_menu_kb(context))
    context.user_data.pop("bulk_user_id", None)
    return panel_menu_state(context)

# ── Admin Transfer ───────────────────────────────────────────────────────────
async def adm_xfer_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back":
        await query.message.edit_text(panel_title(context), parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    if data.startswith("adm_xfr_from_"):
        uid = int(data.split("_")[3])
        context.user_data["adm_xfr_from"] = uid
        numbers = db_get_numbers(uid)
        if not numbers:
            await query.message.edit_text("📭 This user has no numbers.", reply_markup=back_kb("adm_back"))
            return ADM_XFER_FROM
        buttons = [[InlineKeyboardButton(f"📞 {n['phone']}", callback_data=f"adm_xfr_num_{n['id']}")] for n in numbers]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select the number to transfer:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_XFER_NUM

    return ADM_XFER_FROM

async def adm_xfer_num_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back":
        await query.message.edit_text(panel_title(context), parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    if data.startswith("adm_xfr_num_"):
        num_id = int(data.split("_")[3])
        context.user_data["adm_xfr_num_id"] = num_id
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users").fetchall()
        conn.close()
        from_uid = context.user_data.get("adm_xfr_from")
        buttons = [[InlineKeyboardButton(u["username"], callback_data=f"adm_xfr_to_{u['id']}")] for u in users if u["id"] != from_uid]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text("Select *destination* user:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return ADM_XFER_TO

    return ADM_XFER_NUM

async def adm_xfer_to_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back":
        await query.message.edit_text(panel_title(context), parse_mode="Markdown", reply_markup=panel_menu_kb(context))
        return panel_menu_state(context)

    if data.startswith("adm_xfr_to_"):
        to_uid = int(data.split("_")[3])
        num_id = context.user_data.get("adm_xfr_num_id")
        conn = get_db()
        num = conn.execute("SELECT phone FROM numbers WHERE id=?", (num_id,)).fetchone()
        user = conn.execute("SELECT username FROM users WHERE id=?", (to_uid,)).fetchone()
        conn.execute("UPDATE numbers SET user_id=? WHERE id=?", (to_uid, num_id))
        conn.commit()
        conn.close()
        phone = num["phone"] if num else num_id
        uname = user["username"] if user else to_uid
        await query.message.edit_text(
            f"✅ Number *{phone}* transferred to *{uname}*.",
            parse_mode="Markdown",
            reply_markup=panel_menu_kb(context)
        )
        return panel_menu_state(context)

    return ADM_XFER_TO

# ── All Users (admin) ────────────────────────────────────────────────────────
async def adm_all_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back":
        await query.message.edit_text("🔐 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_kb())
        return ADM_MENU

    if data.startswith("adm_view_user_"):
        uid = int(data.split("_")[3])
        conn = get_db()
        user = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        numbers = conn.execute("SELECT * FROM numbers WHERE user_id=?", (uid,)).fetchall()
        conn.close()
        if not user:
            await query.message.edit_text("❌ User not found.", reply_markup=back_kb("adm_back"))
            return ADM_ALL_USERS
        uname = user["username"]
        context.user_data["adm_viewing_uid"] = uid
        context.user_data["adm_viewing_uname"] = uname
        if not numbers:
            await query.message.edit_text(
                f"👤 *{uname}*\n\n📭 No numbers assigned.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm_users_list")]])
            )
            return ADM_USER_DETAIL

        num_ids = [n["id"] for n in numbers]
        otp_map = db_otp_used_batch(num_ids)
        lines = [f"👤 *{uname}* — {len(numbers)} number(s)\n"]
        buttons = []
        for n in numbers:
            remaining = OTP_LIMIT - otp_map.get(n["id"], 0)
            status = f"🔢 {remaining}/7 left today" if remaining > 0 else "🚫 0/7 (limit reached)"
            lines.append(f"📞 `{n['phone']}` {status}")
            buttons.append([InlineKeyboardButton(
                f"🔄 Reset OTP — {n['phone']}", callback_data=f"adm_reset_otp_{n['id']}"
            )])
        buttons.append([InlineKeyboardButton("⬅️ Back to Users", callback_data="adm_users_list")])
        await query.message.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return ADM_USER_DETAIL

    # Re-show user list
    conn = get_db()
    users = conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()
    conn.close()
    buttons = [[InlineKeyboardButton(f"👤 {u['username']}", callback_data=f"adm_view_user_{u['id']}")] for u in users]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
    await query.message.edit_text(
        f"👥 *All Users* ({len(users)} total)\n\nTap a user to see their numbers:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADM_ALL_USERS

async def adm_user_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_users_list":
        conn = get_db()
        users = conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()
        conn.close()
        buttons = [[InlineKeyboardButton(f"👤 {u['username']}", callback_data=f"adm_view_user_{u['id']}")] for u in users]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await query.message.edit_text(
            f"👥 *All Users* ({len(users)} total)\n\nTap a user to see their numbers:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return ADM_ALL_USERS

    if data.startswith("adm_reset_otp_"):
        num_id = int(data.split("_")[3])
        db_otp_reset_today(num_id)
        conn = get_db()
        num = conn.execute("SELECT phone FROM numbers WHERE id=?", (num_id,)).fetchone()
        conn.close()
        phone = num["phone"] if num else num_id
        await query.answer(f"✅ OTP reset to 7/7 for {phone}", show_alert=True)

        # Refresh the user detail view
        uid = context.user_data.get("adm_viewing_uid")
        uname = context.user_data.get("adm_viewing_uname", "User")
        if uid:
            conn = get_db()
            numbers = conn.execute("SELECT * FROM numbers WHERE user_id=?", (uid,)).fetchall()
            conn.close()
            num_ids = [n["id"] for n in numbers]
            otp_map = db_otp_used_batch(num_ids)
            lines = [f"👤 *{uname}* — {len(numbers)} number(s)\n"]
            buttons = []
            for n in numbers:
                remaining = OTP_LIMIT - otp_map.get(n["id"], 0)
                status = f"🔢 {remaining}/7 left today" if remaining > 0 else "🚫 0/7 (limit reached)"
                lines.append(f"📞 `{n['phone']}` {status}")
                buttons.append([InlineKeyboardButton(
                    f"🔄 Reset OTP — {n['phone']}", callback_data=f"adm_reset_otp_{n['id']}"
                )])
            buttons.append([InlineKeyboardButton("⬅️ Back to Users", callback_data="adm_users_list")])
            await query.message.edit_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        return ADM_USER_DETAIL

    if data == "adm_back":
        await query.message.edit_text("🔐 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_kb())
        return ADM_MENU

    return ADM_USER_DETAIL

# ─── /cancel ─────────────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("↩️ Action cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════════════
# Fallback / unknown
# ═══════════════════════════════════════════════════════════════════════════
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start to begin or /cancel to reset.")

# ─── Background cleanup ───────────────────────────────────────────────────────
async def cleanup_expired_numbers_task():
    """Runs forever: deletes numbers older than NUMBER_EXPIRY_DAYS every 24 h."""
    while True:
        try:
            conn = get_db()
            cur = conn.execute(
                "DELETE FROM numbers WHERE created_at < datetime('now', ?)",
                (f"-{NUMBER_EXPIRY_DAYS} days",)
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            if deleted:
                logger.info(f"Auto-deleted {deleted} expired number(s).")
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")
        await asyncio.sleep(86400)

async def post_init(app):
    asyncio.create_task(cleanup_expired_numbers_task())

# ═══════════════════════════════════════════════════════════════════════════
# Build & run
# ═══════════════════════════════════════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN env var is not set. Exiting.")
        raise SystemExit(1)

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("admin", admin_cmd),
            CommandHandler("mod", mod_cmd),
        ],
        states={
            LOGIN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_username)],
            LOGIN_PIN:      [MessageHandler(filters.TEXT & ~filters.COMMAND, login_pin)],
            MAIN_MENU: [CallbackQueryHandler(main_menu_callback)],
            NUM_LIST: [CallbackQueryHandler(num_list_callback)],
            NUM_DETAIL: [CallbackQueryHandler(num_detail_callback)],
            TRANSFER_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, transfer_user_input),
                CallbackQueryHandler(transfer_user_input),
            ],
            TRANSFER_NUM: [CallbackQueryHandler(transfer_num_callback)],
            TRANSFER_CONFIRM: [CallbackQueryHandler(transfer_confirm_callback)],
            CHANGE_PIN_OLD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, change_pin_old),
                CallbackQueryHandler(change_pin_old),
            ],
            CHANGE_PIN_NEW:     [MessageHandler(filters.TEXT & ~filters.COMMAND, change_pin_new)],
            CHANGE_PIN_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_pin_confirm)],
            # Admin
            ADM_MENU: [CallbackQueryHandler(adm_menu_callback)],
            ADM_ADD_MOD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_mod_input),
                CallbackQueryHandler(adm_add_mod_input),
            ],
            ADM_DEL_MOD: [CallbackQueryHandler(adm_del_mod_callback)],
            ADM_CREATE_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_create_user_input),
                CallbackQueryHandler(adm_create_user_input),
            ],
            ADM_CREATE_USER_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_create_user_pin)],
            ADM_DEL_USER:        [CallbackQueryHandler(adm_del_user_callback)],
            ADM_ADD_NUM_USER:    [CallbackQueryHandler(adm_add_num_user_callback)],
            ADM_ADD_NUM_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_num_phone)],
            ADM_ADD_NUM_API:     [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_num_api)],
            ADM_BULK_PASTE: [
                CallbackQueryHandler(adm_bulk_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bulk_paste),
            ],
            ADM_DEL_NUM:   [CallbackQueryHandler(adm_del_num_callback)],
            ADM_XFER_FROM: [CallbackQueryHandler(adm_xfer_from_callback)],
            ADM_XFER_NUM:  [CallbackQueryHandler(adm_xfer_num_callback)],
            ADM_XFER_TO:   [CallbackQueryHandler(adm_xfer_to_callback)],
            ADM_ALL_USERS:   [CallbackQueryHandler(adm_all_users_callback)],
            ADM_USER_DETAIL: [CallbackQueryHandler(adm_user_detail_callback)],
            # Moderator (shares sub-state handlers with admin)
            MOD_MENU: [CallbackQueryHandler(mod_menu_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
