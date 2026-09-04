import os
import io
import uuid
import asyncio
from datetime import datetime, timedelta

import qrcode
from dotenv import load_dotenv
from pymongo import MongoClient
from bson import ObjectId

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
UPI_ID = os.getenv("UPI_ID")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")


if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

if not MONGO_URI:
    raise ValueError("MONGO_URI missing")


# ============================================================
# DATABASE
# ============================================================

mongo = MongoClient(MONGO_URI)

db = mongo["server_subscription_bot"]

users = db["users"]
plans = db["plans"]
servers = db["servers"]
payments = db["payments"]


# ============================================================
# DEFAULT PLANS
# ============================================================

def setup_default_plans():

    if plans.count_documents({}) == 0:

        plans.insert_many([
            {
                "name": "15 Days",
                "price": 29,
                "days": 15,
                "active": True
            },
            {
                "name": "1 Month",
                "price": 99,
                "days": 30,
                "active": True
            }
        ])


setup_default_plans()


# ============================================================
# KEYBOARD
# ============================================================

def main_menu(user_id):

    keyboard = [

        [
            KeyboardButton("🔐 OTP SERVER"),
            KeyboardButton("💎 SUBSCRIPTION")
        ],

        [
            KeyboardButton("🖥 SERVER"),
            KeyboardButton("👤 ACCOUNT")
        ],

        [
            KeyboardButton("💬 SUPPORT")
        ]

    ]

    if user_id == ADMIN_ID:

        keyboard.append([
            KeyboardButton("🛠 ADMIN PANEL")
        ])

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )


# ============================================================
# DATABASE USER
# ============================================================

def create_user(tg_user):

    users.update_one(

        {
            "user_id": tg_user.id
        },

        {
            "$setOnInsert": {

                "user_id": tg_user.id,
                "first_name": tg_user.first_name,
                "username": tg_user.username,
                "subscription_expiry": None,
                "created_at": datetime.utcnow()

            },

            "$set": {

                "first_name": tg_user.first_name,
                "username": tg_user.username

            }

        },

        upsert=True
    )


# ============================================================
# CHANNEL CHECK
# ============================================================

async def is_joined(user_id, context):

    try:

        member = await context.bot.get_chat_member(
            REQUIRED_CHANNEL,
            user_id
        )

        return member.status in [

            "member",
            "administrator",
            "creator"
        ]

    except Exception as error:

        print("Channel check:", error)

        return False


async def send_join_message(message):

    channel_username = REQUIRED_CHANNEL.replace("@", "")

    keyboard = InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📢 JOIN CHANNEL",
                url=f"https://t.me/{channel_username}"
            )
        ],

        [
            InlineKeyboardButton(
                "✅ VERIFY",
                callback_data="verify_join"
            )
        ]

    ])

    await message.reply_text(

        "🔒 CHANNEL JOIN REQUIRED\n\n"
        "Please join our channel before using the bot.",

        reply_markup=keyboard
    )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    create_user(user)

    joined = await is_joined(
        user.id,
        context
    )

    if not joined:

        await send_join_message(
            update.message
        )

        return

    await update.message.reply_text(

        "👋 Welcome!\n\n"
        "Choose an option below.",

        reply_markup=main_menu(
            user.id
        )
    )


# ============================================================
# VERIFY JOIN
# ============================================================

async def verify_join(update, context):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    joined = await is_joined(
        user_id,
        context
    )

    if joined:

        await query.message.reply_text(

            "✅ Verification successful!",

            reply_markup=main_menu(
                user_id
            )
        )

    else:

        await query.answer(

            "❌ Join channel first!",

            show_alert=True
        )


# ============================================================
# SUBSCRIPTION CHECK
# ============================================================

def get_subscription(user_id):

    user = users.find_one(
        {
            "user_id": user_id
        }
    )

    if not user:

        return False, None

    expiry = user.get(
        "subscription_expiry"
    )

    if not expiry:

        return False, None

    if expiry <= datetime.utcnow():

        return False, expiry

    return True, expiry


# ============================================================
# SUBSCRIPTION MENU
# ============================================================

