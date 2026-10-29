import os
import asyncio
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    Message, 
    CallbackQuery
)
from motor.motor_asyncio import AsyncIOMotorClient

# ---------------- CONFIGURATION / ENV VARIABLES ---------------- #
API_ID = int(os.environ.get("API_ID", "1234567"))          # my.telegram.org থেকে পাবেন
API_HASH = os.environ.get("API_HASH", "your_api_hash")      # my.telegram.org থেকে পাবেন
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")  # BotFather থেকে পাবেন
OWNER_ID = int(os.environ.get("OWNER_ID", "12345678"))      # আপনার Telegram User ID
MONGO_URI = os.environ.get("MONGO_URI", "your_mongodb_uri") # MongoDB Connection URL
PORT = int(os.environ.get("PORT", "8080"))                  # Koyeb/Render এর পোর্ট

# ---------------- DATABASE SETUP ---------------- #
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["FileStoreBot"]
settings_col = db["settings"]
categories_col = db["categories"]
files_col = db["files"]

# ---------------- BOT INITIALIZATION ---------------- #
app = Client(
    "FileStoreBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ---------------- WEB SERVER (For Koyeb / Render) ---------------- #
async def web_server():
    async def handle_ping(request):
        return web.Response(text="Bot is running smoothly!")
    app_runner = web.Application()
    app_runner.router.add_get("/", handle_ping)
    runner = web.AppRunner(app_runner)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ---------------- HELPER FUNCTIONS ---------------- #
async def get_settings():
    settings = await settings_col.find_one({"_id": "config"})
    if not settings:
        default = {
            "_id": "config",
            "logo": None,
            "video_btn_name": "ভিডিও টিউটোরিয়াল",
            "video_btn_url": "https://t.me"
        }
        await settings_col.insert_one(default)
        return default
    return settings

# ---------------- /start COMMAND ---------------- #
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user = message.from_user
    settings = await get_settings()

    caption = (
        f"👋 **স্বাগতম, {user.first_name}!**\n\n"
        f"👤 **আপনার তথ্য:**\n"
        f"├ **নাম:** {user.first_name} {user.last_name or ''}\n"
        f"├ **ইউজারনেম:** @{user.username if user.username else 'N/A'}\n"
        f"└ **ইউজার আইডি:** `{user.id}`\n\n"
        f"📂 নিচের বাটনগুলো থেকে আপনার পছন্দের ক্যাটাগরি সিলেক্ট করুন:"
    )

    # Dynamic Buttons
    buttons = []
    
    # 1. Categories Buttons
    async for cat in categories_col.find():
        buttons.append([InlineKeyboardButton(f"📁 {cat['name']}", callback_data=f"cat_{cat['channel_id']}_0")])

    # 2. Tutorial Video Button
    if settings.get("video_btn_url"):
        buttons.append([InlineKeyboardButton(f"🎥 {settings.get('video_btn_name', 'ভিডিও ডাউনলোড করুন')}", url=settings.get("video_btn_url"))])

    keyboard = InlineKeyboardMarkup(buttons)

    # Send Photo or Text
    if settings.get("logo"):
        try:
            await message.reply_photo(photo=settings["logo"], caption=caption, reply_markup=keyboard)
            return
        except Exception:
            pass
    await message.reply_text(text=caption, reply_markup=keyboard)

# ---------------- /setvideo COMMAND ---------------- #
@app.on_message(filters.command("setvideo") & filters.user(OWNER_ID))
async def setvideo_handler(client: Client, message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply_text(
            "⚠️ **ব্যবহারের নিয়ম:**\n`/setvideo [বাটনের নাম] [লিংক]`\n\n"
            "**উদাহরণ:**\n`/setvideo ডাউনলোড টিউটোরিয়াল https://t.me/your_channel`"
        )
    btn_name = args[1]
    btn_url = args[2]

    await settings_col.update_one(
        {"_id": "config"},
        {"$set": {"video_btn_name": btn_name, "video_btn_url": btn_url}},
        upsert=True
    )
    await message.reply_text(f"✅ সফলভাবে টিউটোরিয়াল বাটন সেট হয়েছে:\n🏷️ নাম: `{btn_name}`\n🔗 লিংক: `{btn_url}`")

# ---------------- /setlogo COMMAND ---------------- #
@app.on_message(filters.command("setlogo") & filters.user(OWNER_ID))
async def setlogo_handler(client: Client, message: Message):
    photo_id = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo_id = message.reply_to_message.photo.file_id
    elif len(message.command) > 1:
        photo_id = message.text.split(maxsplit=1)[1]

    if not photo_id:
        return await message.reply_text("⚠️ একটি ছবির রিপ্লাই দিয়ে `/setlogo` লিখুন অথবা সরাসরি ইমেজ URL দিন।")

    await settings_col.update_one(
        {"_id": "config"},
        {"$set": {"logo": photo_id}},
        upsert=True
    )
    await message.reply_text("✅ বটের লোগো সফলভাবে সেট করা হয়েছে!")

# ---------------- /setcategory COMMAND ---------------- #
@app.on_message(filters.command("setcategory") & filters.user(OWNER_ID))
async def setcategory_handler(client: Client, message: Message):
    args = message.text.split()
    
    # Argument দিলে সরাসরি চ্যানেল এড হবে
    if len(args) >= 3:
        btn_name = " ".join(args[1:-1])
        try:
            channel_id = int(args[-1])
        except ValueError:
            return await message.reply_text("⚠️ চ্যানেল আইডি সংখ্যা (Integer) হতে হবে!")

        await categories_col.update_one(
            {"channel_id": channel_id},
            {"$set": {"name": btn_name, "channel_id": channel_id}},
            upsert=True
        )
        return await message.reply_text(f"✅ নতুন ক্যাটাগরি যুক্ত হয়েছে:\n🏷️ নাম: `{btn_name}`\n🆔 আইডি: `{channel_id}`")

    # কোনো আর্গুমেন্ট না দিলে ম্যানেজ মেন্যু আসবে
    buttons = []
    async for cat in categories_col.find():
        buttons.append([
            InlineKeyboardButton(f"📁 {cat['name']}", callback_data=f"showcat_{cat['channel_id']}"),
            InlineKeyboardButton("❌ মুছুন", callback_data=f"delcat_{cat['channel_id']}")
        ])
    
    if not buttons:
        return await message.reply_text(
            "⚠️ বর্তমানে কোনো ক্যাটাগরি এড নেই।\n\n"
            "এড করতে লিখুন:\n`/setcategory [ক্যাটাগরি নাম] [চ্যানেল আইডি]`"
        )
    
    buttons.append([InlineKeyboardButton("➕ নতুন ক্যাটাগরি নিয়ম", callback_data="cat_help")])
    await message.reply_text("📂 **ক্যাটাগরি তালিকা ও ম্যানেজমেন্ট:**", reply_markup=InlineKeyboardMarkup(buttons))

# ---------------- /index COMMAND ---------------- #
@app.on_message(filters.command("index") & filters.user(OWNER_ID))
async def index_handler(client: Client, message: Message):
    categories = []
    async for cat in categories_col.find():
        categories.append([InlineKeyboardButton(cat["name"], callback_data=f"idxcat_{cat['channel_id']}")])

    if not categories:
        return await message.reply_text("⚠️ আগে `/setcategory` দিয়ে ক্যাটাগরি চ্যানেল যুক্ত করুন!")

    await message.reply_text("📥 কোন ক্যাটাগরি চ্যানেলের ফাইল ইন্ডেক্স করতে চান সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(categories))

# ---------------- AUTO CHANNEL LISTENER ---------------- #
# চ্যানেলগুলোতে নতুন কোনো পোস্ট আসলে বট স্বয়ংক্রিয়ভাবে ডাটাবেজে সংরক্ষণ করে নেবে
@app.on_message(filters.channel)
async def channel_post_listener(client: Client, message: Message):
    cat = await categories_col.find_one({"channel_id": message.chat.id})
    if cat:
        await files_col.update_one(
            {"channel_id": message.chat.id, "message_id": message.id},
            {"$set": {
                "channel_id": message.chat.id,
                "message_id": message.id,
                "text": message.caption or message.text or ""
            }},
            upsert=True
        )

# ---------------- CALLBACK QUERY HANDLER ---------------- #
@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    data = query.data

    if data == "cat_help":
        await query.answer(
            "ক্যাটাগরি এড করতে লিখুন:\n/setcategory [বাটনের নাম] [চ্যানেল আইডি]", 
            show_alert=True
        )

    # ক্যাটাগরি ডিলিট
    elif data.startswith("delcat_"):
        cid = int(data.split("_")[1])
        await categories_col.delete_one({"channel_id": cid})
        await files_col.delete_many({"channel_id": cid})
        await query.answer("🗑️ ক্যাটাগরি ডিলিট করা হয়েছে!", show_alert=True)
        await query.message.delete()

    # ইন্ডেক্সিং প্রসেস
    elif data.startswith("idxcat_"):
        cid = int(data.split("_")[1])
        status = await query.message.edit_text("⏳ ইন্ডেক্সিং শুরু হচ্ছে, অনুগ্রহ করে অপেক্ষা করুন...")
        total = 0
        try:
            async for post in client.get_chat_history(cid):
                await files_col.update_one(
                    {"channel_id": cid, "message_id": post.id},
                    {"$set": {
                        "channel_id": cid,
                        "message_id": post.id,
                        "text": post.caption or post.text or ""
                    }},
                    upsert=True
                )
                total += 1
                if total % 50 == 0:
                    await status.edit_text(f"⏳ {total} টি পোস্ট সফলভাবে সংরক্ষিত হয়েছে...")
            
            await status.edit_text(f"✅ ইন্ডেক্সিং সম্পন্ন! মোট {total} টি পোস্ট/ফাইল সংরক্ষণ করা হয়েছে।")
        except Exception as e:
            await status.edit_text(f"❌ ইন্ডেক্স করতে সমস্যা হয়েছে: `{str(e)}`\nবটকে চ্যানেলে অ্যাডমিন দেওয়া আছে কিনা যাচাই করুন!")

    # ইউজারের ফাইল দেখা ও নেওয়া (Pagination সহ)
    elif data.startswith("cat_"):
        _, cid_str, page_str = data.split("_")
        cid = int(cid_str)
        page = int(page_str)
        page_size = 5

        total_files = await files_col.count_documents({"channel_id": cid})
        if total_files == 0:
            return await query.answer("⚠️ এই চ্যানেলে কোনো ফাইল এখনো ইন্ডেক্স করা হয়নি!", show_alert=True)

        cursor = files_col.find({"channel_id": cid}).sort("message_id", -1).skip(page * page_size).limit(page_size)
        
        file_buttons = []
        async for doc in cursor:
            # টেক্সট বা ক্যাপশনের প্রথম ২০ অক্ষর বাটনে দেখানো
            title = (doc.get("text") or f"ফাইল #{doc['message_id']}")[:25]
            file_buttons.append([InlineKeyboardButton(f"🎬 {title}", callback_data=f"get_{cid}_{doc['message_id']}")])

        # Pagination Buttons
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ পূর্ববর্তী", callback_data=f"cat_{cid}_{page - 1}"))
        if (page + 1) * page_size < total_files:
            nav_buttons.append(InlineKeyboardButton("পরবর্তী ➡️", callback_data=f"cat_{cid}_{page + 1}"))

        if nav_buttons:
            file_buttons.append(nav_buttons)

        file_buttons.append([InlineKeyboardButton("🔙 মূল মেন্যু", callback_data="back_start")])
        await query.message.edit_text("📂 **ফাইল তালিকা:** (নিচের বাটনে ক্লিক করে ফাইলটি নিন)", reply_markup=InlineKeyboardMarkup(file_buttons))

    # ইউজারকে ফাইল পাঠানো
    elif data.startswith("get_"):
        _, cid_str, mid_str = data.split("_")
        try:
            await client.copy_message(
                chat_id=query.from_user.id,
                from_chat_id=int(cid_str),
                message_id=int(mid_str)
            )
            await query.answer("✅ ফাইল আপনার ইনবক্সে পাঠানো হয়েছে!")
        except Exception as e:
            await query.answer(f"❌ ফাইল পাঠানো যায়নি! বট চ্যানেলে এডমিন আছে তো?", show_alert=True)

    # মূল মেন্যুতে ফেরত যাওয়া
    elif data == "back_start":
        await query.message.delete()
        await start_handler(client, query.message)

# ---------------- BOT & SERVER START ---------------- #
async def main():
    print("Starting Web Server...")
    await web_server()
    print("Starting Telegram Bot...")
    await app.start()
    print("Bot is LIVE!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
