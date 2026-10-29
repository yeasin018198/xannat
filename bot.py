import os
import sys
import uuid
import secrets
import asyncio
from datetime import datetime, timedelta
import aiohttp
from aiohttp import web
from pyrogram import Client, filters
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
    CallbackQuery
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
mongo_client = AsyncIOMotorClient(MONGO_URI)
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
    bot_token=BOT_TOKEN
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
            "auto_delete_time": 0
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

# ---------------- AUTO-DELETE BACKGROUND TASK ---------------- #
async def auto_delete_task(client: Client, chat_id: int, message_id: int, delay_sec: int, notice_id: int = None):
    await asyncio.sleep(delay_sec)
    try:
        await client.delete_messages(chat_id=chat_id, message_ids=message_id)
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
async def generate_shortlink(user_id: int, bot_username: str):
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

# ---------------- /start COMMAND ---------------- #
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user = message.from_user
    await add_user(user.id)
    settings = await get_settings()

    # 1. Single-Use Verification Token Check
    if len(message.command) > 1 and message.command[1].startswith("verify_"):
        token = message.command[1].split("_")[1]
        token_doc = await tokens_col.find_one({"_id": token})

        if not token_doc:
            return await message.reply_text("❌ অবৈধ বা ভুল ভেরিফিকেশন লিংক!")
        if token_doc.get("used"):
            return await message.reply_text("❌ এই লিংকটি ইতিমধ্যে ব্যবহার করা হয়েছে! প্রতিবার ফাইল নেওয়ার জন্য নতুন লিংক প্রয়োজন।")
        if token_doc.get("user_id") != user.id:
            return await message.reply_text("❌ এই ভেরিফিকেশন লিংকটি অন্য কোনো ইউজারের জন্য তৈরি করা হয়েছে!")

        # Grant 24 Hours Access
        await tokens_col.update_one({"_id": token}, {"$set": {"used": True}})
        verified_until = datetime.utcnow() + timedelta(hours=24)
        await users_col.update_one({"_id": user.id}, {"$set": {"verified_until": verified_until}})
        return await message.reply_text("🎉 **অভিনন্দন!**\nআপনার ভেরিফিকেশন সফল হয়েছে। আগামী ২৪ ঘণ্টার জন্য আপনি সকল ভিডিও আনলিমিটেড দেখতে পারবেন।")

    # 2. Must-Join Channel Check
    is_joined, unjoined = await check_must_join(client, user.id)
    if not is_joined:
        return await message.reply_text(
            "⚠️ **বটটি ব্যবহার করতে হলে আপনাকে আমাদের নিচের চ্যানেলগুলোতে জয়েন করতে হবে!**\nসবগুলোতে জয়েন করে 'Try Again' বাটনে চাপ দিন:",
            reply_markup=get_must_join_markup(unjoined)
        )

    # 3. Main Start Message Display
    caption = (
        f"👋 **স্বাগতম, {user.first_name}!**\n\n"
        f"👤 **আপনার প্রোফাইল তথ্য:**\n"
        f"├ **নাম:** {user.first_name} {user.last_name or ''}\n"
        f"├ **ইউজারনেম:** @{user.username if user.username else 'N/A'}\n"
        f"└ **ইউজার আইডি:** `{user.id}`\n\n"
        f"⭐ **স্ট্যাটাস:** {'💎 প্রিমিয়াম মেম্বার' if await is_premium_user(user.id) else '🆓 সাধারণ ইউজার'}\n\n"
        f"📂 নিচের বাটন থেকে আপনার প্রয়োজনীয় ফাইল বা ভিডিও বেছে নিন:"
    )

    buttons = []

    # GetFile Button - Always placed at the VERY TOP
    if settings.get("getfile_channel"):
        buttons.append([InlineKeyboardButton("🌟 স্পেশাল ফাইল (Get Files)", callback_data="special_getfile")])

    # Dynamic Category Buttons
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

