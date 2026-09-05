import os
import io
import uuid
import asyncio
import math
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import certifi
import qrcode
from dotenv import load_dotenv
from pymongo import MongoClient
from bson import ObjectId
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except ValueError:
    ADMIN_ID = 0

ENV_UPI_ID = os.getenv("UPI_ID", "").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")
if not MONGO_URI:
    raise ValueError("MONGO_URI missing")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID missing")

mongo = MongoClient(
    MONGO_URI,
    tls=True,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=30000,
    connectTimeoutMS=20000,
)
mongo.admin.command("ping")

db = mongo[os.getenv("MONGO_DB", "telegram_subscription_bot")]
users = db["users"]
plans = db["plans"]
servers = db["servers"]
payments = db["payments"]
settings = db["settings"]
temp_messages = db["temp_messages"]

users.create_index("user_id", unique=True)
payments.create_index("payment_id", unique=True)
payments.create_index([("user_id", 1), ("status", 1)])


def now_utc():
    return datetime.now(timezone.utc)


def dt_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


settings.update_one(
    {"_id": "config"},
    {"$setOnInsert": {"upi_id": ENV_UPI_ID, "created_at": now_utc()}},
    upsert=True,
)


def get_upi_id():
    doc = settings.find_one({"_id": "config"})
    return (doc or {}).get("upi_id", "") or ENV_UPI_ID


def set_upi_id(value):
    settings.update_one({"_id": "config"}, {"$set": {"upi_id": value}}, upsert=True)


def seed_plans():
    if plans.count_documents({}) == 0:
        plans.insert_many([
            {"name": "15 Days", "price": 29.0, "days": 15, "active": True, "created_at": now_utc()},
            {"name": "1 Month", "price": 99.0, "days": 30, "active": True, "created_at": now_utc()},
        ])


seed_plans()


def create_user(tg_user):
    users.update_one(
        {"user_id": tg_user.id},
        {
            "$setOnInsert": {
                "user_id": tg_user.id,
                "subscription_expiry": None,
                "created_at": now_utc(),
            },
            "$set": {
                "first_name": tg_user.first_name or "",
                "username": tg_user.username or "",
            },
        },
        upsert=True,
    )


def get_user(user_id):
    return users.find_one({"user_id": user_id})


def get_subscription(user_id):
    user = get_user(user_id)
    if not user:
        return None
    expiry = dt_utc(user.get("subscription_expiry"))
    return expiry


def has_active_subscription(user_id):
    expiry = get_subscription(user_id)
    return bool(expiry and expiry > now_utc())


def remaining_text(expiry):
    expiry = dt_utc(expiry)
    if not expiry or expiry <= now_utc():
        return "Expired"
    seconds = (expiry - now_utc()).total_seconds()
    days = math.ceil(seconds / 86400)
    return f"{days} day(s)"


async def safe_delete_message(bot, chat_id, message_id):
    if not chat_id or not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def delete_payment_qr(bot, payment):
    await safe_delete_message(
        bot,
        payment.get("qr_chat_id"),
        payment.get("qr_message_id"),
    )


def main_keyboard(user_id=None):
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔐 OTP SERVER"), KeyboardButton("💎 SUBSCRIPTION")],
            [KeyboardButton("🖥 SERVER"), KeyboardButton("👤 ACCOUNT")],
            [KeyboardButton("👥 REFER & EARN"), KeyboardButton("🆘 SUPPORT")],
        ] + ([[KeyboardButton("⚙️ ADMIN PANEL")]] if user_id is not None and is_admin(user_id) else []),
        resize_keyboard=True,
    )


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥 SERVERS", callback_data="admin_servers"),
         InlineKeyboardButton("💳 PAYMENTS", callback_data="admin_payments")],
        [InlineKeyboardButton("💎 PLANS", callback_data="admin_plans"),
         InlineKeyboardButton("💰 UPI", callback_data="admin_upi")],
        [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast"),
         InlineKeyboardButton("👥 USERS", callback_data="admin_users")],
    ])


