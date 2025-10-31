# main.py
import os
import json
import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Dict, Any, List

from fastapi import FastAPI, Request, Response
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ---------------------------
# CONFIG (via env vars)
# ---------------------------
BOT_TOKEN = os.getenv("8423955356:AAEOfmiGaoHbYoLoZJREtWq_sb50dG5i9Xc")            # Bot token from BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "5841736888"))   # Your Telegram user id (owner)
SERVICE_URL = os.getenv("SERVICE_URL", "")   # e.g. https://your-app.onrender.com
DATA_FILE = "data.json"
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "6"))
WEBHOOK_PATH = f"/{BOT_TOKEN}"
DEFAULT_INTERVAL_MIN = int(os.getenv("DEFAULT_INTERVAL_MIN", "30"))
# ---------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smartlink-hub")

app = FastAPI()

# ---------------------------
# Persistent storage helpers
# ---------------------------
def init_data():
    if not os.path.exists(DATA_FILE):
        base = {
            "settings": {
                "chat_id": None,
                "interval": DEFAULT_INTERVAL_MIN,
                "running": False,
                "last_link": None,
                "rotation_index": 0
            },
            # links: list of { "link": str, "owner_id": int, "owner_username": str, "added_at": iso }
            "links": [],
            # users: userid -> { "username": str, "token": str, "invites": int, "links_added": int, "limit": int, "interval": None }
            "users": {},
            # referrals: token -> referrer_userid
            "referrals": {}
        }
        with open(DATA_FILE, "w") as f:
            json.dump(base, f, indent=2)
        return base
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

data = init_data()
data_lock = asyncio.Lock()

# ---------------------------
# Utility functions
# ---------------------------
def ensure_user_entry(user_id: int, username: str = None):
    uid = str(user_id)
    if uid not in data["users"]:
        token = secrets.token_urlsafe(8)
        data["users"][uid] = {
            "username": username or "",
            "token": token,
            "invites": 0,
            "links_added": 0,
            "limit": 5,  # starts with 5 slots
            "interval": None  # optional per-user interval (minutes)
        }
        data["referrals"][token] = user_id
        save_data(data)
    else:
        # update username if changed
        if username and data["users"][uid].get("username") != username:
            data["users"][uid]["username"] = username
            save_data(data)
    return data["users"][uid]

def compute_limit_from_invites(invites:int) -> int:
    # tiered limits
    if invites >= 60:
        return 30
    if invites >= 40:
        return 20
    if invites >= 20:
        return 10
    return 5

def get_bot_username(bot: Bot):
    return bot.username or bot.get_me().username

# ---------------------------
# Keyboard helpers
# ---------------------------
def help_markup():
    kb = [
        [InlineKeyboardButton("Getting Started", callback_data="help_getting_started")],
        [InlineKeyboardButton("Earning Slots", callback_data="help_earning")],
        [InlineKeyboardButton("Commands", callback_data="help_commands")],
        [InlineKeyboardButton("Contact Admin", callback_data="help_admin")]
    ]
    return InlineKeyboardMarkup(kb)