# ---------------- /autodelet COMMAND ---------------- #
@app.on_message(filters.command(["autodelet", "autodelete"]) & filters.user(OWNER_ID))
async def autodelete_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        settings = await get_settings()
        curr = settings.get("auto_delete_time", 0)
        status = f"⏱️ সেট করা আছে: **{curr} সেকেন্ড**" if curr > 0 else "❌ বর্তমানে অটো-ডিলিট ফিচার **বন্ধ** আছে।"
        return await message.reply_text(
            f"{status}\n\n"
            f"⚠️ **ব্যবহারের নিয়ম:**\n"
            f"• সেট করতে: `/autodelet 30` অথবা `/autodelet 30 সেকেন্ড`\n"
            f"• বন্ধ করতে: `/autodelet 0` বা `/autodelet off`"
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
        return await message.reply_text("⚠️ সেকেন্ড সংখ্যায় দিন! যেমন: `/autodelet 30`")

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
        return await message.reply_text("⚠️ **ব্যবহারের নিয়ম:** `/skip [ভিডিও নাম্বার]`\nউদাহরণ: `/skip 30` (৩০ নম্বর ভিডিও থেকে দেখতে)")

    target_pos = int(args[1])
    buttons = []
    async for cat in categories_col.find():
        buttons.append([InlineKeyboardButton(cat["name"], callback_data=f"setskip_{cat['channel_id']}_{target_pos}")])

    if not buttons:
        return await message.reply_text("⚠️ ডাটাবেজে কোনো ক্যাটাগরি তৈরি করা নেই!")

    await message.reply_text(
        f"📌 আপনি **{target_pos}** নাম্বার ভিডিও থেকে দেখতে চেয়েছেন।\nকোন ক্যাটাগরিতে এই স্কিপ সেট করতে চান বেছে নিন:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ---------------- /forward COMMAND ---------------- #
@app.on_message(filters.command("forward") & filters.user(OWNER_ID))
async def forward_toggle(client: Client, message: Message):
    settings = await get_settings()
    current = settings.get("protect_content", False)
    new_state = not current
    await settings_col.update_one({"_id": "config"}, {"$set": {"protect_content": new_state}}, upsert=True)
    status_text = "🔒 অন (ফাইল ফরওয়ার্ড বন্ধ)" if new_state else "🔓 অফ (ফাইল ফরওয়ার্ড উন্মুক্ত)"
    await message.reply_text(f"✅ ফরোয়ার্ড রেস্ট্রিকশন এখন: **{status_text}**")

# ---------------- REDEEM & PREMIUM SYSTEM ---------------- #
@app.on_message(filters.command("addreedim") & filters.user(OWNER_ID))
async def addreedim_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        return await message.reply_text("⚠️ **নিয়ম:** `/addreedim [মেয়াদ মিনিটে] [কোডের সংখ্যা]`\nউদাহরণ: `/addreedim 1440 5`")

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
        return await message.reply_text("⚠️ আপনার রিডিম কোডটি দিন। যেমন: `/reedim PREM-XXXXXXXX`")

    code = args[1].strip()
    doc = await redeem_col.find_one({"_id": code})

    if not doc:
        return await message.reply_text("❌ ভুল বা অস্তিত্বহীন রিডিম কোড!")
    if doc.get("used"):
        return await message.reply_text("❌ এই রিডিম কোডটি আগেই ব্যবহার করা হয়েছে!")

    duration = doc.get("duration_minutes", 10)
    user = await users_col.find_one({"_id": user_id})
    current_expiry = user.get("premium_expiry")

    start_point = current_expiry if (current_expiry and current_expiry > datetime.utcnow()) else datetime.utcnow()
    new_expiry = start_point + timedelta(minutes=duration)

    await users_col.update_one({"_id": user_id}, {"$set": {"premium_expiry": new_expiry}})
    await redeem_col.update_one({"_id": code}, {"$set": {"used": True, "used_by": user_id, "used_at": datetime.utcnow()}})

    await message.reply_text(f"🎉 অভিনন্দন! আপনার প্রিমিয়াম অ্যাক্টিভ হয়েছে।\n⏰ মেয়াদ: `{new_expiry.strftime('%Y-%m-%d %H:%M:%S UTC')}` পর্যন্ত।")

@app.on_message(filters.command("deletpremium") & filters.user(OWNER_ID))
async def delete_premium_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply_text("⚠️ **নিয়ম:** `/deletpremium [ইউজার আইডি]`")
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
        return await message.reply_text("⚠️ অ্যাডমিনের ইউজারনেম দিন। যেমন: `/setadmin @YourUsername`")
    adm = args[1].strip()
    await settings_col.update_one({"_id": "config"}, {"$set": {"admin_contact": adm}}, upsert=True)
    await message.reply_text(f"✅ প্রিমিয়াম কেনার জন্য অ্যাডমিন কন্টাক্ট সেট হয়েছে: `{adm}`")

# ---------------- /setgetfile & /getfile ---------------- #
@app.on_message(filters.command("setgetfile") & filters.user(OWNER_ID))
async def setgetfile_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.reply_text("⚠️ চ্যানেলের আইডি দিন। যেমন: `/setgetfile -100123456789`")
    try:
        chid = int(args[1])
        await settings_col.update_one({"_id": "config"}, {"$set": {"getfile_channel": chid}}, upsert=True)
        await message.reply_text(f"✅ GetFile স্পেশাল চ্যানেল সেট করা হয়েছে: `{chid}`")
    except ValueError:
        await message.reply_text("⚠️ চ্যানেল আইডি অবশ্যই পূর্ণসংখ্যা (Integer) হতে হবে!")

# ---------------- /broadcast COMMAND ---------------- #
@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_handler(client: Client, message: Message):
    if not message.reply_to_message:
        return await message.reply_text("⚠️ যেকোনো মেসেজ, বাটন, ছবি বা ভিডিওতে রিপ্লাই দিয়ে `/broadcast` লিখুন।")

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
        return await message.reply_text("⚠️ **নিয়ম:** `/setlink [domain] [api_key]`\nউদাহরণ: `/setlink droplink.co 98d6s8sd6f8sd6f`")
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
        return await message.reply_text("⚠️ ডাটাবেজে কোনো শর্টনার লিংক নেই!")
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
        return await message.reply_text("⚠️ কোনো মাস্ট-জয়েন চ্যানেল নেই!\nযুক্ত করতে লিখুন:\n`/mustjoine [চ্যানেল নাম] [চ্যানেল আইডি]`")
    await message.reply_text("📢 **মাস্ট-জয়েন চ্যানেল তালিকা ও ম্যানেজমেন্ট:**", reply_markup=InlineKeyboardMarkup(buttons))

# ---------------- /setcategory, /setlogo, /setvideo ---------------- #
@app.on_message(filters.command("setcategory") & filters.user(OWNER_ID))
async def setcat_handler(client: Client, message: Message):
    args = message.text.split()
    if len(args) >= 3:
        cat_name = " ".join(args[1:-1])
        try:
            cid = int(args[-1])
            await categories_col.update_one(
                {"channel_id": cid},
                {"$set": {"name": cat_name, "channel_id": cid}},
                upsert=True
            )
            return await message.reply_text(f"✅ ক্যাটাগরি তৈরি হয়েছে:\n📁 `{cat_name}`\n🆔 `{cid}`")
        except ValueError:
            return await message.reply_text("⚠️ চ্যানেল আইডি পূর্ণসংখ্যা হতে হবে!")

    buttons = []
    async for cat in categories_col.find():
        buttons.append([
            InlineKeyboardButton(f"📁 {cat['name']}", callback_data=f"playcat_{cat['channel_id']}"),
            InlineKeyboardButton("❌ মুছুন", callback_data=f"delcat_{cat['channel_id']}")
        ])
    if not buttons:
        return await message.reply_text("⚠️ কোনো ক্যাটাগরি নেই!\nএড করতে লিখুন:\n`/setcategory [নাম] [চ্যানেল আইডি]`")
    await message.reply_text("📁 **ক্যাটাগরি চ্যানেল তালিকা ও ম্যানেজমেন্ট:**", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_message(filters.command("setlogo") & filters.user(OWNER_ID))
async def setlogo_cmd(client: Client, message: Message):
    photo_id = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo_id = message.reply_to_message.photo.file_id
    elif len(message.command) > 1:
        photo_id = message.text.split(maxsplit=1)[1]
    if not photo_id:
        return await message.reply_text("⚠️ একটি ছবির রিপ্লাই দিয়ে `/setlogo` লিখুন অথবা সরাসরি ছবির লিংক দিন।")
    await settings_col.update_one({"_id": "config"}, {"$set": {"logo": photo_id}}, upsert=True)
    await message.reply_text("✅ বটের স্টার্ট লোগো সফলভাবে সেট করা হয়েছে!")

@app.on_message(filters.command("setvideo") & filters.user(OWNER_ID))
async def setvideo_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply_text("⚠️ **নিয়ম:** `/setvideo [বাটনের নাম] [লিংক]`")
    name = args[1]
    url = args[2]
    await settings_col.update_one({"_id": "config"}, {"$set": {"video_btn_name": name, "video_btn_url": url}}, upsert=True)
    await message.reply_text(f"✅ টিউটোরিয়াল ভিডিও বাটন সেট হয়েছে:\n🏷️ {name}\n🔗 {url}")

# ---------------- /index COMMAND ---------------- #
@app.on_message(filters.command("index") & filters.user(OWNER_ID))
async def index_cmd(client: Client, message: Message):
    buttons = []
    async for cat in categories_col.find():
        buttons.append([InlineKeyboardButton(cat["name"], callback_data=f"idx_{cat['channel_id']}")])
    if not buttons:
        return await message.reply_text("⚠️ আগে `/setcategory` দিয়ে ক্যাটাগরি চ্যানেল যুক্ত করুন!")
    await message.reply_text("📥 কোন চ্যানেলের ভিডিওগুলো ১, ২, ৩... ক্রমানুসারে সাজিয়ে ডাটাবেজে ইন্ডেক্স করতে চান?", reply_markup=InlineKeyboardMarkup(buttons))

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

# ---------------- SEQUENTIAL VIDEO PLAYER & DELIVERY ---------------- #
async def send_serial_video(client: Client, user_id: int, channel_id: int, serial_num: int):
    settings = await get_settings()
    protect = settings.get("protect_content", False)
    auto_del = settings.get("auto_delete_time", 0)

    total_videos = await files_col.count_documents({"channel_id": channel_id})
    if total_videos == 0:
        return await client.send_message(user_id, "⚠️ এই ক্যাটাগরিতে এখনো কোনো ভিডিও ইন্ডেক্স করা হয়নি!")

    if serial_num < 1:
        serial_num = 1
    if serial_num > total_videos:
        serial_num = total_videos

    # ইউজারের পয়েন্টার সংরক্ষণ করা (যাতে বারবার স্কিপ না হয়)
    await users_col.update_one({"_id": user_id}, {"$set": {f"video_pointer.{channel_id}": serial_num}})

    video_doc = await files_col.find_one({"channel_id": channel_id, "serial": serial_num})
    if not video_doc:
        return await client.send_message(user_id, "❌ ভিডিওটি ডাটাবেজে পাওয়া যায়নি!")

    # নেভিগেশন কন্ট্রোল বাটন
    buttons = []
    nav_row = []
    if serial_num > 1:
        nav_row.append(InlineKeyboardButton("⬅️ পূর্ববর্তী", callback_data=f"seq_{channel_id}_{serial_num - 1}"))
    nav_row.append(InlineKeyboardButton(f"🎬 {serial_num}/{total_videos}", callback_data=f"show_curr_{serial_num}"))
    if serial_num < total_videos:
        nav_row.append(InlineKeyboardButton("পরবর্তী ➡️", callback_data=f"seq_{channel_id}_{serial_num + 1}"))

    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton("🔙 মূল মেন্যু", callback_data="back_start")])

    try:
        sent_msg = await client.copy_message(
            chat_id=user_id,
            from_chat_id=channel_id,
            message_id=video_doc["message_id"],
            reply_markup=InlineKeyboardMarkup(buttons),
            protect_content=protect
        )

        # অটো ডিলিট টাইমার কার্যকর করা
        if auto_del and auto_del > 0:
            notice = await client.send_message(
                chat_id=user_id,
                text=f"⏳ **সতর্কবার্তা:** এই ভিডিওটি আগামী **{auto_del} সেকেন্ড** পর স্বয়ংক্রিয়ভাবে মুছে যাবে!"
            )
            asyncio.create_task(auto_delete_task(client, user_id, sent_msg.id, auto_del, notice.id))

    except Exception as e:
        await client.send_message(user_id, f"❌ ভিডিও পাঠাতে ব্যর্থ: `{str(e)}`")

# ---------------- CALLBACK QUERY HANDLER ---------------- #
@app.on_callback_query()
async def callback_router(client: Client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    settings = await get_settings()
    protect = settings.get("protect_content", False)
    auto_del = settings.get("auto_delete_time", 0)

    # Must Join Recheck Button
    if data == "check_join_again":
        is_joined, unjoined = await check_must_join(client, user_id)
        if is_joined:
            await query.message.delete()
            return await start_handler(client, query.message)
        else:
            return await query.answer("❌ আপনি এখনো সব চ্যানেলে জয়েন করেননি! সবগুলোতে জয়েন করে আবার চাপ দিন।", show_alert=True)

    # Strict Force-Sub Check
    is_joined, unjoined = await check_must_join(client, user_id)
    if not is_joined:
        return await query.message.edit_text(
            "⚠️ আপনি চ্যানেলগুলো থেকে লিভ নিয়েছেন! বট ব্যবহার করতে আবার জয়েন করুন:",
            reply_markup=get_must_join_markup(unjoined)
        )

    # ক্যাটাগরি ডিলিট
    if data.startswith("delcat_"):
        cid = int(data.split("_")[1])
        await categories_col.delete_one({"channel_id": cid})
        await files_col.delete_many({"channel_id": cid})
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
        await query.message.edit_text(f"🎯 আপনার স্কিপ পয়েন্ট সেট করা হয়েছে **{pos}** নম্বর ভিডিওতে।")
        return await send_serial_video(client, user_id, cid, pos)

    # ক্যাটাগরিতে ক্লিক করে প্লে করা
    elif data.startswith("playcat_"):
        cid = int(data.split("_")[1])

        # শর্টলিংক যাচাই (নন-প্রিমিয়ামদের জন্য)
        if not await is_verified_user(user_id):
            me = await client.get_me()
            short_url, _ = await generate_shortlink(user_id, me.username)
            if short_url:
                btn = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔓 আনলক লিংক (এক ক্লিকে ভেরিফাই)", url=short_url)],
                    [InlineKeyboardButton("💎 সরাসরি প্রিমিয়াম নিন (বিজ্ঞাপনহীন)", url=f"https://t.me/{settings['admin_contact'].replace('@', '')}")]
                ])
                return await query.message.reply_text(
                    "🔒 **ফাইলটি দেখতে হলে ছোট একটি ভেরিফিকেশন সম্পন্ন করুন!**\n\n"
                    "নিচের লিংকে গিয়ে ভেরিফাই করলে ২৪ ঘণ্টার জন্য সকল ভিডিও সম্পূর্ণ ফ্রিতে দেখতে পারবেন।\n"
                    "⚠️ *প্রতিটি লিংক কেবল একবারই কার্যকর। ২ বার ক্লিক করলে ফাইল পাবেন না।*",
                    reply_markup=btn
                )

        user_doc = await users_col.find_one({"_id": user_id})
        current_pointer = user_doc.get("video_pointer", {}).get(str(cid), 1)
        await query.message.delete()
        return await send_serial_video(client, user_id, cid, current_pointer)

    # আগের / পরের ভিডিও নেভিগেশন
    elif data.startswith("seq_"):
        _, cid_str, next_serial_str = data.split("_")
        cid = int(cid_str)
        next_serial = int(next_serial_str)
        return await send_serial_video(client, user_id, cid, next_serial)

    # বর্তমান সিরিয়াল প্রদর্শন
    elif data.startswith("show_curr_"):
        curr = data.split("_")[2]
        return await query.answer(f"আপনি বর্তমানে {curr} নম্বর ভিডিওতে আছেন।", show_alert=False)

    # স্পেশাল /getfile বাটন হ্যান্ডলার
    elif data == "special_getfile":
        getfile_cid = settings.get("getfile_channel")
        if not getfile_cid:
            return await query.answer("⚠️ GetFile চ্যানেল এখনো সেট করা হয়নি!", show_alert=True)
        
        try:
            async for post in client.get_chat_history(getfile_cid, limit=1):
                sent_msg = await post.copy(chat_id=user_id, protect_content=protect)
                if auto_del and auto_del > 0:
                    notice = await client.send_message(
                        chat_id=user_id,
                        text=f"⏳ **সতর্কবার্তা:** এই ফাইলটি আগামী **{auto_del} সেকেন্ড** পর স্বয়ংক্রিয়ভাবে মুছে যাবে!"
                    )
                    asyncio.create_task(auto_delete_task(client, user_id, sent_msg.id, auto_del, notice.id))
            await query.answer("✅ স্পেশাল ফাইল আপনার ইনবক্সে পাঠানো হয়েছে!")
        except Exception as e:
            await query.answer(f"❌ এরর: {str(e)}", show_alert=True)

    # চ্যানেল ইন্ডেক্সিং এক্সিকিউশন
    elif data.startswith("idx_"):
        cid = int(data.split("_")[1])
        status = await query.message.edit_text("⏳ চ্যানেলের ভিডিওগুলো ক্রমানুসারে সাজিয়ে ডাটাবেজে ইন্ডেক্স করা হচ্ছে, অপেক্ষা করুন...")
        total_indexed = 0

        # পুরাতন ইনডেক্স ক্লিন করা যাতে ফ্রেশ সিরিয়াল ১ থেকে বসে
        await files_col.delete_many({"channel_id": cid})

        all_posts = []
        async for post in client.get_chat_history(cid):
            if post.video or (post.document and post.document.mime_type and post.document.mime_type.startswith("video")):
                all_posts.append(post)

        # প্রথম পোস্টকে ১ নম্বর দেওয়ার জন্য লিস্ট রিভার্স করা
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

        await status.edit_text(f"✅ সফলভাবে মোট **{total_indexed}** টি ভিডিও ১ থেকে {total_indexed} পর্যন্ত সিরিয়াল অনুযায়ী ইন্ডেক্স সম্পন্ন হয়েছে!")

    # ব্যাক টু হোম
    elif data == "back_start":
        await query.message.delete()
        await start_handler(client, query.message)

# ---------------- MAIN APPLICATION ENTRY POINT ---------------- #
async def main():
    print("--------------------------------------------------")
    print("⚡ Starting Koyeb / Render AioHTTP Port Server...")
    await web_server()
    print(f"✅ Web Server active on port {PORT}")
    print("⚡ Starting Telegram Bot Client...")
    await app.start()
    bot_info = await app.get_me()
    print(f"🤖 Bot is Live: @{bot_info.username}")
    print("--------------------------------------------------")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
