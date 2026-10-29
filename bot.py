import os
import sys
import re
import uuid
import secrets
import asyncio
from datetime import datetime, timedelta
import aiohttp
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.errors import (
    FloodWait, 
    UserNotParticipant, 
    ChatAdminRequired, 
    MessageNotModified
)
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
    BotCommand
)
from motor.motor_asyncio import AsyncIOMotorClient

# ---------------- CONFIGURATION / ENV VARIABLES ---------------- #
API_ID = int(os.environ.get("API_ID", "29904834"))
API_HASH = os.environ.get("API_HASH", "8b4fd9ef578af114502feeafa2d31938")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8915927925:AAGCRPRRqniqN9JMe-nr8JneEjTkFFcFlNg")
OWNER_ID = int(os.environ.get("OWNER_ID", "7120801813"))
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://FlixBixs:FlixBixs@cluster0.awkvdsd.mongodb.net/?appName=Cluster0")
PORT = int(os.environ.get("PORT", "8080"))

# ---------------- DATABASE CONNECTION ---------------- #
# serverSelectionTimeoutMS দিয়ে ৫ সেকেন্ডে টাইমআউট করা হলো যাতে ডাটাবেজ ব্লক হলে ইনফিনিট ফ্রিজ না হয়
mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo_client["UltimateFileStoreBot"]

users_col = db["users"]
settings_col = db["settings"]
categories_col = db["categories"]
files_col = db["files"]
mustjoin_col = db["mustjoin"]
shorteners_col = db["shorteners"]
redeem_col = db["redeem_codes"]
tokens_col = db["tokens"]

# ---------------- PYROGRAM BOT CLIENT ---------------- #
app = Client(
    "UltimateFileStoreBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True  # রেন্ডার/কোয়েবের ফাইল ডিলিট ইস্যু এড়াতে মেমোরি সেশন ব্যবহার করা হয়েছে
)