def is_admin(user_id):
    return user_id == ADMIN_ID


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        return True
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, update.effective_user.id)
        if member.status in ("member", "administrator", "creator"):
            return True
    except Exception:
        pass

    channel_link = REQUIRED_CHANNEL
    if REQUIRED_CHANNEL.startswith("@"): 
        channel_link = "https://t.me/" + REQUIRED_CHANNEL[1:]
    elif REQUIRED_CHANNEL.startswith("https://"):
        channel_link = REQUIRED_CHANNEL

    keyboard = []
    if channel_link.startswith("http"):
        keyboard.append([InlineKeyboardButton("📢 JOIN CHANNEL", url=channel_link)])
    keyboard.append([InlineKeyboardButton("✅ VERIFY", callback_data="verify_join")])

    text = "⚠️ Pehle required channel join karo, phir VERIFY dabao."
    if update.callback_query:
        await update.callback_query.answer("Pehle channel join karo.", show_alert=True)
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return False


def apply_referral(user_id, referrer_id):
    """Save a valid referral only once. Returns True when saved."""
    if not referrer_id or referrer_id == user_id or is_admin(user_id):
        return False
    referrer = users.find_one({"user_id": referrer_id})
    if not referrer:
        return False
    result = users.update_one(
        {"user_id": user_id, "referred_by": {"$exists": False}},
        {"$set": {"referred_by": referrer_id, "referred_at": now_utc()}}
    )
    return result.modified_count == 1


def get_referral_stats(user_id):
    referred = users.count_documents({"referred_by": user_id})
    rewarded = users.count_documents({"referred_by": user_id, "referral_rewarded": True})
    return referred, rewarded


def reward_referrer(referred_user_id, days=7):
    """Give 7 days to the referrer only once, after referred user's first approved purchase."""
    referred_user = get_user(referred_user_id)
    if not referred_user:
        return None

    referrer_id = referred_user.get("referred_by")
    if not referrer_id or referred_user.get("referral_rewarded"):
        return None

    # Atomic claim prevents duplicate rewards if approval is triggered twice.
    claimed = users.update_one(
        {"user_id": referred_user_id, "referral_rewarded": {"$ne": True}},
        {"$set": {"referral_rewarded": True, "referral_rewarded_at": now_utc()}}
    )
    if claimed.modified_count != 1:
        return None

    referrer = get_user(referrer_id)
    if not referrer:
        return None

    current = dt_utc(referrer.get("subscription_expiry"))
    base = current if current and current > now_utc() else now_utc()
    new_expiry = base + timedelta(days=days)

    users.update_one(
        {"user_id": referrer_id},
        {"$set": {"subscription_expiry": new_expiry}}
    )
    return referrer_id, new_expiry


async def referral_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_join(update, context):
        return
    user_id = update.effective_user.id
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    total, rewarded = get_referral_stats(user_id)
    await update.message.reply_text(
        "👥 REFER & EARN\n\n"
        "Apna referral link share karo.\n"
        "Jab tumhare referral se koi user join karke **koi bhi active plan purchase karta hai**, "
        "tumhe **7 days extra access** milega.\n\n"
        f"🔗 Your referral link:\n{link}\n\n"
        f"👤 Total referrals: {total}\n"
        f"🎁 Rewards received: {rewarded}\n\n"
        "⚠️ Reward sirf referred user ki first successful/approved purchase par milega.",
        parse_mode="Markdown"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_user(update.effective_user)

    # Referral deep-link: /start ref_<user_id>
    if context.args:
        payload = (context.args[0] or "").strip()
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload[4:])
                if apply_referral(update.effective_user.id, referrer_id):
                    await update.message.reply_text("🎉 Referral linked successfully!")
            except (ValueError, TypeError):
                pass

    if not await check_join(update, context):
        return
    await update.message.reply_text(
        "👋 Welcome!\n\nMenu se option select karo.",
        reply_markup=main_keyboard(update.effective_user.id),
    )


async def subscription_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_join(update, context):
        return
    active_plans = list(plans.find({"active": True}).sort("price", 1))
    if not active_plans:
        await update.message.reply_text("❌ Abhi koi active plan nahi hai.")
        return
    buttons = []
    for p in active_plans:
        buttons.append([InlineKeyboardButton(
            f"💎 {p['name']} — ₹{p['price']:.0f}",
            callback_data=f"buyplan_{p['_id']}"
        )])
    await update.message.reply_text("💎 Subscription Plans:", reply_markup=InlineKeyboardMarkup(buttons))