# ---------------------------
# Telegram command handlers
# ---------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Check referral parameter
    args = context.args or []
    ref_token = args[0] if args else None
    # ensure user in db
    async with data_lock:
        ensure_user_entry(user.id, user.username)
        # if referred by token and not self-referral
        if ref_token:
            ref_uid = data["referrals"].get(ref_token)
            if ref_uid and ref_uid != user.id:
                # increment invites for referrer
                ref_key = str(ref_uid)
                data["users"][ref_key]["invites"] = data["users"][ref_key].get("invites", 0) + 1
                # recompute limits
                new_limit = compute_limit_from_invites(data["users"][ref_key]["invites"])
                data["users"][ref_key]["limit"] = new_limit
                save_data(data)
                try:
                    await context.bot.send_message(
                        ref_uid,
                        f"🎉 Good news! You gained 1 invite. Total invites: {data['users'][ref_key]['invites']}. "
                        f"Your slot limit is now {new_limit}."
                    )
                except Exception as e:
                    logger.info("Could not DM referrer: %s", e)

    # Stylish welcome (Style 3)
    welcome = (
        "👋 Welcome to SmartLink Hub!\n\n"
        "📌 You can manage and rotate your links automatically every 30 minutes.\n"
        "Start with 5 link slots for FREE.\n\n"
        "📈 Unlock more slots by inviting friends:\n"
        "➡️ 20 Invites = 10 slots\n"
        "➡️ 40 Invites = 20 slots\n"
        "➡️ 60 Invites = 30 slots\n\n"
        "Use these commands:\n"
        "🧩 /addlinks <link1> <link2> ... — Add links (within your limit)\n"
        "🔗 /invite — Get your referral link to invite users\n"
        "📊 /status — View your stats\n"
        "❓ /help — Learn how to use the bot\n\n"
        "Let’s automate your link growth 💫"
    )
    await update.message.reply_text(welcome, reply_markup=help_markup())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Choose a topic:", reply_markup=help_markup())

async def callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_key = query.data
    if data_key == "help_getting_started":
        text = (
            "🚀 Getting Started:\n"
            "1) Use /invite to get your personal referral link.\n"
            "2) Share it — when people join via it, you earn invite credits.\n"
            "3) Use /addlinks to add up to your unlocked slots.\n"
            "4) Admin rotates links into the target chat automatically."
        )
    elif data_key == "help_earning":
        text = (
            "🏆 Earning Slots:\n"
            "• Start with 5 free slots.\n"
            "• 20 invites → 10 slots\n"
            "• 40 invites → 20 slots\n"
            "• 60 invites → 30 slots\n"
            "Use /status to check your current invites and limit."
        )
    elif data_key == "help_commands":
        text = (
            "📚 Commands:\n"
            "/start — Intro\n"
            "/invite — Your referral link\n"
            "/addlinks l1 l2 ... — Add links (within your limit)\n"
            "/removelink <index> — Remove your link\n"
            "/showlinks — See your added links\n"
            "/status — Your stats\n"
            "/leaderboard — Top inviters\n"
            "/help — This menu\n"
        )
    else:
        text = f"Need help? Contact admin: @{(await context.bot.get_me()).username}"

    await query.edit_message_text(text)