# ---------------- EMBEDDED WEB SERVER (FOR KOYEB & RENDER PORT BINDING) ---------------- #
async def web_server():
    async def handle_ping(request):
        return web.Response(text="Bot is running smoothly on Koyeb/Render!")
    server_app = web.Application()
    server_app.router.add_get("/", handle_ping)
    runner = web.AppRunner(server_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ---------------- FORCE WEBHOOK DELETION ---------------- #
async def remove_webhook_if_exists():
    """টেলিগ্রাম এপিআই থেকে সরাসরি যেকোনাে অ্যাক্টিভ ওয়েবহুক ডিলিট করবে"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                res = await resp.json()
                print(f"📡 Webhook Status Cleaned: {res.get('description', 'OK')}")
    except Exception as e:
        print(f"⚠️ Webhook removal error: {e}")

# ---------------- CORE HELPER FUNCTIONS ---------------- #
async def get_settings():
    settings = await settings_col.find_one({"_id": "config"})
    if not settings:
        default = {
            "_id": "config",
            "logo": None,
            "video_btn_name": "ভিডিও টিউটোরিয়াল",
            "video_btn_url": "https://t.me",
            "admin_contact": "Admin",
            "protect_content": False,
            "getfile_channel": None,
            "auto_delete_time": 0,
            "files_per_request": 1  # ডিফল্টভাবে প্রতি ক্লিকে ১টি করে ফাইল যাবে
        }
        await settings_col.insert_one(default)
        return default
    return settings

async def add_user(user_id: int):
    user = await users_col.find_one({"_id": user_id})
    if not user:
        await users_col.insert_one({
            "_id": user_id,
            "joined_at": datetime.utcnow(),
            "premium_expiry": None,
            "verified_until": None,
            "video_pointer": {},
            "shortlink_count": 0
        })

async def is_premium_user(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    user = await users_col.find_one({"_id": user_id})
    if not user:
        return False
    expiry = user.get("premium_expiry")
    if expiry and expiry > datetime.utcnow():
        return True
    return False

async def is_verified_user(user_id: int) -> bool:
    if await is_premium_user(user_id):
        return True
    user = await users_col.find_one({"_id": user_id})
    if not user:
        return False
    v_until = user.get("verified_until")
    if v_until and v_until > datetime.utcnow():
        return True
    return False

def slugify_command(text: str) -> str:
    """যেকোনো টেক্সটকে টেলিগ্রাম স্ল্যাশ কমান্ডের উপযোগী স্ট্রিং-এ রূপান্তর করে"""
    clean = re.sub(r'[^a-zA-Z0-9_]', '', text.lower().replace(" ", "_"))
    return clean if clean else "file"

# ---------------- TELEGRAM BOT COMMANDS MENU SYNC ---------------- #
async def sync_bot_commands(client: Client):
    """বটের সকল সাধারণ ও ক্যাটাগরি কমান্ড টেলিগ্রাম মেন্যুতে সেট করে"""
    cmds = [
        BotCommand("start", "বট চালু করুন ও মূল মেন্যু দেখুন"),
        BotCommand("help", "সাহায্য ও সকল কমান্ডের উদাহরণ"),
        BotCommand("skip", "নির্দিষ্ট পর্ব থেকে দেখা শুরু করুন"),
        BotCommand("reedim", "প্রিমিয়াম মেম্বারশিপ রিডিম কোড ব্যবহার"),
    ]
    # সকল ক্যাটাগরির নিজস্ব কমান্ড যুক্ত করা
    async for cat in categories_col.find():
        cmd = cat.get("cmd", slugify_command(cat["name"]))
        cmds.append(BotCommand(cmd, f"{cat['name']} ফাইলসমূহ"))

    try:
        await client.set_bot_commands(cmds)
        print("✅ Telegram Bot Commands Menu Synced!")
    except Exception as e:
        print(f"⚠️ Failed to sync commands: {e}")

# ---------------- AUTO-DELETE BACKGROUND TASK ---------------- #
async def auto_delete_task(client: Client, chat_id: int, message_ids: list, delay_sec: int, notice_id: int = None):
    await asyncio.sleep(delay_sec)
    for mid in message_ids:
        try:
            await client.delete_messages(chat_id=chat_id, message_ids=mid)
        except Exception:
            pass
    if notice_id:
        try:
            await client.delete_messages(chat_id=chat_id, message_ids=notice_id)
        except Exception:
            pass

# ---------------- FORCE SUBSCRIPTION (MUST JOIN) CHECK ---------------- #
async def check_must_join(client: Client, user_id: int):
    if user_id == OWNER_ID:
        return True, []
    unjoined = []
    async for ch in mustjoin_col.find():
        try:
            await client.get_chat_member(ch["channel_id"], user_id)
        except (UserNotParticipant, ChatAdminRequired):
            unjoined.append(ch)
        except Exception:
            continue
    return (len(unjoined) == 0), unjoined

def get_must_join_markup(unjoined_channels):
    buttons = []
    for ch in unjoined_channels:
        buttons.append([InlineKeyboardButton(f"📢 জয়েন করুন: {ch['name']}", url=ch["invite_link"])])
    buttons.append([InlineKeyboardButton("🔄 জয়েন সম্পন্ন করেছি (Try Again)", callback_data="check_join_again")])
    return InlineKeyboardMarkup(buttons)

# ---------------- SHORTLINK GENERATOR (MULTI-DOMAIN ROTATION) ---------------- #
async def generate_shortlink(user_id: int, bot_username: str, action: str = "general"):
    shorteners = await shorteners_col.find().to_list(length=100)
    if not shorteners:
        return None, None

    user = await users_col.find_one({"_id": user_id})
    count = user.get("shortlink_count", 0) if user else 0
    selected = shorteners[count % len(shorteners)]
    await users_col.update_one({"_id": user_id}, {"$inc": {"shortlink_count": 1}})

    token = secrets.token_urlsafe(16)
    await tokens_col.insert_one({
        "_id": token,
        "user_id": user_id,
        "action": action,
        "used": False,
        "created_at": datetime.utcnow()
    })

    long_url = f"https://t.me/{bot_username}?start=verify_{token}"
    api_url = f"https://{selected['domain']}/api?api={selected['api']}&url={long_url}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=10) as resp:
                data = await resp.json()
                if "shortenedUrl" in data:
                    return data["shortenedUrl"], token
                elif "url" in data:
                    return data["url"], token
    except Exception as e:
        print(f"Shortlink API Error: {e}")
    return long_url, token

# ---------------- BATCH & SEQUENTIAL FILE SENDER (NO BUTTONS, PURE FILE) ---------------- #
async def send_batch_serial_videos(client: Client, user_id: int, channel_id: int, start_serial: int):
    settings = await get_settings()
    protect = settings.get("protect_content", False)
    auto_del = settings.get("auto_delete_time", 0)
    batch_count = settings.get("files_per_request", 1)

    total_videos = await files_col.count_documents({"channel_id": channel_id})
    if total_videos == 0:
        return await client.send_message(user_id, "⚠️ এই ক্যাটাগরিতে এখনো কোনো ফাইল বা ভিডিও ইনডেক্স করা হয়নি! এডমিনকে বলুন `/index` কমান্ড দিতে।")

    if start_serial < 1:
        start_serial = 1
    if start_serial > total_videos:
        start_serial = 1

    sent_message_ids = []
    current_num = start_serial

    # এক ক্লিকে যতগুলো ফাইল সেট করা হয়েছে ততগুলো একসাথে পাঠানো হবে (হোম, নেক্সট, প্রিভিয়াস বাটন ছাড়া)
    for i in range(batch_count):
        target_serial = ((current_num - 1 + i) % total_videos) + 1
        video_doc = await files_col.find_one({"channel_id": channel_id, "serial": target_serial})
        
        if video_doc:
            try:
                caption_text = f"🎬 **পর্ব: {target_serial}/{total_videos}**\n\n{video_doc.get('caption', '')}"
                sent_msg = await client.copy_message(
                    chat_id=user_id,
                    from_chat_id=channel_id,
                    message_id=video_doc["message_id"],
                    caption=caption_text,
                    protect_content=protect
                )
                sent_message_ids.append(sent_msg.id)
                await asyncio.sleep(0.6)  # টেলিগ্রাম ফ্লাডওয়েট এড়াতে সুরক্ষা বিরতি
            except Exception as e:
                print(f"File send error (serial {target_serial}): {e}")

    # ইউজারের পরবর্তী পয়েন্টার স্বয়ংক্রিয়ভাবে ব্যাচ অনুযায়ী আপডেট হবে
    next_pointer = ((current_num - 1 + batch_count) % total_videos) + 1
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {f"video_pointer.{channel_id}": next_pointer}}
    )

    # অটো ডিলিট কার্যকর করা
    if auto_del and auto_del > 0 and sent_message_ids:
        notice = await client.send_message(
            chat_id=user_id,
            text=f"⏳ **সতর্কবার্তা:** এই ফাইলগুলো আগামী **{auto_del} সেকেন্ড** পর স্বয়ংক্রিয়ভাবে মুছে যাবে!"
        )
        asyncio.create_task(auto_delete_task(client, user_id, sent_message_ids, auto_del, notice.id))

# ---------------- CATEGORY & GETFILE DELIVERY LOGIC ---------------- #
async def handle_category_request(client: Client, user_id: int, channel_id: int, message_or_query):
    settings = await get_settings()
    is_premium = await is_premium_user(user_id)
    batch_count = settings.get("files_per_request", 1)

    user_doc = await users_col.find_one({"_id": user_id})
    current_pointer = user_doc.get("video_pointer", {}).get(str(channel_id), 1) if user_doc else 1

    # সাধারণ ইউজার হলে প্রতিবারই বাধ্যতামূলক ভেরিফাই লিংক আসবে (বিজ্ঞাপন ইনকাম নিশ্চিতকরণ)
    if not is_premium:
        me = await client.get_me()
        short_url, _ = await generate_shortlink(user_id, me.username, action=f"cat_{channel_id}")
        if short_url:
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔓 {batch_count}টি ফাইল আনলক করুন", url=short_url)],
                [InlineKeyboardButton("💎 প্রিমিয়াম নিন (বিজ্ঞাপনহীন)", url=f"https://t.me/{settings['admin_contact'].replace('@', '')}")]
            ])
            text = (
                f"🔒 **{current_pointer} নম্বর পর্ব থেকে মোট {batch_count}টি ফাইল দেখতে ভেরিফাই করুন!**\n\n"
                "👉 নিচের লিংকে গিয়ে ভেরিফিকেশন সম্পন্ন করলেই ফাইল সাথে সাথে ইনবক্সে চলে আসবে।\n"
                "💎 প্রিমিয়াম মেম্বারদের কোনো বিজ্ঞাপন বা ভেরিফাই করতে হয় না।"
            )
            if isinstance(message_or_query, CallbackQuery):
                return await message_or_query.message.reply_text(text, reply_markup=btn)
            return await message_or_query.reply_text(text, reply_markup=btn)

    # প্রিমিয়াম হলে সরাসরি ভিডিও পাবে (স্টার্ট মেন্যুর বাটন কখনই ডিলিট হবে না)
    await send_batch_serial_videos(client, user_id, channel_id, current_pointer)

async def handle_special_getfile_request(client: Client, user_id: int, message_or_query):
    """গেটফাইল চ্যানেলের ফাইল হ্যান্ডলিং"""
    # প্রথমে চেক করবে getfile নামে কোনো ক্যাটাগরি তৈরি আছে কিনা
    cat = await categories_col.find_one({"cmd": "getfile"})
    if not cat:
        cat = await categories_col.find_one({"name": {"$regex": "^getfile$", "$options": "i"}})

    if cat:
        return await handle_category_request(client, user_id, cat["channel_id"], message_or_query)

    settings = await get_settings()
    protect = settings.get("protect_content", False)
    auto_del = settings.get("auto_delete_time", 0)
    getfile_cid = settings.get("getfile_channel")

    if not getfile_cid:
        err = (
            "⚠️ GetFile চ্যানেল এখনো সেট করা হয়নি!\n\n"
            "💡 **সমাধান:**\n"
            "১. হয় লিখুন: `/setgetfile -100xxxxxxxxxx`\n"
            "২. অথবা ক্যাটাগরি হিসেবে যুক্ত করুন: `/setcategory getfile -100xxxxxxxxxx`"
        )
        if isinstance(message_or_query, CallbackQuery):
            return await message_or_query.answer(err, show_alert=True)
        return await message_or_query.reply_text(err)

    is_premium = await is_premium_user(user_id)
    if not is_premium:
        me = await client.get_me()
        short_url, _ = await generate_shortlink(user_id, me.username, action="special_getfile")
        if short_url:
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔓 স্পেশাল ফাইল আনলক করুন", url=short_url)],
                [InlineKeyboardButton("💎 প্রিমিয়াম নিন (বিজ্ঞাপনহীন)", url=f"https://t.me/{settings['admin_contact'].replace('@', '')}")]
            ])
            text = (
                "🔒 **স্পেশাল ফাইল পেতে হলে একটি ভেরিফিকেশন সম্পন্ন করুন!**\n\n"
                "👉 নিচের আনলক লিংকে গিয়ে ভেরিফাই সম্পন্ন করুন, ফাইল সাথে সাথে পেয়ে যাবেন।"
            )
            if isinstance(message_or_query, CallbackQuery):
                return await message_or_query.message.reply_text(text, reply_markup=btn)
            return await message_or_query.reply_text(text, reply_markup=btn)

    # প্রিমিয়াম ইউজার সরাসরি ফাইল পাবে (কোনো বাটন ছাড়া)
    try:
        sent_any = False
        async for post in client.get_chat_history(getfile_cid, limit=1):
            sent_msg = await post.copy(chat_id=user_id, protect_content=protect)
            sent_any = True
            if auto_del and auto_del > 0:
                notice = await client.send_message(
                    chat_id=user_id,
                    text=f"⏳ **সতর্কবার্তা:** এই ফাইলটি আগামী **{auto_del} সেকেন্ড** পর স্বয়ংক্রিয়ভাবে মুছে যাবে!"
                )
                asyncio.create_task(auto_delete_task(client, user_id, [sent_msg.id], auto_del, notice.id))

        if not sent_any:
            msg = "⚠️ GetFile চ্যানেলে কোনো পোস্ট পাওয়া যায়নি!"
            if isinstance(message_or_query, CallbackQuery):
                return await message_or_query.answer(msg, show_alert=True)
            return await message_or_query.reply_text(msg)

        if isinstance(message_or_query, CallbackQuery):
            await message_or_query.answer("✅ ফাইল পাঠানো হয়েছে!", show_alert=False)
    except Exception as e:
        err = f"❌ এরর: {str(e)}"
        if isinstance(message_or_query, CallbackQuery):
            await message_or_query.answer(err, show_alert=True)
        else:
            await message_or_query.reply_text(err)

# ---------------- /start COMMAND ---------------- #
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user = message.from_user
    await add_user(user.id)
    settings = await get_settings()

    # 1. Single-Use Verification Token Check & Direct Batch Delivery
    if len(message.command) > 1 and message.command[1].startswith("verify_"):
        token = message.command[1].split("_")[1]
        token_doc = await tokens_col.find_one({"_id": token})

        if not token_doc:
            return await message.reply_text("❌ অবৈধ বা ভুল ভেরিফিকেশন লিংক!")
        if token_doc.get("used"):
            return await message.reply_text("❌ এই লিংকটি ইতিমধ্যে ব্যবহার করা হয়েছে! পরবর্তী ফাইল পাওয়ার জন্য আবার মেনু থেকে বাটনে চাপ দিন বা কমান্ড দিন।")
        if token_doc.get("user_id") != user.id:
            return await message.reply_text("❌ এই ভেরিফিকেশন লিংকটি অন্য কোনো ইউজারের জন্য তৈরি করা হয়েছে!")

        # মার্ক টোকেন ইউসড
        await tokens_col.update_one({"_id": token}, {"$set": {"used": True}})
        await message.reply_text("🎉 **ভেরিফিকেশন সফল হয়েছে!** আপনার কাঙ্ক্ষিত ফাইলগুলো পাঠানো হচ্ছে...")

        action = token_doc.get("action", "")
        if action == "special_getfile":
            getfile_cid = settings.get("getfile_channel")
            if getfile_cid:
                async for post in client.get_chat_history(getfile_cid, limit=1):
                    sent_msg = await post.copy(chat_id=user.id, protect_content=settings.get("protect_content", False))
                    auto_del = settings.get("auto_delete_time", 0)
                    if auto_del and auto_del > 0:
                        notice = await client.send_message(chat_id=user.id, text=f"⏳ ফাইলটি **{auto_del} সেকেন্ড** পর মুছে যাবে!")
                        asyncio.create_task(auto_delete_task(client, user.id, [sent_msg.id], auto_del, notice.id))
            return
        elif action.startswith("cat_"):
            cid = int(action.split("_")[1])
            user_doc = await users_col.find_one({"_id": user.id})
            pos = user_doc.get("video_pointer", {}).get(str(cid), 1) if user_doc else 1
            return await send_batch_serial_videos(client, user.id, cid, pos)
        return

    # 2. Must-Join Channel Check
    is_joined, unjoined = await check_must_join(client, user.id)
    if not is_joined:
        return await message.reply_text(
            "⚠️ **বটটি ব্যবহার করতে হলে আপনাকে আমাদের নিচের চ্যানেলগুলোতে জয়েন করতে হবে!**\nসবগুলোতে জয়েন করে 'Try Again' বাটনে চাপ দিন:",
            reply_markup=get_must_join_markup(unjoined)
        )

    # 3. Main Start Message Display
    per_v = settings.get("files_per_request", 1)
    caption = (
        f"👋 **স্বাগতম, {user.first_name}!**\n\n"
        f"👤 **আপনার প্রোফাইল তথ্য:**\n"
        f"├ **নাম:** {user.first_name} {user.last_name or ''}\n"
        f"├ **ইউজারনেম:** @{user.username if user.username else 'N/A'}\n"
        f"├ **ইউজার আইডি:** `{user.id}`\n"
        f"└ **স্ট্যাটাস:** {'💎 প্রিমিয়াম মেম্বার' if await is_premium_user(user.id) else '🆓 সাধারণ ইউজার'}\n\n"
        f"📦 **প্রতি ক্লিকে ডেলিভারি:** `{per_v} টি ফাইল`\n\n"
        f"📂 নিচের বাটন থেকে অথবা সরাসরি স্ল্যাশ কমান্ড দিয়ে ফাইল বেছে নিন:"
    )

    buttons = []

    # GetFile Button (যদি স্পেশাল গেটফাইল চ্যানেল থাকে)
    if settings.get("getfile_channel"):
        buttons.append([InlineKeyboardButton("🌟 স্পেশাল ফাইল (Get Files)", callback_data="special_getfile")])

    # Dynamic Category Buttons (বাটনে শুধুই নাম থাকবে, কোনো অতিরিক্ত স্ল্যাশ বা ব্র্যাকেট থাকবে না)
    async for cat in categories_col.find():
        buttons.append([InlineKeyboardButton(f"📁 {cat['name']}", callback_data=f"playcat_{cat['channel_id']}")])

    # Tutorial Video Button
    if settings.get("video_btn_url"):
        buttons.append([InlineKeyboardButton(f"🎥 {settings.get('video_btn_name')}", url=settings.get("video_btn_url"))])

    # Admin / Buy Premium Button
    if settings.get("admin_contact"):
        buttons.append([InlineKeyboardButton("💎 প্রিমিয়াম কিনুন (Admin)", url=f"https://t.me/{settings['admin_contact'].replace('@', '')}")])

    keyboard = InlineKeyboardMarkup(buttons)

    if settings.get("logo"):
        try:
            return await message.reply_photo(photo=settings["logo"], caption=caption, reply_markup=keyboard)
        except Exception:
            pass

    await message.reply_text(text=caption, reply_markup=keyboard)

# ---------------- /setcmd COMMAND (অটোমেটিক সকল কমান্ড টেলিগ্রাম মেনুতে বসানো) ---------------- #
@app.on_message(filters.command("setcmd") & filters.user(OWNER_ID))
async def setcmd_handler(client: Client, message: Message):
    status = await message.reply_text("⏳ টেলিগ্রাম মেন্যুতে সকল কমান্ড সেট করা হচ্ছে...")
    await sync_bot_commands(client)
    await status.edit_text(
        "✅ **সফল হয়েছে!**\n"
        "বটের সকল সাধারণ কমান্ড এবং আপনার তৈরি করা সকল ক্যাটাগরি কমান্ড টেলিগ্রামের মেনুতে বসে গেছে।\n"
        "এখন চ্যাটে যেকোনো ইউজার `/` চাপলেই কমান্ডগুলোর তালিকা দেখতে পাবে।"
    )

# ---------------- /setparvideo COMMAND (এক ক্লিকে কত ফাইল যাবে) ---------------- #
@app.on_message(filters.command(["setparvideo", "setpervideo"]) & filters.user(OWNER_ID))
async def setparvideo_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        settings = await get_settings()
        curr = settings.get("files_per_request", 1)
        return await message.reply_text(
            f"📦 বর্তমানে প্রতি ক্লিকে ফাইল পাঠানো হচ্ছে: **{curr} টি**\n\n"
            f"⚠️ **ব্যবহারের সঠিক নিয়ম:**\n"
            f"`/setparvideo [ফাইলের সংখ্যা]`\n\n"
            f"💡 **উদাহরণ:**\n"
            f"• প্রতি ক্লিকে ২টি ফাইল দিতে: `/setparvideo 2`\n"
            f"• প্রতি ক্লিকে ৩টি ফাইল দিতে: `/setparvideo 3`\n"
            f"• প্রতি ক্লিকে ৫টি ফাইল দিতে: `/setparvideo 5`"
        )

    count = int(args[1])
    if count <= 0:
        return await message.reply_text("⚠️ ফাইলের সংখ্যা অবশ্যই ১ বা তার বেশি হতে হবে!")

    await settings_col.update_one({"_id": "config"}, {"$set": {"files_per_request": count}}, upsert=True)
    await message.reply_text(
        f"✅ **সফলভাবে সেট করা হয়েছে!**\n\n"
        f"📦 এখন থেকে কোনো ইউজার একবার বাটনে ক্লিক করলে বা কমান্ড দিলে একসাথে **{count}টি** করে ফাইল পাবে।"
    )

# ---------------- /help COMMAND WITH ALL EXAMPLES ---------------- #
@app.on_message(filters.command("help") & filters.private)
async def help_handler(client: Client, message: Message):
    is_owner = (message.from_user.id == OWNER_ID)
    user_cmds = (
        "📖 **সকল ইউজার কমান্ড ও ব্যবহারের উদাহরণ:**\n\n"
        "1️⃣ `/start` - বট চালু করা ও মূল মেন্যু দেখা।\n"
        "2️⃣ `/skip [নাম্বার]` - নির্দিষ্ট ভিডিও নম্বরে স্কিপ করা।\n"
        "   └ 💡 উদাহরণ: `/skip 10`\n"
        "3️⃣ `/reedim [কোড]` - প্রিমিয়াম কোড রিডিম করা।\n"
        "   └ 💡 উদাহরণ: `/reedim PREM-ABC12345`\n"
        "4️⃣ `/getfile` - সরাসরি স্পেশাল ফাইল বা getfile ক্যাটাগরি নেওয়া।\n"
        "   └ 💡 উদাহরণ: `/getfile`\n"
        "5️⃣ `/[ক্যাটাগরি_নাম]` - ক্যাটাগরির নামে সরাসরি স্ল্যাশ কমান্ড দিয়ে ফাইল নেওয়া।\n"
        "   └ 💡 উদাহরণ: `/natok` অথবা `/movie`\n"
    )
    admin_cmds = (
        "\n⚙️ **অ্যাডমিন কমান্ডসমূহ ও উদাহরণ:**\n\n"
        "• `/setcmd` - টেলিগ্রামের মেন্যুতে সব কমান্ডের লিস্ট বসানো\n"
        "• `/setparvideo 3` - প্রতি ক্লিকে একসাথে কতগুলো ফাইল যাবে তা সেট করা।\n"
        "• `/autodelet 30` - ৩০ সেকেন্ড পর অটো-ডিলিট সেট। বন্ধ করতে `/autodelet 0`\n"
        "• `/forward` - ফাইল ফরওয়ার্ড অপশন অন/অফ টগল।\n"
        "• `/setcategory নাটক -100123456789` - নতুন ক্যাটাগরি ও কমান্ড যুক্ত করা।\n"
        "• `/setgetfile -100123456789` - স্পেশাল getfile চ্যানেল সেট করা।\n"
        "• `/setlink droplink.co api_key` - শর্টনার যুক্ত করা।\n"
        "• `/deletlink` - শর্টনার লিংক ডিলিট করা।\n"
        "• `/mustjoine চ্যানেলনাম -100123456789` - মাস্ট জয়েন চ্যানেল অ্যাড করা।\n"
        "• `/addreedim 1440 5` - ১ দিনের ৫টি রিডিম কোড তৈরি।\n"
        "• `/deletpremium 123456789` - ইউজারের প্রিমিয়াম বাতিল করা।\n"
        "• `/premiumlist` - সক্রিয় প্রিমিয়াম লিস্ট দেখা।\n"
        "• `/setadmin @AdminUser` - অ্যাডমিন কন্টাক্ট সেট করা।\n"
        "• `/setlogo [ছবি রিপ্লাই বা লিংক]` - লোগো পরিবর্তন করা।\n"
        "• `/setvideo টিউটোরিয়াল https://t.me/link` - ভিডিও বাটন সেট।\n"
        "• `/index` - চ্যানেলের ভিডিও ১, ২, ৩... সিরিয়ালে সাজানো।\n"
        "• `/broadcast` - যেকোনো মেসেজে রিপ্লাই দিয়ে সকল ইউজারকে পাঠানো।"
    )
    msg = user_cmds + (admin_cmds if is_owner else "")
    await message.reply_text(msg)

# ---------------- /autodelet COMMAND ---------------- #
@app.on_message(filters.command(["autodelet", "autodelete"]) & filters.user(OWNER_ID))
async def autodelete_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        settings = await get_settings()
        curr = settings.get("auto_delete_time", 0)
        status = f"⏱️ বর্তমানে সেট করা আছে: **{curr} সেকেন্ড**" if curr > 0 else "❌ বর্তমানে অটো-ডিলিট ফিচার **বন্ধ (OFF)** আছে।"
        return await message.reply_text(
            f"{status}\n\n"
            f"ℹ️ **কমান্ড ব্যবহারের নিয়ম ও উদাহরণ:**\n"
            f"• **সেট করতে:** `/autodelet [সেকেন্ড]`\n"
            f"  └ 💡 উদাহরণ: `/autodelet 30` (৩০ সেকেন্ড পর ফাইল মুছে যাবে)\n"
            f"  └ 💡 উদাহরণ: `/autodelet 60` (১ মিনিট পর মুছে যাবে)\n"
            f"• **বন্ধ করতে:** `/autodelet 0` বা `/autodelet off`"
        )

    val = args[1].lower().replace("সেকেন্ড", "").strip()
    if val in ["0", "off", "বন্ধ"]:
        await settings_col.update_one({"_id": "config"}, {"$set": {"auto_delete_time": 0}}, upsert=True)
        return await message.reply_text("✅ ফাইল অটো-ডিলিট ফিচারটি **বন্ধ (OFF)** করা হয়েছে।")

    try:
        seconds = int(val)
        if seconds <= 0:
            raise ValueError
    except ValueError:
        return await message.reply_text("⚠️ সেকেন্ড অবশ্যই ধনাত্মক পূর্ণসংখ্যা হতে হবে!\n💡 উদাহরণ: `/autodelet 30`")

    await settings_col.update_one({"_id": "config"}, {"$set": {"auto_delete_time": seconds}}, upsert=True)
    await message.reply_text(
        f"✅ **অটো-ডিলিট সফলভাবে সেট হয়েছে!**\n\n"
        f"⏱️ এখন থেকে পাঠানো ফাইল ঠিক **{seconds} সেকেন্ড** পর স্বয়ংক্রিয়ভাবে মুছে যাবে।"
    )

# ---------------- /skip COMMAND ---------------- #
@app.on_message(filters.command("skip") & filters.private)
async def skip_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await add_user(user_id)

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply_text(
            "⚠️ **ভুল কমান্ড ফরম্যাট!**\n\n"
            "📝 **ব্যবহারের নিয়ম:** `/skip [ভিডিও নম্বর]`\n"
            "💡 **উদাহরণ:**\n"
            "• `/skip 10` (১০ নম্বর ভিডিও থেকে দেখতে)\n"
            "• `/skip 50` (৫০ নম্বর ভিডিও থেকে দেখতে)"
        )

    target_pos = int(args[1])
    buttons = []
    async for cat in categories_col.find():
        buttons.append([InlineKeyboardButton(cat["name"], callback_data=f"setskip_{cat['channel_id']}_{target_pos}")])

    if not buttons:
        return await message.reply_text("⚠️ ডাটাবেজে কোনো ক্যাটাগরি তৈরি করা নেই!")

    await message.reply_text(
        f"📌 আপনি **{target_pos}** নম্বর ভিডিও থেকে দেখতে চেয়েছেন।\nকোন ক্যাটাগরিতে এই স্কিপ সেট করতে চান বেছে নিন:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ---------------- /forward COMMAND ---------------- #
@app.on_message(filters.command("forward") & filters.user(OWNER_ID))
async def forward_toggle(client: Client, message: Message):
    settings = await get_settings()
    current = settings.get("protect_content", False)
    new_state = not current
    await settings_col.update_one({"_id": "config"}, {"$set": {"protect_content": new_state}}, upsert=True)
    status_text = "🔒 অন (ফাইল ফরওয়ার্ড বন্ধ/সুরক্ষিত)" if new_state else "🔓 অফ (ফাইল ফরওয়ার্ড উন্মুক্ত)"
    await message.reply_text(
        f"✅ ফরোয়ার্ড রেস্ট্রিকশন পরিবর্তন হয়েছে:\n"
        f"📢 বর্তমান অবস্থা: **{status_text}**\n\n"
        f"💡 উদাহরণ: পুনরায় টগল করতে আবার `/forward` লিখুন।"
    )

# ---------------- REDEEM & PREMIUM SYSTEM ---------------- #
@app.on_message(filters.command("addreedim") & filters.user(OWNER_ID))
async def addreedim_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        return await message.reply_text(
            "⚠️ **ভুল কমান্ড ফরম্যাট!**\n\n"
            "📝 **ব্যবহারের নিয়ম:** `/addreedim [মেয়াদ মিনিটে] [কোডের সংখ্যা]`\n\n"
            "💡 **উদাহরণ:**\n"
            "• `/addreedim 1440 5` (২৪ ঘণ্টার জন্য ৫টি কোড)\n"
            "• `/addreedim 10080 2` (৭ দিনের জন্য ২টি কোড)"
        )

    duration = int(args[1])
    count = int(args[2])
    created_codes = []

    for _ in range(count):
        code = f"PREM-{uuid.uuid4().hex[:8].upper()}"
        await redeem_col.insert_one({
            "_id": code,
            "duration_minutes": duration,
            "used": False,
            "used_by": None,
            "created_at": datetime.utcnow()
        })
        created_codes.append(code)

    codes_txt = "\n".join([f"`{c}`" for c in created_codes])
    await message.reply_text(f"✅ **{count} টি রিডিম কোড তৈরি হয়েছে ({duration} মিনিটের জন্য):**\n\n{codes_txt}")

@app.on_message(filters.command("reedim") & filters.private)
async def redeem_code_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await add_user(user_id)
    args = message.text.split()
    if len(args) < 2:
        return await message.reply_text(
            "⚠️ **রিডিম কোড উল্লেখ করেননি!**\n\n"
            "📝 **ব্যবহারের নিয়ম:** `/reedim [আপনার কোড]`\n"
            "💡 **উদাহরণ:** `/reedim PREM-A1B2C3D4`"
        )

    code = args[1].strip()
    doc = await redeem_col.find_one({"_id": code})

    if not doc:
        return await message.reply_text("❌ ভুল বা অস্তিত্বহীন রিডিম কোড! সঠিক কোড দিয়ে আবার চেষ্টা করুন।")
    if doc.get("used"):
        return await message.reply_text("❌ এই রিডিম কোডটি আগেই ব্যবহার করা হয়েছে!")

    duration = doc.get("duration_minutes", 10)
    user = await users_col.find_one({"_id": user_id})
    current_expiry = user.get("premium_expiry")

    start_point = current_expiry if (current_expiry and current_expiry > datetime.utcnow()) else datetime.utcnow()
    new_expiry = start_point + timedelta(minutes=duration)

    await users_col.update_one({"_id": user_id}, {"$set": {"premium_expiry": new_expiry}})
    await redeem_col.update_one({"_id": code}, {"$set": {"used": True, "used_by": user_id, "used_at": datetime.utcnow()}})

    await message.reply_text(f"🎉 **অভিনন্দন! আপনার প্রিমিয়াম অ্যাক্টিভ হয়েছে।**\n⏰ মেয়াদ: `{new_expiry.strftime('%Y-%m-%d %H:%M:%S UTC')}` পর্যন্ত।")

@app.on_message(filters.command("deletpremium") & filters.user(OWNER_ID))
async def delete_premium_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply_text(
            "⚠️ **ভুল কমান্ড ফরম্যাট!**\n\n"
            "📝 **ব্যবহারের নিয়ম:** `/deletpremium [ইউজার আইডি]`\n"
            "💡 **উদাহরণ:** `/deletpremium 7120801813`"
        )
    target_uid = int(args[1])
    await users_col.update_one({"_id": target_uid}, {"$set": {"premium_expiry": None}})
    await message.reply_text(f"✅ ইউজার `{target_uid}` এর প্রিমিয়াম অ্যাক্সেস বাতিল করা হয়েছে।")

@app.on_message(filters.command("premiumlist") & filters.user(OWNER_ID))
async def premium_list_handler(client: Client, message: Message):
    now = datetime.utcnow()
    cursor = users_col.find({"premium_expiry": {"$gt": now}})
    msg = "💎 **সক্রিয় প্রিমিয়াম ইউজারদের তালিকা:**\n\n"
    count = 0
    async for u in cursor:
        count += 1
        rem = u['premium_expiry'] - now
        mins = int(rem.total_seconds() // 60)
        msg += f"{count}. আইডি: `{u['_id']}` — অবশিষ্ট: {mins} মিনিট\n"
    if count == 0:
        msg = "⚠️ বর্তমানে কোনো প্রিমিয়াম ইউজার নেই!"
    await message.reply_text(msg)

@app.on_message(filters.command("setadmin") & filters.user(OWNER_ID))
async def setadmin_handler(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text(
            "⚠️ **অ্যাডমিন ইউজারনেম দেননি!**\n\n"
            "📝 **ব্যবহারের নিয়ম:** `/setadmin [ইউজারনেম]`\n"
            "💡 **উদাহরণ:** `/setadmin @MyTelegramUser`"
        )
    adm = args[1].strip()
    await settings_col.update_one({"_id": "config"}, {"$set": {"admin_contact": adm}}, upsert=True)
    await message.reply_text(f"✅ প্রিমিয়াম কেনার জন্য অ্যাডমিন কন্টাক্ট সেট হয়েছে: `{adm}`")

# ---------------- /setgetfile & /getfile (FIXED FOR BOTH SPECIAL & CATEGORY) ---------------- #
@app.on_message(filters.command("setgetfile") & filters.user(OWNER_ID))
async def setgetfile_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply_text(
            "⚠️ **চ্যানেল আইডি দেননি!**\n\n"
            "📝 **ব্যবহারের নিয়ম:** `/setgetfile [চ্যানেল আইডি]`\n"
            "💡 **উদাহরণ:** `/setgetfile -1001234567890`"
        )
    try:
        chid = int(args[1])
        await settings_col.update_one({"_id": "config"}, {"$set": {"getfile_channel": chid}}, upsert=True)
        await message.reply_text(f"✅ GetFile স্পেশাল চ্যানেল সেট করা হয়েছে: `{chid}`\nএখন ইউজাররা `/getfile` দিলে অথবা বাটনে চাপলে ফাইল পাবে।")
    except ValueError:
        await message.reply_text("⚠️ চ্যানেল আইডি অবশ্যই পূর্ণসংখ্যা (Integer) হতে হবে!\n💡 উদাহরণ: `/setgetfile -1001234567890`")

@app.on_message(filters.command("getfile") & filters.private)
async def getfile_cmd_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await add_user(user_id)
    is_joined, unjoined = await check_must_join(client, user_id)
    if not is_joined:
        return await message.reply_text(
            "⚠️ ফাইলটি পাওয়ার পূর্বে আমাদের চ্যানেলগুলোতে জয়েন করুন:",
            reply_markup=get_must_join_markup(unjoined)
        )
    # প্রথমে ক্যাটাগরি খুঁজবে, না পেলে স্পেশাল চ্যানেল খুঁজবে
    await handle_special_getfile_request(client, user_id, message)

# ---------------- /broadcast COMMAND ---------------- #
@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_handler(client: Client, message: Message):
    if not message.reply_to_message:
        return await message.reply_text(
            "⚠️ **যেকোনো মেসেজ/ছবি/ভিডিওতে রিপ্লাই দিয়ে কমান্ডটি দিতে হবে!**\n\n"
            "📝 **ব্যবহারের নিয়ম:**\n"
            "১. যে পোস্টটি সবাইকে পাঠাতে চান সেটি বটকে পাঠান বা ফরোয়ার্ড করুন।\n"
            "২. সেই পোস্টটির রিপ্লাইতে লিখুন: `/broadcast`"
        )

    status = await message.reply_text("🚀 ব্রডকাস্ট শুরু হচ্ছে...")
    total = 0
    successful = 0
    failed = 0

    broadcast_msg = message.reply_to_message

    async for user in users_col.find():
        uid = user["_id"]
        total += 1
        try:
            await broadcast_msg.copy(chat_id=uid)
            successful += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await broadcast_msg.copy(chat_id=uid)
            successful += 1
        except Exception:
            failed += 1

        if total % 25 == 0:
            await status.edit_text(f"🚀 ব্রডকাস্ট চলছে...\nমোট ইউজার: {total}\nসফল: {successful}\nব্যর্থ: {failed}")

    await status.edit_text(f"✅ **ব্রডকাস্ট সম্পন্ন হয়েছে!**\n\n👥 মোট ইউজার: {total}\n🎉 সফল: {successful}\n❌ ব্যর্থ: {failed}")

# ---------------- /setlink & /deletlink (SHORTENERS) ---------------- #
@app.on_message(filters.command("setlink") & filters.user(OWNER_ID))
async def setlink_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 3:
        return await message.reply_text(
            "⚠️ **ভুল কমান্ড ফরম্যাট!**\n\n"
            "📝 **ব্যবহারের নিয়ম:** `/setlink [domain] [api_key]`\n\n"
            "💡 **উদাহরণ:**\n"
            "• `/setlink droplink.co 98d6s8sd6f8sd6f`\n"
            "• `/setlink shareus.io your_api_token`"
        )
    domain = args[1].replace("https://", "").replace("http://", "").strip("/")
    api = args[2].strip()

    await shorteners_col.update_one(
        {"domain": domain},
        {"$set": {"domain": domain, "api": api}},
        upsert=True
    )
    await message.reply_text(f"✅ নতুন শর্টনার যুক্ত হয়েছে:\n🌐 ডোমেইন: `{domain}`\n🔑 এপিআই: `{api}`")

@app.on_message(filters.command("deletlink") & filters.user(OWNER_ID))
async def deletlink_handler(client: Client, message: Message):
    buttons = []
    async for s in shorteners_col.find():
        buttons.append([InlineKeyboardButton(f"❌ {s['domain']}", callback_data=f"delshort_{s['domain']}")])
    if not buttons:
        return await message.reply_text("⚠️ ডাটাবেজে কোনো শর্টনার লিংক নেই!\n💡 নতুন শর্টনার যোগ করতে লিখুন: `/setlink [domain] [api_key]`")
    await message.reply_text("🗑️ যে শর্টনারটি ডিলিট করতে চান বেছে নিন:", reply_markup=InlineKeyboardMarkup(buttons))

# ---------------- /mustjoine COMMAND ---------------- #
@app.on_message(filters.command("mustjoine") & filters.user(OWNER_ID))
async def mustjoine_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) >= 3:
        channel_name = " ".join(args[1:-1])
        try:
            channel_id = int(args[-1])
            chat = await client.get_chat(channel_id)
            inv_link = chat.invite_link or await client.export_chat_invite_link(channel_id)
            await mustjoin_col.update_one(
                {"channel_id": channel_id},
                {"$set": {"channel_id": channel_id, "name": channel_name, "invite_link": inv_link}},
                upsert=True
            )
            return await message.reply_text(f"✅ মাস্ট-জয়েন চ্যানেল যুক্ত হয়েছে:\n🏷️ নাম: `{channel_name}`\n🆔 আইডি: `{channel_id}`\n🔗 লিংক: {inv_link}")
        except Exception as e:
            return await message.reply_text(f"❌ চ্যানেল যুক্ত করা যায়নি: `{str(e)}`\nবটকে চ্যানেলে অ্যাডমিন বানিয়ে আবার চেষ্টা করুন!")

    buttons = []
    async for ch in mustjoin_col.find():
        buttons.append([
            InlineKeyboardButton(f"📢 {ch['name']}", url=ch["invite_link"]),
            InlineKeyboardButton("❌ মুছুন", callback_data=f"delmj_{ch['channel_id']}")
        ])
    if not buttons:
        return await message.reply_text(
            "⚠️ কোনো মাস্ট-জয়েন চ্যানেল যুক্ত নেই!\n\n"
            "📝 **যুক্ত করার নিয়ম:** `/mustjoine [চ্যানেল নাম] [চ্যানেল আইডি]`\n"
            "💡 **উদাহরণ:** `/mustjoine MovieChannel -1001234567890`"
        )
    await message.reply_text("📢 **মাস্ট-জয়েন চ্যানেল তালিকা ও ম্যানেজমেন্ট:**", reply_markup=InlineKeyboardMarkup(buttons))

# ---------------- /setcategory, /setlogo, /setvideo ---------------- #
@app.on_message(filters.command("setcategory") & filters.user(OWNER_ID))
async def setcat_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) >= 3:
        cat_name = " ".join(args[1:-1])
        clean_slug = slugify_command(cat_name)

        try:
            cid = int(args[-1])
            await categories_col.update_one(
                {"channel_id": cid},
                {"$set": {"name": cat_name, "cmd": clean_slug, "channel_id": cid}},
                upsert=True
            )
            # মেন্যুর কমান্ডগুলো আপডেট করে দেওয়া
            asyncio.create_task(sync_bot_commands(client))

            return await message.reply_text(
                f"✅ **ক্যাটাগরি এবং স্ল্যাশ কমান্ড সফলভাবে তৈরি হয়েছে!**\n\n"
                f"📁 নাম: `{cat_name}`\n"
                f"⚡ কমান্ড: `/{clean_slug}`\n"
                f"🆔 চ্যানেল আইডি: `{cid}`\n\n"
                f"👉 এখন থেকে ইউজাররা বাটনে ক্লিক করলেও পাবে, আবার সরাসরি `/{clean_slug}` লিখলেও ফাইল পাবে।"
            )
        except ValueError:
            return await message.reply_text("⚠️ চ্যানেল আইডি পূর্ণসংখ্যা হতে হবে!\n💡 উদাহরণ: `/setcategory getfile -1001234567890`")

    buttons = []
    async for cat in categories_col.find():
        buttons.append([
            InlineKeyboardButton(f"📁 {cat['name']}", callback_data=f"playcat_{cat['channel_id']}"),
            InlineKeyboardButton("❌ মুছুন", callback_data=f"delcat_{cat['channel_id']}")
        ])
    if not buttons:
        return await message.reply_text(
            "⚠️ কোনো ক্যাটাগরি তৈরি নেই!\n\n"
            "📝 **যুক্ত করার নিয়ম:** `/setcategory [নাম] [চ্যানেল আইডি]`\n\n"
            "💡 **উদাহরণ:**\n"
            "• `/setcategory getfile -1001234567890` (কমান্ড হবে `/getfile`)\n"
            "• `/setcategory নাটক -1009876543210`\n"
            "• `/setcategory Hindi_Movies -1001122334455` (কমান্ড হবে `/hindi_movies`)"
        )
    await message.reply_text("📁 **ক্যাটাগরি চ্যানেল তালিকা ও ম্যানেজমেন্ট:**", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_message(filters.command("setlogo") & filters.user(OWNER_ID))
async def setlogo_cmd(client: Client, message: Message):
    photo_id = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo_id = message.reply_to_message.photo.file_id
    elif len(message.command) > 1:
        photo_id = message.text.split(maxsplit=1)[1]

    if not photo_id:
        return await message.reply_text(
            "⚠️ **ছবির লিংক বা রিপ্লাই দেননি!**\n\n"
            "📝 **ব্যবহারের নিয়ম:**\n"
            "১. যেকোনো ছবির রিপ্লাই দিয়ে লিখুন: `/setlogo`\n"
            "২. অথবা সরাসরি ছবির ডিরেক্ট লিংক দিন: `/setlogo https://example.com/logo.jpg`"
        )
    await settings_col.update_one({"_id": "config"}, {"$set": {"logo": photo_id}}, upsert=True)
    await message.reply_text("✅ বটের স্টার্ট লোগো সফলভাবে সেট করা হয়েছে!")

@app.on_message(filters.command("setvideo") & filters.user(OWNER_ID))
async def setvideo_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply_text(
            "⚠️ **ভুল ফরম্যাট!**\n\n"
            "📝 **ব্যবহারের নিয়ম:** `/setvideo [বাটনের নাম] [লিংক]`\n\n"
            "💡 **উদাহরণ:**\n"
            "`/setvideo কীভাবে_দেখবেন https://t.me/your_tutorial`"
        )
    name = args[1]
    url = args[2]
    await settings_col.update_one({"_id": "config"}, {"$set": {"video_btn_name": name, "video_btn_url": url}}, upsert=True)
    await message.reply_text(f"✅ টিউটোরিয়াল ভিডিও বাটন সেট হয়েছে:\n🏷️ নাম: {name}\n🔗 লিংক: {url}")

# ---------------- /index COMMAND ---------------- #
@app.on_message(filters.command("index") & filters.user(OWNER_ID))
async def index_cmd(client: Client, message: Message):
    buttons = []
    async for cat in categories_col.find():
        buttons.append([InlineKeyboardButton(cat["name"], callback_data=f"idx_{cat['channel_id']}")])
    if not buttons:
        return await message.reply_text(
            "⚠️ আগে `/setcategory` দিয়ে ক্যাটাগরি চ্যানেল যুক্ত করুন!\n"
            "💡 উদাহরণ: `/setcategory Movies -1001234567890`"
        )
    await message.reply_text("📥 কোন চ্যানেলের ভিডিওগুলো ১, ২, ৩... ক্রমানুসারে ডাটাবেজে সাজিয়ে ইন্ডেক্স করতে চান?", reply_markup=InlineKeyboardMarkup(buttons))

# ---------------- DYNAMIC SLASH COMMANDS HANDLER ---------------- #
@app.on_message(filters.regex(r"^/[a-zA-Z0-9_]+") & filters.private)
async def dynamic_command_listener(client: Client, message: Message):
    cmd_text = message.text.split()[0][1:].lower()
    
    # বিল্টইন কমান্ডগুলো বাদ দেওয়া
    reserved = ["start", "help", "autodelet", "autodelete", "skip", "forward", 
                "addreedim", "reedim", "deletpremium", "premiumlist", "setadmin", 
                "setgetfile", "getfile", "broadcast", "setlink", "deletlink", 
                "mustjoine", "setcategory", "setlogo", "setvideo", "index", 
                "setparvideo", "setpervideo", "setcmd"]
    if cmd_text in reserved:
        return

    # ক্যাটাগরির সাথে কমান্ড ম্যাচ করা
    cat = await categories_col.find_one({"cmd": cmd_text})
    if not cat:
        cat = await categories_col.find_one({"name": {"$regex": f"^{cmd_text}$", "$options": "i"}})

    if cat:
        user_id = message.from_user.id
        await add_user(user_id)
        is_joined, unjoined = await check_must_join(client, user_id)
        if not is_joined:
            return await message.reply_text(
                "⚠️ ফাইল পেতে হলে আগে আমাদের চ্যানেলগুলোতে জয়েন করুন:",
                reply_markup=get_must_join_markup(unjoined)
            )
        return await handle_category_request(client, user_id, cat["channel_id"], message)

    await message.reply_text(
        f"⚠️ `/{cmd_text}` কমান্ডটি পাওয়া যায়নি!\n"
        f"💡 সকল কমান্ড ও উদাহরণ দেখতে লিখুন: `/help`"
    )

# ---------------- AUTO CHANNEL LISTENER ---------------- #
@app.on_message(filters.channel)
async def auto_post_listener(client: Client, message: Message):
    cat = await categories_col.find_one({"channel_id": message.chat.id})
    if cat:
        if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith("video")):
            count = await files_col.count_documents({"channel_id": message.chat.id})
            await files_col.update_one(
                {"channel_id": message.chat.id, "message_id": message.id},
                {"$set": {
                    "channel_id": message.chat.id,
                    "message_id": message.id,
                    "serial": count + 1,
                    "caption": message.caption or ""
                }},
                upsert=True
            )

# ---------------- CALLBACK QUERY HANDLER ---------------- #
@app.on_callback_query()
async def callback_router(client: Client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id

    # Must Join Recheck Button
    if data == "check_join_again":
        is_joined, unjoined = await check_must_join(client, user_id)
        if is_joined:
            return await start_handler(client, query.message)
        else:
            return await query.answer("❌ আপনি এখনো সব চ্যানেলে জয়েন করেননি! সবগুলোতে জয়েন করে আবার চাপ দিন।", show_alert=True)

    # Force Sub Check
    is_joined, unjoined = await check_must_join(client, user_id)
    if not is_joined:
        return await query.message.reply_text(
            "⚠️ আপনি চ্যানেলগুলো থেকে লিভ নিয়েছেন! বট ব্যবহার করতে আবার জয়েন করুন:",
            reply_markup=get_must_join_markup(unjoined)
        )

    # ক্যাটাগরি ডিলিট
    if data.startswith("delcat_"):
        cid = int(data.split("_")[1])
        await categories_col.delete_one({"channel_id": cid})
        await files_col.delete_many({"channel_id": cid})
        asyncio.create_task(sync_bot_commands(client))
        await query.answer("🗑️ ক্যাটাগরি ও এর ভিডিও রেকর্ড মুছে ফেলা হয়েছে!", show_alert=True)
        return await query.message.delete()

    # মাস্ট-জয়েন ডিলিট
    elif data.startswith("delmj_"):
        cid = int(data.split("_")[1])
        await mustjoin_col.delete_one({"channel_id": cid})
        await query.answer("🗑️ চ্যানেলটি মাস্ট-জয়েন থেকে মুছে দেওয়া হয়েছে!", show_alert=True)
        return await query.message.delete()

    # শর্টনার ডিলিট
    elif data.startswith("delshort_"):
        domain = data.split("_")[1]
        await shorteners_col.delete_one({"domain": domain})
        await query.answer(f"🗑️ শর্টনার `{domain}` ডিলিট করা হয়েছে!", show_alert=True)
        return await query.message.delete()

    # স্কিপ পয়েন্ট সেট করা
    elif data.startswith("setskip_"):
        _, cid_str, pos_str = data.split("_")
        cid = int(cid_str)
        pos = int(pos_str)
        await users_col.update_one({"_id": user_id}, {"$set": {f"video_pointer.{cid}": pos}})
        await query.answer("✅ আপনার স্কিপ সফলভাবে সেট হয়েছে!", show_alert=True)
        await query.message.reply_text(f"🎯 আপনার স্কিপ পয়েন্ট সেট করা হয়েছে **{pos}** নম্বর ভিডিওতে।")
        return await handle_category_request(client, user_id, cid, query)

    # ক্যাটাগরি বাটন থেকে ভিডিও প্লে (স্টার্ট মেন্যুর বাটন কখনই ডিলিট হবে না)
    elif data.startswith("playcat_"):
        cid = int(data.split("_")[1])
        await query.answer("ফাইল প্রস্তুত করা হচ্ছে...")
        return await handle_category_request(client, user_id, cid, query)

    # স্পেশাল গেটফাইল বাটন
    elif data == "special_getfile":
        await query.answer("ফাইল প্রস্তুত করা হচ্ছে...")
        return await handle_special_getfile_request(client, user_id, query)

    # চ্যানেল ইন্ডেক্সিং
    elif data.startswith("idx_"):
        cid = int(data.split("_")[1])
        status = await query.message.reply_text("⏳ চ্যানেলের ফাইলগুলো ক্রমানুসারে সাজিয়ে ডাটাবেজে ইন্ডেক্স করা হচ্ছে, অপেক্ষা করুন...")
        total_indexed = 0

        await files_col.delete_many({"channel_id": cid})

        all_posts = []
        async for post in client.get_chat_history(cid):
            if post.video or (post.document and post.document.mime_type and post.document.mime_type.startswith("video")):
                all_posts.append(post)

        all_posts.reverse()

        for idx, post in enumerate(all_posts, start=1):
            await files_col.update_one(
                {"channel_id": cid, "message_id": post.id},
                {"$set": {
                    "channel_id": cid,
                    "message_id": post.id,
                    "serial": idx,
                    "caption": post.caption or ""
                }},
                upsert=True
            )
            total_indexed += 1

        await status.edit_text(f"✅ মোট **{total_indexed}** টি ফাইল ১ থেকে {total_indexed} পর্যন্ত সিরিয়াল অনুযায়ী ইন্ডেক্স সম্পন্ন হয়েছে!")

# ---------------- MAIN APPLICATION ENTRY POINT ---------------- #
async def main():
    print("--------------------------------------------------")
    print("⏳ Testing MongoDB Connection...")
    try:
        await mongo_client.admin.command('ping')
        print("✅ MongoDB Connected Successfully!")
    except Exception as e:
        print(f"❌ MongoDB Connection Failed! Check IP Whitelist (0.0.0.0/0) in Atlas. Error: {e}")
        return

    print("⚡ Checking and deleting active Webhooks...")
    await remove_webhook_if_exists()

    print("⚡ Starting Koyeb / Render AioHTTP Port Server...")
    await web_server()
    print(f"✅ Web Server active on port {PORT}")

    print("⚡ Starting Telegram Bot Client...")
    await app.start()
    bot_info = await app.get_me()
    print(f"🤖 Bot is Live: @{bot_info.username}")
    
    # বট রান হওয়ার সাথে সাথে মেন্যু কমান্ড সিঙ্ক হবে
    await sync_bot_commands(app)
    print("--------------------------------------------------")
    
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