async def subscription_menu(update, context):

    all_plans = list(
        plans.find(
            {
                "active": True
            }
        )
    )

    keyboard = []

    for plan in all_plans:

        keyboard.append([

            InlineKeyboardButton(

                f"💎 ₹{plan['price']} | {plan['name']}",

                callback_data=f"buyplan_{plan['_id']}"
            )

        ])

    await update.message.reply_text(

        "💎 SUBSCRIPTION PLANS\n\n"
        "Choose a plan:",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# ============================================================
# CREATE QR PAYMENT
# ============================================================

async def create_payment(update, context):

    query = update.callback_query

    await query.answer()

    plan_id = query.data.replace(
        "buyplan_",
        ""
    )

    try:

        plan = plans.find_one(

            {
                "_id": ObjectId(plan_id)
            }
        )

    except Exception:

        plan = None


    if not plan:

        await query.message.reply_text(
            "❌ Plan not found."
        )

        return


    payment_id = str(
        uuid.uuid4()
    )[:10].upper()

    amount = plan["price"]

    upi_link = (

        f"upi://pay?"

        f"pa={UPI_ID}"

        f"&pn=SERVER_BOT"

        f"&am={amount}"

        f"&cu=INR"

        f"&tn={payment_id}"

    )


    qr = qrcode.make(
        upi_link
    )

    qr_file = io.BytesIO()

    qr.save(
        qr_file,
        format="PNG"
    )

    qr_file.seek(0)


    expires_at = (

        datetime.utcnow()
        + timedelta(minutes=5)

    )


    payments.insert_one({

        "payment_id": payment_id,

        "user_id": query.from_user.id,

        "plan_name": plan["name"],

        "amount": amount,

        "days": plan["days"],

        "utr": None,

        "status": "waiting_utr",

        "expires_at": expires_at,

        "created_at": datetime.utcnow()

    })


    context.user_data[
        "waiting_payment"
    ] = payment_id


    message = await query.message.reply_photo(

        photo=qr_file,

        caption=(

            "💳 PAYMENT\n\n"

            f"💎 Plan: {plan['name']}\n"

            f"💰 Amount: ₹{amount}\n"

            f"🆔 Code: {payment_id}\n\n"

            "⚠️ Complete payment within 5 minutes.\n"

            "⌛ QR will automatically expire after 5 minutes.\n\n"

            "After payment send your UTR number."

        )
    )


    asyncio.create_task(

        expire_payment(

            context,

            payment_id,

            message.chat_id,

            message.message_id
        )

    )


# ============================================================
# EXPIRE PAYMENT AFTER 5 MINUTES
# ============================================================

async def expire_payment(

    context,

    payment_id,

    chat_id,

    message_id

):

    await asyncio.sleep(
        300
    )


    payment = payments.find_one({

        "payment_id": payment_id

    })


    if not payment:

        return


    if payment["status"] == "waiting_utr":

        payments.update_one(

            {
                "payment_id": payment_id
            },

            {
                "$set": {

                    "status": "expired"

                }
            }
        )


        try:

            await context.bot.delete_message(

                chat_id=chat_id,

                message_id=message_id
            )

        except Exception:

            pass


        await context.bot.send_message(

            chat_id,

            "⌛ Payment QR expired after 5 minutes."

        )


# ============================================================
# UTR HANDLER
# ============================================================

async def process_utr(update, context):

    payment_id = context.user_data.get(
        "waiting_payment"
    )

    if not payment_id:

        return False


    payment = payments.find_one({

        "payment_id": payment_id

    })


    if not payment:

        return False


    if payment["status"] == "expired":

        context.user_data.pop(
            "waiting_payment",
            None
        )

        await update.message.reply_text(
            "❌ Payment request expired."
        )

        return True


    utr = update.message.text.strip()


    if len(utr) < 6:

        await update.message.reply_text(
            "❌ Please enter a valid UTR."
        )

        return True


    payments.update_one(

        {
            "payment_id": payment_id
        },

        {
            "$set": {

                "utr": utr,

                "status": "pending"

            }
        }
    )


    context.user_data.pop(
        "waiting_payment",
        None
    )


    await update.message.reply_text(

        "✅ UTR submitted.\n\n"
        "Admin will verify your payment."

    )


    keyboard = InlineKeyboardMarkup([

        [

            InlineKeyboardButton(

                "✅ APPROVE",

                callback_data=f"approve_{payment_id}"
            ),

            InlineKeyboardButton(

                "❌ REJECT",

                callback_data=f"reject_{payment_id}"
            )

        ]

    ])


    await context.bot.send_message(

        ADMIN_ID,

        (

            "💳 NEW PAYMENT\n\n"

            f"👤 User: {update.effective_user.first_name}\n"

            f"🆔 ID: {update.effective_user.id}\n"

            f"💰 Amount: ₹{payment['amount']}\n"

            f"📦 Plan: {payment['plan_name']}\n"

            f"🔢 UTR: {utr}"

        ),

        reply_markup=keyboard
    )


    return True


# ============================================================
# PAYMENT APPROVE
# ============================================================

async def payment_action(update, context):

    query = update.callback_query

    if query.from_user.id != ADMIN_ID:

        await query.answer(
            "Unauthorized!",
            show_alert=True
        )

        return


    await query.answer()


    action, payment_id = query.data.split(
        "_",
        1
    )


    payment = payments.find_one({

        "payment_id": payment_id

    })


    if not payment:

        return


    if payment["status"] != "pending":

        await query.message.reply_text(
            "Payment already processed."
        )

        return


    if action == "approve":

        user = users.find_one({

            "user_id": payment["user_id"]

        })


        now = datetime.utcnow()


        old_expiry = user.get(
            "subscription_expiry"
        )


        if old_expiry and old_expiry > now:

            new_expiry = (

                old_expiry

                + timedelta(
                    days=payment["days"]
                )

            )

        else:

            new_expiry = (

                now

                + timedelta(
                    days=payment["days"]
                )

            )


        users.update_one(

            {

                "user_id": payment["user_id"]

            },

            {

                "$set": {

                    "subscription_expiry": new_expiry

                }

            }
        )


        payments.update_one(

            {

                "payment_id": payment_id

            },

            {

                "$set": {

                    "status": "approved"

                }

            }
        )


        await context.bot.send_message(

            payment["user_id"],

            (

                "🎉 SUBSCRIPTION ACTIVATED!\n\n"

                f"Plan: {payment['plan_name']}\n"

                f"Expires: "

                f"{new_expiry.strftime('%d-%m-%Y')}"

            )
        )


        await query.message.reply_text(
            "✅ Payment approved."
        )


    elif action == "reject":

        payments.update_one(

            {

                "payment_id": payment_id

            },

            {

                "$set": {

                    "status": "rejected"

                }

            }
        )


        await context.bot.send_message(

            payment["user_id"],

            "❌ Your payment was rejected."

        )


        await query.message.reply_text(
            "❌ Payment rejected."
        )


# ============================================================
# SERVER MENU
# ============================================================

async def server_menu(update, context):

    active, expiry = get_subscription(
        update.effective_user.id
    )


    if not active:

        await update.message.reply_text(

            "🔒 ACTIVE SUBSCRIPTION REQUIRED\n\n"

            "Please buy a subscription first."

        )

        return


    all_servers = list(

        servers.find(

            {

                "active": True

            }

        )

    )


    if not all_servers:

        await update.message.reply_text(
            "❌ No servers available."
        )

        return


    keyboard = []


    for server in all_servers:

        keyboard.append([

            InlineKeyboardButton(

                f"🔓 OPEN | {server['name']}",

                url=server["link"]
            )

        ])


    message = await update.message.reply_text(

        "🖥 AVAILABLE SERVERS\n\n"

        "⚠️ This server list will automatically "
        "delete after 1 hour.\n\n"

        "After deletion, open SERVER again "
        "to check your active subscription.",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


    asyncio.create_task(

        delete_server_list(

            context,

            message.chat_id,

            message.message_id
        )

    )


# ============================================================
# DELETE SERVER LIST AFTER 1 HOUR
# ============================================================

async def delete_server_list(

    context,

    chat_id,

    message_id

):

    await asyncio.sleep(
        3600
    )


    try:

        await context.bot.delete_message(

            chat_id=chat_id,

            message_id=message_id
        )


        await context.bot.send_message(

            chat_id,

            (

                "⌛ Server access list expired "
                "after 1 hour."

            )
        )

    except Exception as error:

        print(
            "Server delete error:",
            error
        )


# ============================================================
# ACCOUNT
# ============================================================

async def account(update, context):

    active, expiry = get_subscription(
        update.effective_user.id
    )


    if active:

        remaining = (
            expiry
            - datetime.utcnow()
        )


        text = (

            "👤 MY ACCOUNT\n\n"

            f"🆔 User ID: {update.effective_user.id}\n"

            "💎 Subscription: ACTIVE\n"

            f"📅 Days Remaining: "

            f"{remaining.days + 1}\n"

            f"⏳ Expiry: "

            f"{expiry.strftime('%d-%m-%Y %H:%M')}"

        )

    else:

        text = (

            "👤 MY ACCOUNT\n\n"

            f"🆔 User ID: {update.effective_user.id}\n"

            "💎 Subscription: INACTIVE"

        )


    await update.message.reply_text(
        text
    )


# ============================================================
# SUPPORT
# ============================================================

async def support_start(update, context):

    context.user_data[
        "support_mode"
    ] = True


    await update.message.reply_text(

        "💬 SUPPORT\n\n"

        "Send your message now."

    )


async def process_support(update, context):

    if not context.user_data.get(
        "support_mode"
    ):

        return False


    context.user_data.pop(
        "support_mode",
        None
    )


    user = update.effective_user


    keyboard = InlineKeyboardMarkup([

        [

            InlineKeyboardButton(

                "💬 REPLY",

                callback_data=f"supportreply_{user.id}"
            )

        ]

    ])


    await context.bot.send_message(

        ADMIN_ID,

        (

            "💬 SUPPORT MESSAGE\n\n"

            f"👤 {user.first_name}\n"

            f"🆔 {user.id}\n\n"

            f"{update.message.text}"

        ),

        reply_markup=keyboard
    )


    await update.message.reply_text(
        "✅ Message sent to support."
    )


    return True


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(update, context):

    if update.effective_user.id != ADMIN_ID:

        return


    keyboard = InlineKeyboardMarkup([

        [

            InlineKeyboardButton(

                "🖥 SERVERS",

                callback_data="admin_servers"
            ),

            InlineKeyboardButton(

                "💳 PAYMENTS",

                callback_data="admin_payments"
            )

        ],

        [

            InlineKeyboardButton(

                "📢 BROADCAST",

                callback_data="admin_broadcast"
            ),

            InlineKeyboardButton(

                "👥 USERS",

                callback_data="admin_users"
            )

        ]

    ])


    await update.message.reply_text(

        "🛠 ADMIN PANEL",

        reply_markup=keyboard
    )


# ============================================================
# ADMIN SERVER PANEL
# ============================================================

async def admin_servers(update, context):

    query = update.callback_query


    keyboard = [

        [

            InlineKeyboardButton(

                "➕ ADD SERVER",

                callback_data="add_server"
            )

        ]

    ]


    all_servers = list(
        servers.find({})
    )


    for server in all_servers:

        keyboard.append([

            InlineKeyboardButton(

                f"❌ DELETE {server['name']}",

                callback_data=f"delete_{server['_id']}"
            )

        ])


    await query.message.reply_text(

        "🖥 SERVER MANAGEMENT",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


async def add_server_start(update, context):

    context.user_data[
        "add_server"
    ] = "name"


    await update.callback_query.message.reply_text(

        "Send server name:"

    )


async def process_add_server(update, context):

    if update.effective_user.id != ADMIN_ID:

        return False


    state = context.user_data.get(
        "add_server"
    )


    if state == "name":

        context.user_data[
            "server_name"
        ] = update.message.text


        context.user_data[
            "add_server"
        ] = "link"


        await update.message.reply_text(
            "Send server link:"
        )


        return True


    if state == "link":

        name = context.user_data.get(
            "server_name"
        )


        servers.insert_one({

            "name": name,

            "link": update.message.text,

            "active": True

        })


        context.user_data.pop(
            "add_server",
            None
        )


        context.user_data.pop(
            "server_name",
            None
        )


        await update.message.reply_text(
            "✅ Server added."
        )


        return True


    return False


async def delete_server(update, context):

    query = update.callback_query


    server_id = query.data.replace(
        "delete_",
        ""
    )


    try:

        servers.delete_one({

            "_id": ObjectId(server_id)

        })


        await query.message.reply_text(
            "✅ Server deleted."
        )

    except Exception:

        await query.message.reply_text(
            "❌ Error."
        )


# ============================================================
# ADMIN PAYMENTS
# ============================================================

async def admin_payments(update, context):

    query = update.callback_query


    pending = list(

        payments.find({

            "status": "pending"

        })

    )


    if not pending:

        await query.message.reply_text(
            "No pending payments."
        )

        return


    for payment in pending:


        keyboard = InlineKeyboardMarkup([

            [

                InlineKeyboardButton(

                    "✅ APPROVE",

                    callback_data=f"approve_{payment['payment_id']}"
                ),

                InlineKeyboardButton(

                    "❌ REJECT",

                    callback_data=f"reject_{payment['payment_id']}"
                )

            ]

        ])


        await query.message.reply_text(

            (

                "💳 PENDING PAYMENT\n\n"

                f"User: {payment['user_id']}\n"

                f"Amount: ₹{payment['amount']}\n"

                f"UTR: {payment['utr']}"

            ),

            reply_markup=keyboard
        )


# ============================================================
# BROADCAST
# ============================================================

async def broadcast_start(update, context):

    context.user_data[
        "broadcast"
    ] = True


    await update.callback_query.message.reply_text(

        "Send broadcast message:"

    )


async def process_broadcast(update, context):

    if update.effective_user.id != ADMIN_ID:

        return False


    if not context.user_data.get(
        "broadcast"
    ):

        return False


    context.user_data.pop(
        "broadcast",
        None
    )


    count = 0


    for user in users.find({}):

        try:

            await context.bot.send_message(

                user["user_id"],

                update.message.text
            )


            count += 1

            await asyncio.sleep(
                0.05
            )

        except Exception:

            pass


    await update.message.reply_text(

        f"✅ Broadcast sent to {count} users."

    )


    return True


# ============================================================
# ADMIN USERS
# ============================================================

async def admin_users(update, context):

    query = update.callback_query


    total = users.count_documents({})


    active = users.count_documents({

        "subscription_expiry": {

            "$gt": datetime.utcnow()

        }

    })


    await query.message.reply_text(

        "👥 USER STATISTICS\n\n"

        f"Total Users: {total}\n"

        f"Active Subscribers: {active}"

    )


# ============================================================
# MAIN TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    text = update.message.text


    # Admin server creation

    if await process_add_server(
        update,
        context
    ):

        return


    # Broadcast

    if await process_broadcast(
        update,
        context
    ):

        return


    # UTR

    if await process_utr(
        update,
        context
    ):

        return


    # Support

    if await process_support(
        update,
        context
    ):

        return


    # Channel check

    joined = await is_joined(

        update.effective_user.id,

        context
    )


    if not joined:

        await send_join_message(
            update.message
        )

        return


    # Buttons

    if text == "🔐 OTP SERVER":

        await update.message.reply_text(

            "🔐 OTP SERVER\n\n"
            "Authorized/private server access only."

        )


    elif text == "💎 SUBSCRIPTION":

        await subscription_menu(
            update,
            context
        )


    elif text == "🖥 SERVER":

        await server_menu(
            update,
            context
        )


    elif text == "👤 ACCOUNT":

        await account(
            update,
            context
        )


    elif text == "💬 SUPPORT":

        await support_start(
            update,
            context
        )


    elif text == "🛠 ADMIN PANEL":

        await admin_panel(
            update,
            context
        )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(update, context):

    query = update.callback_query

    data = query.data


    if data == "verify_join":

        await verify_join(
            update,
            context
        )


    elif data.startswith("buyplan_"):

        await create_payment(
            update,
            context
        )


    elif data.startswith("approve_"):

        await payment_action(
            update,
            context
        )


    elif data.startswith("reject_"):

        await payment_action(
            update,
            context
        )


    elif data == "admin_servers":

        await admin_servers(
            update,
            context
        )


    elif data == "add_server":

        await add_server_start(
            update,
            context
        )


    elif data.startswith("delete_"):

        await delete_server(
            update,
            context
        )


    elif data == "admin_payments":

        await admin_payments(
            update,
            context
        )


    elif data == "admin_broadcast":

        await broadcast_start(
            update,
            context
        )


    elif data == "admin_users":

        await admin_users(
            update,
            context
        )


# ============================================================
# MAIN
# ============================================================

def main():

    app = (

        Application.builder()

        .token(BOT_TOKEN)

        .build()

    )


    app.add_handler(

        CommandHandler(
            "start",
            start
        )
    )


    app.add_handler(

        CallbackQueryHandler(
            callback_handler
        )
    )


    app.add_handler(

        MessageHandler(

            filters.TEXT
            & ~filters.COMMAND,

            text_handler
        )
    )


    print(
        "BOT STARTED..."
    )


    app.run_polling()


if __name__ == "__main__":

    main()