async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with data_lock:
        u = ensure_user_entry(user.id, user.username)
        token = u["token"]
    bot_username = (await context.bot.get_me()).username
    invite_link = f"https://t.me/{bot_username}?start={token}"
    await update.message.reply_text(
        f"🔗 Your referral link:\n{invite_link}\n\nShare this — each person who joins using it increases your invite count.",
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    async with data_lock:
        u = data["users"].get(uid)
        if not u:
            u = ensure_user_entry(user.id, user.username)
    text = (
        f"📊 Your Stats:\n"
        f"👤 Username: @{user.username if user.username else user.first_name}\n"
        f"🔢 Invites: {u['invites']}\n"
        f"🔗 Links added: {u['links_added']}\n"
        f"🎯 Slot limit: {u['limit']}\n"
        f"⏱ Per-user interval: {u['interval'] or 'Default'} minutes"
    )
    await update.message.reply_text(text)

async def cmd_addlinks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    if not args:
        return await update.message.reply_text("Usage: /addlinks <link1> <link2> ... (space-separated)")
    uid = str(user.id)
    async with data_lock:
        ensure_user_entry(user.id, user.username)
        user_entry = data["users"][uid]
        allowed = user_entry["limit"] - user_entry["links_added"]
        if allowed <= 0:
            return await update.message.reply_text(
                f"⚠️ You have reached your slot limit ({user_entry['limit']}). Invite more users to increase your limit."
            )
        to_add = args[:allowed]
        added = 0
        for l in to_add:
            link_obj = {
                "link": l,
                "owner_id": user.id,
                "owner_username": user.username or "",
                "added_at": datetime.utcnow().isoformat()
            }
            data["links"].append(link_obj)
            user_entry["links_added"] += 1
            added += 1
        save_data(data)
    await update.message.reply_text(f"✅ Added {added} link(s). Total your links in pool: {user_entry['links_added']}")

async def cmd_showlinks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    async with data_lock:
        user_links = [l for l in data["links"] if str(l["owner_id"]) == uid]
    if not user_links:
        return await update.message.reply_text("You have no links in the pool.")
    text = "\n".join([f"{i+1}. {l['link']}" for i, l in enumerate(user_links)])
    await update.message.reply_text(f"🔗 Your Links:\n{text}")

async def cmd_removelink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    if not args:
        return await update.message.reply_text("Usage: /removelink <index_from_showlinks>")
    try:
        idx = int(args[0]) - 1
    except:
        return await update.message.reply_text("Provide a valid index number.")
    uid = str(user.id)
    async with data_lock:
        user_links = [l for l in data["links"] if str(l["owner_id"]) == uid]
        if idx < 0 or idx >= len(user_links):
            return await update.message.reply_text("Invalid index.")
        target = user_links[idx]
        # remove first matching link instance
        for i, l in enumerate(data["links"]):
            if l is target:
                data["links"].pop(i)
                data["users"][uid]["links_added"] -= 1
                save_data(data)
                return await update.message.reply_text("✅ Link removed.")
    await update.message.reply_text("Could not remove link.")

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with data_lock:
        users = data["users"]
        ranked = sorted(users.items(), key=lambda kv: kv[1].get("invites", 0), reverse=True)[:10]
    if not ranked:
        return await update.message.reply_text("No invites yet.")
    text = "🏆 Top Inviters:\n"
    for i, (uid, u) in enumerate(ranked, start=1):
        uname = u.get("username") or uid
        text += f"{i}. @{uname} — {u.get('invites',0)} invites\n"
    await update.message.reply_text(text)

# ---------------------------
# Admin commands
# ---------------------------
async def admin_setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        return await update.message.reply_text("Usage: /setchat <@username or chat_id>")
    chat = context.args[0]
    async with data_lock:
        data["settings"]["chat_id"] = chat
        save_data(data)
    await update.message.reply_text(f"✅ Target chat set to {chat}")

async def admin_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        return await update.message.reply_text("Usage: /setinterval <minutes>")
    try:
        minutes = int(context.args[0])
    except:
        return await update.message.reply_text("Provide integer minutes.")
    async with data_lock:
        data["settings"]["interval"] = minutes
        save_data(data)
    await update.message.reply_text(f"✅ Interval set to {minutes} minutes")

async def admin_startrotation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    async with data_lock:
        if not data["settings"]["chat_id"]:
            return await update.message.reply_text("Set target chat first using /setchat")
        if data["settings"]["running"]:
            return await update.message.reply_text("Rotation already running.")
        data["settings"]["running"] = True
        save_data(data)
    await update.message.reply_text("✅ Rotation started (admin)")

async def admin_stoprotation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    async with data_lock:
        data["settings"]["running"] = False
        save_data(data)
    await update.message.reply_text("⏹ Rotation stopped (admin)")

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = " ".join(context.args) or None
    if not text:
        return await update.message.reply_text("Usage: /broadcast <message>")
    async with data_lock:
        users = list(data["users"].keys())
    sent = 0
    for uid in users:
        try:
            await context.bot.send_message(int(uid), f"📣 Broadcast from admin:\n\n{text}")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} users (attempted).")