async def server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_join(update, context):
        return
    if not has_active_subscription(update.effective_user.id):
        await update.message.reply_text("🔒 Active subscription required to open SERVER.")
        return

    server_list = list(servers.find({}).sort("created_at", 1))
    if not server_list:
        await update.message.reply_text("❌ Abhi koi server available nahi hai.")
        return

    buttons = []
    for s in server_list:
        buttons.append([InlineKeyboardButton(
            f"🖥 {s['name']}",
            url=s["link"]
        )])

    msg = await update.message.reply_text(
        "🖥 Available Servers\n\n⚠️ This server list will automatically disappear after 1 hour.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    temp_messages.insert_one({
        "type": "server_list",
        "chat_id": update.effective_chat.id,
        "message_id": msg.message_id,
        "user_id": update.effective_user.id,
        "expires_at": now_utc() + timedelta(hours=1),
        "created_at": now_utc(),
    })


async def account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_join(update, context):
        return
    user = get_user(update.effective_user.id)
    expiry = dt_utc(user.get("subscription_expiry")) if user else None
    status = "🟢 Active" if expiry and expiry > now_utc() else "🔴 Expired"
    expiry_text = expiry.strftime("%d-%m-%Y %H:%M UTC") if expiry and expiry > now_utc() else "—"
    await update.message.reply_text(
        f"👤 ACCOUNT\n\n"
        f"🆔 User ID: {update.effective_user.id}\n"
        f"📛 Name: {update.effective_user.first_name or 'User'}\n"
        f"📌 Status: {status}\n"
        f"⏳ Remaining: {remaining_text(expiry)}\n"
        f"📅 Expiry: {expiry_text}"
    )


async def otp_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_join(update, context):
        return
    await update.message.reply_text(
        "🔐 OTP SERVER\n\nServer access / OTP service information yahan configure ki ja sakti hai."
    )


async def support_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_join(update, context):
        return
    context.user_data["support_waiting"] = True
    await update.message.reply_text("🆘 Apna message bhejo. Admin ko forward kar diya jayega.")


async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, plan):
    user_id = update.effective_user.id

    # Remove/expire any older QR waiting for UTR from this user.
    old_payments = list(payments.find({"user_id": user_id, "status": "waiting_utr"}))
    for old in old_payments:
        payments.update_one(
            {"_id": old["_id"], "status": "waiting_utr"},
            {"$set": {"status": "expired", "expired_at": now_utc()}},
        )
        await delete_payment_qr(context.bot, old)

    upi_id = get_upi_id()
    if not upi_id:
        await update.callback_query.message.reply_text("❌ Payment UPI abhi configured nahi hai.")
        return

    payment_id = uuid.uuid4().hex[:12].upper()
    amount = float(plan["price"])
    expires_at = now_utc() + timedelta(minutes=5)

    upi_params = {
        "pa": upi_id,
        "pn": "Subscription",
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": payment_id,
    }
    upi_link = "upi://pay?" + urlencode(upi_params)

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_link)
    qr.make(fit=True)
    image = qr.make_image()
    bio = io.BytesIO()
    image.save(bio, format="PNG")
    bio.seek(0)

    payment_doc = {
        "payment_id": payment_id,
        "user_id": user_id,
        "plan_id": plan["_id"],
        "plan_name": plan["name"],
        "days": int(plan["days"]),
        "amount": amount,
        "utr": None,
        "status": "waiting_utr",
        "expires_at": expires_at,
        "created_at": now_utc(),
        "qr_chat_id": update.effective_chat.id,
        "qr_message_id": None,
    }
    payments.insert_one(payment_doc)

    sent = await update.callback_query.message.reply_photo(
        photo=bio,
        caption=(
            f"💳 PAYMENT\n\n"
            f"Plan: {plan['name']}\n"
            f"Amount: ₹{amount:.2f}\n"
            f"UPI: {upi_id}\n"
            f"Payment Code: {payment_id}\n\n"
            "QR se payment karo aur UTR yahan send karo.\n"
            "⏱ QR 5 minutes ke baad automatically delete ho jayega.\n"
            "✅ UTR submit karte hi QR immediately delete ho jayega."
        ),
    )

    payments.update_one(
        {"payment_id": payment_id},
        {"$set": {"qr_message_id": sent.message_id}},
    )
    context.user_data["waiting_payment"] = payment_id
    await update.callback_query.answer("QR generated")