# ---------------------------
# Rotation task (background)
# ---------------------------
async def rotation_worker(app):
    bot = app.bot
    while True:
        await asyncio.sleep(5)  # short startup delay
        while True:
            async with data_lock:
                running = data["settings"].get("running", False)
                chat_id = data["settings"].get("chat_id")
                interval = data["settings"].get("interval", DEFAULT_INTERVAL_MIN)
                if not running or not chat_id:
                    break
                links: List[Dict[str,Any]] = data.get("links", [])
                idx = data["settings"].get("rotation_index", 0)
            # rotation loop
            if not links:
                # notify admin, keep last_link
                last_link = data["settings"].get("last_link")
                try:
                    await bot.send_message(ADMIN_ID, "⚠️ All links exhausted in SmartLink Hub. Add new links to resume rotation.")
                    if last_link and chat_id:
                        # post last link again (best-effort) so it remains visible
                        await bot.send_message(chat_id, f"🔗 Current link (last): {last_link}\n(Waiting for new links.)")
                except Exception as e:
                    logger.info("Admin notify failed: %s", e)
                # stop rotation in memory
                async with data_lock:
                    data["settings"]["running"] = False
                    save_data(data)
                break

            # ensure index valid
            async with data_lock:
                if idx >= len(data["links"]):
                    idx = 0
                link_obj = data["links"].pop(0)  # pop front to rotate FIFO
                data["settings"]["last_link"] = link_obj["link"]
                data["settings"]["rotation_index"] = 0
                save_data(data)

            # send to chat
            try:
                await bot.send_message(chat_id, f"🔁 New invite link:\n{link_obj['link']}")
            except Exception as e:
                logger.exception("Failed to send link to chat: %s", e)

            # notify owner that one of their links was used; if owner has no more links then notify them
            owner_id = link_obj.get("owner_id")
            if owner_id:
                try:
                    # count remaining links owner has
                    async with data_lock:
                        owner_links = [l for l in data["links"] if l.get("owner_id") == owner_id]
                        if not owner_links:
                            await bot.send_message(owner_id, "ℹ️ All your links currently used in rotation. Add new links or invite more users to unlock more slots.")
                except Exception as e:
                    logger.info("Could not DM owner: %s", e)

            # wait interval
            await asyncio.sleep(interval * 60)

# ---------------------------
# Auto-backup task
# ---------------------------
async def backup_worker(app):
    bot = app.bot
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)
        # create a simple backup copy and DM admin
        try:
            ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            backup_name = f"data-backup-{ts}.json"
            async with data_lock:
                with open(DATA_FILE, "r") as f:
                    raw = f.read()
                with open(backup_name, "w") as bf:
                    bf.write(raw)
            # send small notification to admin (file sending sometimes blocked, so send summary)
            await bot.send_message(ADMIN_ID, f"🔐 Backup created: {backup_name} (stored on server). If you need the file, request /getbackup.")
        except Exception as e:
            logger.exception("Backup failed: %s", e)

async def admin_getbackup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    # send latest backup file content
    try:
        with open(DATA_FILE, "rb") as f:
            await context.bot.send_document(ADMIN_ID, f)
    except Exception as e:
        await update.message.reply_text("Failed to send backup file.")

# ---------------------------
# Webhook endpoint + startup/shutdown
# ---------------------------
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

# Register handlers
telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CommandHandler("help", cmd_help))
telegram_app.add_handler(CallbackQueryHandler(callback_help))
telegram_app.add_handler(CommandHandler("invite", cmd_invite))
telegram_app.add_handler(CommandHandler("status", cmd_status))
telegram_app.add_handler(CommandHandler("addlinks", cmd_addlinks))
telegram_app.add_handler(CommandHandler("showlinks", cmd_showlinks))
telegram_app.add_handler(CommandHandler("removelink", cmd_removelink))
telegram_app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))

# admin handlers
telegram_app.add_handler(CommandHandler("setchat", admin_setchat))
telegram_app.add_handler(CommandHandler("setinterval", admin_setinterval))
telegram_app.add_handler(CommandHandler("startrotation", admin_startrotation))
telegram_app.add_handler(CommandHandler("stoprotation", admin_stoprotation))
telegram_app.add_handler(CommandHandler("broadcast", admin_broadcast))
telegram_app.add_handler(CommandHandler("getbackup", admin_getbackup))

@telegram_app.post(WEBHOOK_PATH)  # type: ignore
async def telegram_webhoo