async def process_utr(update: Update, context: ContextTypes.DEFAULT_TYPE, text):
    payment_id = context.user_data.get("waiting_payment")
    payment = None
    if payment_id:
        payment = payments.find_one({"payment_id": payment_id, "user_id": update.effective_user.id})

    if not payment:
        payment = payments.find_one(
            {"user_id": update.effective_user.id, "status": "waiting_utr"},
            sort=[("created_at", -1)],
        )

    if not payment:
        return False

    if payment.get("status") != "waiting_utr":
        context.user_data.pop("waiting_payment", None)
        return False

    if dt_utc(payment.get("expires_at")) <= now_utc():
        payments.update_one(
            {"_id": payment["_id"], "status": "waiting_utr"},
            {"$set": {"status": "expired", "expired_at": now_utc()}},
        )
        await delete_payment_qr(context.bot, payment)
        context.user_data.pop("waiting_payment", None)
        await update.message.reply_text("⏰ Payment QR expire ho gaya. Naya payment create karo.")
        return True

    utr = text.strip()
    if len(utr) < 6 or len(utr) > 100:
        await update.message.reply_text("❌ Valid UTR/transaction number bhejo.")
        return True

    duplicate = payments.find_one({
        "utr": utr,
        "status": {"$in": ["pending", "approved"]},
        "_id": {"$ne": payment["_id"]},
    })
    if duplicate:
        await update.message.reply_text("❌ Ye UTR pehle hi use ho chuka hai.")
        return True

    result = payments.update_one(
        {"_id": payment["_id"], "status": "waiting_utr", "expires_at": {"$gt": now_utc()}},
        {"$set": {"utr": utr, "status": "pending", "submitted_at": now_utc()}},
    )
    if result.modified_count != 1:
        await update.message.reply_text("⏰ Payment expired or already processed.")
        context.user_data.pop("waiting_payment", None)
        return True

    await delete_payment_qr(context.bot, payment)
    context.user_data.pop("waiting_payment", None)

    await update.message.reply_text("✅ UTR submitted. Admin verification ke baad subscription activate hoga.")

    user = get_user(update.effective_user.id)
    name = user.get("first_name", "User") if user else "User"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{payment['payment_id']}"),
         InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{payment['payment_id']}")]
    ])
    await context.bot.send_message(
        ADMIN_ID,
        f"💳 NEW PAYMENT\n\n"
        f"User: {name}\n"
        f"User ID: {update.effective_user.id}\n"
        f"Plan: {payment['plan_name']}\n"
        f"Amount: ₹{payment['amount']:.2f}\n"
        f"Payment Code: {payment['payment_id']}\n"
        f"UTR: {utr}",
        reply_markup=kb,
    )
    return True


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🛠 ADMIN PANEL", reply_markup=admin_keyboard())


async def admin_servers(query, context):
    if not is_admin(query.from_user.id):
        return
    server_list = list(servers.find({}).sort("created_at", 1))
    text = "🖥 SERVER MANAGEMENT\n\n"
    buttons = [[InlineKeyboardButton("➕ ADD SERVER", callback_data="server_add")]]
    if not server_list:
        text += "No servers."
    else:
        for s in server_list:
            text += f"• {s['name']}\n{s['link']}\n\n"
            buttons.append([
                InlineKeyboardButton(f"🗑 DELETE {s['name'][:20]}", callback_data=f"server_delete_{s['_id']}")
            ])
    buttons.append([InlineKeyboardButton("⬅️ BACK", callback_data="admin_home")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_payments(query, context):
    if not is_admin(query.from_user.id):
        return
    pending = list(payments.find({"status": "pending"}).sort("created_at", -1).limit(20))
    text = "💳 PENDING PAYMENTS\n\n"
    buttons = []
    if not pending:
        text += "No pending payments."
    else:
        for p in pending:
            text += f"{p['payment_id']} | User {p['user_id']} | ₹{p['amount']:.0f} | UTR {p.get('utr','-')}\n"
            buttons.append([
                InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{p['payment_id']}"),
                InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{p['payment_id']}"),
            ])
    buttons.append([InlineKeyboardButton("⬅️ BACK", callback_data="admin_home")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_plans(query, context):
    if not is_admin(query.from_user.id):
        return
    all_plans = list(plans.find({}).sort("price", 1))
    text = "💎 PLAN MANAGEMENT\n\n"
    buttons = [[InlineKeyboardButton("➕ ADD PLAN", callback_data="plan_add")]]
    if not all_plans:
        text += "No plans."
    for p in all_plans:
        state = "ON" if p.get("active") else "OFF"
        text += f"• {p['name']} — ₹{p['price']:.2f} — {p['days']} days — {state}\n"
        pid = str(p["_id"])
        buttons.append([
            InlineKeyboardButton("✏️ EDIT", callback_data=f"plan_edit_{pid}"),
            InlineKeyboardButton(f"🔄 {state}", callback_data=f"plan_toggle_{pid}"),
            InlineKeyboardButton("🗑", callback_data=f"plan_delete_{pid}"),
        ])
    buttons.append([InlineKeyboardButton("⬅️ BACK", callback_data="admin_home")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_upi(query, context):
    if not is_admin(query.from_user.id):
        return
    upi = get_upi_id() or "Not set"
    await query.edit_message_text(
        f"💰 UPI SETTINGS\n\nCurrent UPI: {upi}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ EDIT UPI", callback_data="upi_edit")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="admin_home")],
        ])
    )


async def admin_users(query, context):
    if not is_admin(query.from_user.id):
        return
    total = users.count_documents({})
    active = 0
    now = now_utc()
    for u in users.find({}, {"subscription_expiry": 1}):
        expiry = dt_utc(u.get("subscription_expiry"))
        if expiry and expiry > now:
            active += 1
    await query.edit_message_text(
        f"👥 USERS\n\nTotal users: {total}\nActive subscriptions: {active}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ BACK", callback_data="admin_home")]])
    )


async def approve_payment(query, context, payment_id):
    if not is_admin(query.from_user.id):
        return
    payment = payments.find_one({"payment_id": payment_id})
    if not payment or payment.get("status") != "pending":
        await query.answer("Payment already processed / not found", show_alert=True)
        return

    current = get_subscription(payment["user_id"])
    base = current if current and current > now_utc() else now_utc()
    expiry = base + timedelta(days=int(payment["days"]))

    payments.update_one(
        {"_id": payment["_id"], "status": "pending"},
        {"$set": {"status": "approved", "approved_at": now_utc(), "approved_by": query.from_user.id}},
    )
    users.update_one(
        {"user_id": payment["user_id"]},
        {"$set": {"subscription_expiry": expiry}},
        upsert=True,
    )

    # Referral reward: referred user's first approved purchase gives referrer 7 days.
    referral_reward = reward_referrer(payment["user_id"], days=7)

    await query.answer("Approved")
    try:
        await context.bot.send_message(
            payment["user_id"],
            f"✅ Payment approved!\n\nPlan: {payment['plan_name']}\n"
            f"Subscription expiry: {expiry.strftime('%d-%m-%Y %H:%M UTC')}"
        )
    except Exception:
        pass

    if referral_reward:
        referrer_id, referrer_expiry = referral_reward
        try:
            await context.bot.send_message(
                referrer_id,
                "🎁 REFERRAL BONUS!\n\n"
                "Tumhare referral ne subscription purchase kiya.\n"
                "✅ Tumhe 7 days extra access mil gaya!\n\n"
                f"📅 New expiry: {referrer_expiry.strftime('%d-%m-%Y %H:%M UTC')}"
            )
        except Exception:
            pass
    try:
        await query.edit_message_text(
            f"✅ APPROVED\nPayment: {payment_id}\nUser: {payment['user_id']}\nUTR: {payment.get('utr','-')}"
        )
    except Exception:
        pass


async def reject_payment(query, context, payment_id):
    if not is_admin(query.from_user.id):
        return
    payment = payments.find_one({"payment_id": payment_id})
    if not payment or payment.get("status") != "pending":
        await query.answer("Payment already processed / not found", show_alert=True)
        return
    payments.update_one(
        {"_id": payment["_id"], "status": "pending"},
        {"$set": {"status": "rejected", "rejected_at": now_utc(), "rejected_by": query.from_user.id}},
    )
    await query.answer("Rejected")
    try:
        await context.bot.send_message(payment["user_id"], "❌ Payment rejected by admin. Agar payment kiya hai to support se contact karo.")
    except Exception:
        pass
    try:
        await query.edit_message_text(
            f"❌ REJECTED\nPayment: {payment_id}\nUser: {payment['user_id']}\nUTR: {payment.get('utr','-')}"
        )
    except Exception:
        pass


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data == "verify_join":
        if await check_join(update, context):
            await query.answer("Verified ✅")
            await query.message.reply_text("✅ Verified! /start dabao.", reply_markup=main_keyboard(query.from_user.id))
        return

    if data.startswith("buyplan_"):
        if not await check_join(update, context):
            return
        try:
            plan = plans.find_one({"_id": ObjectId(data.split("_", 1)[1]), "active": True})
        except Exception:
            plan = None
        if not plan:
            await query.answer("Plan unavailable", show_alert=True)
            return
        await query.answer()
        await create_payment(update, context, plan)
        return

    if data.startswith("approve_"):
        await approve_payment(query, context, data.split("_", 1)[1])
        return
    if data.startswith("reject_"):
        await reject_payment(query, context, data.split("_", 1)[1])
        return

    if not is_admin(query.from_user.id):
        await query.answer("Not allowed", show_alert=True)
        return

    if data == "admin_home":
        await query.answer()
        await query.edit_message_text("🛠 ADMIN PANEL", reply_markup=admin_keyboard())
    elif data == "admin_servers":
        await query.answer()
        await admin_servers(query, context)
    elif data == "admin_payments":
        await query.answer()
        await admin_payments(query, context)
    elif data == "admin_plans":
        await query.answer()
        await admin_plans(query, context)
    elif data == "admin_upi":
        await query.answer()
        await admin_upi(query, context)
    elif data == "admin_users":
        await query.answer()
        await admin_users(query, context)
    elif data == "admin_broadcast":
        context.user_data["admin_state"] = "broadcast"
        await query.answer()
        await query.message.reply_text("📢 Broadcast text bhejo.")
    elif data == "server_add":
        context.user_data["admin_state"] = "server_add"
        await query.answer()
        await query.message.reply_text("🖥 Server add format:\nName | https://example.com")
    elif data.startswith("server_delete_"):
        sid = data.split("_", 2)[2]
        try:
            servers.delete_one({"_id": ObjectId(sid)})
        except Exception:
            pass
        await query.answer("Deleted")
        await admin_servers(query, context)
    elif data == "plan_add":
        context.user_data["admin_state"] = "plan_add"
        await query.answer()
        await query.message.reply_text("💎 Add plan format:\nName | Price | Days\nExample: 15 Days | 29 | 15")
    elif data.startswith("plan_edit_"):
        pid = data.split("_", 2)[2]
        try:
            plan = plans.find_one({"_id": ObjectId(pid)})
        except Exception:
            plan = None
        if not plan:
            await query.answer("Plan not found", show_alert=True)
            return
        context.user_data["admin_state"] = "plan_edit"
        context.user_data["edit_plan_id"] = pid
        await query.answer()
        await query.message.reply_text(
            f"✏️ Edit plan:\nCurrent: {plan['name']} | {plan['price']} | {plan['days']}\n\n"
            "New format: Name | Price | Days"
        )
    elif data.startswith("plan_toggle_"):
        pid = data.split("_", 2)[2]
        try:
            plan = plans.find_one({"_id": ObjectId(pid)})
            if plan:
                plans.update_one({"_id": plan["_id"]}, {"$set": {"active": not bool(plan.get("active"))}})
        except Exception:
            pass
        await query.answer("Updated")
        await admin_plans(query, context)
    elif data.startswith("plan_delete_"):
        pid = data.split("_", 2)[2]
        try:
            plans.delete_one({"_id": ObjectId(pid)})
        except Exception:
            pass
        await query.answer("Deleted")
        await admin_plans(query, context)
    elif data == "upi_edit":
        context.user_data["admin_state"] = "upi_edit"
        await query.answer()
        await query.message.reply_text("💰 New UPI ID bhejo. Example: name@upi")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_user(update.effective_user)
    text = (update.message.text or "").strip()

    # Admin workflow states first.
    if is_admin(update.effective_user.id):
        state = context.user_data.get("admin_state")
        if state == "server_add":
            parts = [x.strip() for x in text.split("|", 1)]
            if len(parts) != 2 or not parts[0] or not parts[1].startswith(("http://", "https://")):
                await update.message.reply_text("❌ Format: Name | https://example.com")
                return
            servers.insert_one({"name": parts[0], "link": parts[1], "created_at": now_utc()})
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("✅ Server added.")
            return

        if state in ("plan_add", "plan_edit"):
            parts = [x.strip() for x in text.split("|")]
            if len(parts) != 3:
                await update.message.reply_text("❌ Format: Name | Price | Days")
                return
            try:
                name = parts[0]
                price = float(parts[1])
                days = int(parts[2])
                if not name or price <= 0 or days <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Price aur Days valid number hone chahiye.")
                return

            if state == "plan_add":
                plans.insert_one({"name": name, "price": price, "days": days, "active": True, "created_at": now_utc()})
                await update.message.reply_text("✅ Plan added.")
            else:
                pid = context.user_data.get("edit_plan_id")
                try:
                    plans.update_one(
                        {"_id": ObjectId(pid)},
                        {"$set": {"name": name, "price": price, "days": days}}
                    )
                    await update.message.reply_text("✅ Plan updated.")
                except Exception:
                    await update.message.reply_text("❌ Plan update failed.")
            context.user_data.pop("admin_state", None)
            context.user_data.pop("edit_plan_id", None)
            return

        if state == "upi_edit":
            if "@" not in text or len(text) < 5:
                await update.message.reply_text("❌ Valid UPI ID bhejo, example: name@upi")
                return
            set_upi_id(text)
            context.user_data.pop("admin_state", None)
            await update.message.reply_text(f"✅ UPI updated: {text}")
            return

        if state == "broadcast":
            all_users = list(users.find({}, {"user_id": 1}))
            sent = 0
            failed = 0
            for u in all_users:
                try:
                    await context.bot.send_message(u["user_id"], f"📢 BROADCAST\n\n{text}")
                    sent += 1
                except Exception:
                    failed += 1
                await asyncio.sleep(0.03)
            context.user_data.pop("admin_state", None)
            await update.message.reply_text(f"✅ Broadcast complete.\nSent: {sent}\nFailed: {failed}")
            return

    # Payment UTR.
    if await process_utr(update, context, text):
        return

    if not await check_join(update, context):
        return

    if text == "⚙️ ADMIN PANEL" and is_admin(update.effective_user.id):
        await admin_panel(update, context)
    elif text == "💎 SUBSCRIPTION":
        await subscription_menu(update, context)
    elif text == "🖥 SERVER":
        await server_menu(update, context)
    elif text == "👤 ACCOUNT":
        await account_menu(update, context)
    elif text == "🔐 OTP SERVER":
        await otp_server(update, context)
    elif text == "👥 REFER & EARN":
        await referral_menu(update, context)
    elif text == "🆘 SUPPORT":
        await support_menu(update, context)
    elif text == "⚙️ ADMIN PANEL" and is_admin(update.effective_user.id):
        await admin_panel(update, context)
    elif text == "/admin" and is_admin(update.effective_user.id):
        await admin_panel(update, context)
    elif context.user_data.get("support_waiting"):
        context.user_data.pop("support_waiting", None)
        await context.bot.send_message(
            ADMIN_ID,
            f"🆘 SUPPORT MESSAGE\n\nUser ID: {update.effective_user.id}\n"
            f"Name: {update.effective_user.first_name or 'User'}\n\n{text}",
        )
        await update.message.reply_text("✅ Message admin ko bhej diya gaya.")
    else:
        await update.message.reply_text("Menu se option select karo.", reply_markup=main_keyboard(update.effective_user.id))


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Reserved for optional future reply flow; normal admin text is handled above.
    return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("BOT ERROR:")
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)


async def cleanup_loop(application: Application):
    while True:
        try:
            now = now_utc()

            expired = list(payments.find({
                "status": "waiting_utr",
                "expires_at": {"$lte": now},
            }).limit(100))
            for payment in expired:
                result = payments.update_one(
                    {"_id": payment["_id"], "status": "waiting_utr"},
                    {"$set": {"status": "expired", "expired_at": now}},
                )
                if result.modified_count:
                    await delete_payment_qr(application.bot, payment)

            old_server_messages = list(temp_messages.find({"expires_at": {"$lte": now}}).limit(100))
            for item in old_server_messages:
                await safe_delete_message(application.bot, item.get("chat_id"), item.get("message_id"))
                temp_messages.delete_one({"_id": item["_id"]})

        except Exception as exc:
            print("Cleanup error:", exc)
        await asyncio.sleep(30)


async def post_init(application: Application):
    application.create_task(cleanup_loop(application))


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    print("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
