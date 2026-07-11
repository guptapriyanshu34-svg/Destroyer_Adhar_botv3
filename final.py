# requirements:
# pip install "aiogram==3.7.0" aiosqlite aiohttp twocaptcha

import asyncio
import logging
import aiosqlite
import aiohttp
import base64

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.filters import Command

# ================= CONFIG =================

BOT_TOKEN = "8896391845:AAHZ4C7wCeVtCnL-u3j7HCK8JgD1WD1zTgE"
OWNER_ID   = 8072708919          # apna Telegram ID

# --- UIDAI Sandbox ---
UIDAI_BASE_URL    = "https://stage1.uidai.gov.in/onlineDownloadService/api"
UIDAI_APP_ID      = "YOUR_UIDAI_APP_ID"
UIDAI_APP_KEY     = "YOUR_UIDAI_APP_KEY"
UIDAI_SECRET_KEY  = "YOUR_UIDAI_SECRET_KEY"

# --- 2Captcha ---
TWOCAPTCHA_API_KEY = "YOUR_2CAPTCHA_API_KEY"

# ================= BOT =================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= DATABASE =================

async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id  INTEGER PRIMARY KEY,
            username TEXT,
            approved INTEGER DEFAULT 0
        )
        """)
        await db.commit()

# ================= KEYBOARDS =================

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📄 Retrieve EID + Download PDF")],
        [KeyboardButton(text="🪪 Retrieve EID only")],
        [KeyboardButton(text="⬇ Download Aadhaar by EID")],
    ],
    resize_keyboard=True
)

owner_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📢 Broadcast")],
        [KeyboardButton(text="👥 Users")],
    ],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Cancel")]],
    resize_keyboard=True
)

# ================= DB FUNCTIONS =================

async def add_user(user_id, username):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username) VALUES(?, ?)",
            (user_id, username)
        )
        await db.commit()

async def is_approved(user_id):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT approved FROM users WHERE user_id=?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0] == 1
    return False

async def approve_user(user_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "UPDATE users SET approved=1 WHERE user_id=?", (user_id,)
        )
        await db.commit()

async def disapprove_user(user_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "UPDATE users SET approved=0 WHERE user_id=?", (user_id,)
        )
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            return await cursor.fetchall()

# ================= PREMIUM TEXT =================

PREMIUM_TEXT = """
🛒 <b>Premium — Aadhaar Helper</b>

<b>What you get</b>
• Use services anytime
• Unlimited runs while your plan is active
• Full access when free mode is OFF
• Priority access

<b>How to buy</b>
Message owner directly on Telegram.

Owner: @@Destroyeiso

Your Telegram ID: <code>{user_id}</code>

<i>No active plan — contact owner to activate premium.</i>
"""

# ================= STATE =================

user_state    = {}   # { user_id: { step, mode, mobile, name, txn, captcha_ref, ... } }
broadcast_mode = {}  # { user_id: True }

# ================= ACCESS CHECK =================

async def check_access(message: Message) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    approved = await is_approved(message.from_user.id)
    if not approved:
        await message.answer(
            PREMIUM_TEXT.format(user_id=message.from_user.id)
        )
        return False
    return True

# ================= 2CAPTCHA SOLVER =================

async def solve_captcha_with_2captcha(image_b64: str) -> str | None:
    """
    Submit captcha image to 2captcha, poll for result.
    Returns solved text or None on failure.
    """
    try:
        async with aiohttp.ClientSession() as session:

            # --- Step 1: Submit captcha ---
            submit_url = "http://2captcha.com/in.php"
            submit_data = {
                "key":    TWOCAPTCHA_API_KEY,
                "method": "base64",
                "body":   image_b64,
                "json":   "1",
            }
            async with session.post(submit_url, data=submit_data) as resp:
                result = await resp.json(content_type=None)

            if result.get("status") != 1:
                logger.warning(f"2captcha submit failed: {result}")
                return None

            captcha_id = result["request"]

            # --- Step 2: Poll for answer (max 30s) ---
            poll_url = "http://2captcha.com/res.php"
            poll_params = {
                "key":    TWOCAPTCHA_API_KEY,
                "action": "get",
                "id":     captcha_id,
                "json":   "1",
            }

            for _ in range(10):
                await asyncio.sleep(3)
                async with session.get(poll_url, params=poll_params) as resp:
                    poll_result = await resp.json(content_type=None)

                if poll_result.get("status") == 1:
                    return poll_result["request"]

                if poll_result.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                    return None

            return None

    except Exception as e:
        logger.error(f"2captcha error: {e}")
        return None

# ================= UIDAI API FUNCTIONS =================

async def uidai_get_captcha() -> dict | None:
    """
    Fetch captcha image from UIDAI.
    Returns { "captchaTxnId": "...", "captchaImage": "<base64>" } or None.
    """
    url = f"{UIDAI_BASE_URL}/getCaptcha"
    headers = {
        "appId":     UIDAI_APP_ID,
        "secretKey": UIDAI_SECRET_KEY,
        "Content-Type": "application/json",
    }
    payload = {"uid": ""}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                logger.info(f"getCaptcha response: {data}")
                if data.get("status") == "Y":
                    return {
                        "captchaTxnId": data.get("captchaTxnId"),
                        "captchaImage": data.get("captchaImage"),
                    }
                return None
    except Exception as e:
        logger.error(f"getCaptcha error: {e}")
        return None


async def uidai_generate_otp(mobile: str, captcha_txn_id: str, captcha_value: str) -> dict | None:
    """
    Generate OTP using mobile + captcha.
    Returns { "txnId": "..." } or None.
    """
    url = f"{UIDAI_BASE_URL}/generateOtp"
    headers = {
        "appId":     UIDAI_APP_ID,
        "secretKey": UIDAI_SECRET_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "mobileNumber":  mobile,
        "captchaTxnId":  captcha_txn_id,
        "captchaValue":  captcha_value,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                logger.info(f"generateOtp response: {data}")
                if data.get("status") == "Y":
                    return {"txnId": data.get("txnId")}
                return None
    except Exception as e:
        logger.error(f"generateOtp error: {e}")
        return None


async def uidai_verify_otp_get_eid(txn_id: str, otp: str, mobile: str, name: str) -> dict | None:
    """
    Verify OTP and retrieve EID.
    Returns { "eid": "..." } or None.
    """
    url = f"{UIDAI_BASE_URL}/verifyOtp"
    headers = {
        "appId":     UIDAI_APP_ID,
        "secretKey": UIDAI_SECRET_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "txnId":        txn_id,
        "otp":          otp,
        "mobileNumber": mobile,
        "name":         name,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                logger.info(f"verifyOtp response: {data}")
                if data.get("status") == "Y":
                    return {"eid": data.get("eid")}
                return None
    except Exception as e:
        logger.error(f"verifyOtp error: {e}")
        return None


async def uidai_download_aadhaar(eid: str, txn_id: str, otp: str) -> bytes | None:
    """
    Download Aadhaar PDF by EID.
    Returns PDF bytes or None.
    """
    url = f"{UIDAI_BASE_URL}/downloadAadhaar"
    headers = {
        "appId":     UIDAI_APP_ID,
        "secretKey": UIDAI_SECRET_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "eid":   eid,
        "txnId": txn_id,
        "otp":   otp,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                logger.info(f"downloadAadhaar response status: {data.get('status')}")
                if data.get("status") == "Y":
                    pdf_b64 = data.get("aadhaarPdf") or data.get("pdf")
                    if pdf_b64:
                        return base64.b64decode(pdf_b64)
                return None
    except Exception as e:
        logger.error(f"downloadAadhaar error: {e}")
        return None


async def uidai_download_by_eid_only(eid: str, captcha_txn_id: str, captcha_value: str) -> dict | None:
    """
    Download Aadhaar using EID directly (separate captcha flow).
    Returns { "txnId": "..." } or None — triggers OTP on registered mobile.
    """
    url = f"{UIDAI_BASE_URL}/getOtpForEid"
    headers = {
        "appId":     UIDAI_APP_ID,
        "secretKey": UIDAI_SECRET_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "eid":           eid,
        "captchaTxnId":  captcha_txn_id,
        "captchaValue":  captcha_value,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                logger.info(f"getOtpForEid response: {data}")
                if data.get("status") == "Y":
                    return {"txnId": data.get("txnId")}
                return None
    except Exception as e:
        logger.error(f"getOtpForEid error: {e}")
        return None

# ================= CAPTCHA FLOW HELPER =================

async def start_captcha_flow(message: Message, mode: str):
    """
    Fetch captcha → try 2captcha auto solve → if fail, send image to user manually.
    mode: "eid_pdf" | "eid_only" | "download_eid"
    """
    uid = message.from_user.id
    await message.answer("🔄 Fetching captcha from UIDAI...")

    captcha_data = await uidai_get_captcha()

    if not captcha_data:
        user_state.pop(uid, None)
        return await message.answer(
            "❌ Could not reach UIDAI server. Try again later.",
            reply_markup=main_kb
        )

    user_state[uid]["captcha_txn_id"] = captcha_data["captchaTxnId"]
    image_b64 = captcha_data["captchaImage"]

    await message.answer("🤖 Trying to solve captcha automatically via 2Captcha...")

    solved = await solve_captcha_with_2captcha(image_b64)

    if solved:
        user_state[uid]["captcha_value"] = solved
        user_state[uid]["step"] = "captcha_confirmed"
        await message.answer(
            f"✅ Captcha solved automatically: <code>{solved}</code>\n\n"
            "Sending OTP to registered mobile..."
        )
        await proceed_after_captcha(message)

    else:
        # Send captcha image to user for manual solving
        user_state[uid]["step"] = "manual_captcha"
        image_bytes = base64.b64decode(image_b64)

        from aiogram.types import BufferedInputFile
        await message.answer_photo(
            BufferedInputFile(image_bytes, filename="captcha.jpg"),
            caption="❌ Auto solve failed.\n\nPlease type the captcha text shown in the image.",
            reply_markup=cancel_kb
        )


async def proceed_after_captcha(message: Message):
    """
    After captcha is solved (auto or manual), call generateOtp or getOtpForEid.
    """
    uid   = message.from_user.id
    state = user_state.get(uid, {})
    mode  = state.get("mode")

    captcha_txn_id = state.get("captcha_txn_id")
    captcha_value  = state.get("captcha_value")

    if mode in ("eid_pdf", "eid_only"):
        mobile = state.get("mobile")
        result = await uidai_generate_otp(mobile, captcha_txn_id, captcha_value)

        if not result:
            user_state.pop(uid, None)
            return await message.answer(
                "❌ OTP generation failed. Mobile number not registered or captcha wrong.\n"
                "Use /start to try again.",
                reply_markup=main_kb
            )

        user_state[uid]["txn_id"] = result["txnId"]
        user_state[uid]["step"]   = "otp"

        await message.answer(
            "✅ OTP sent to your registered mobile number.\n\n"
            "📩 Send OTP now.",
            reply_markup=cancel_kb
        )

    elif mode == "download_eid":
        eid    = state.get("eid")
        result = await uidai_download_by_eid_only(eid, captcha_txn_id, captcha_value)

        if not result:
            user_state.pop(uid, None)
            return await message.answer(
                "❌ EID not found or captcha wrong.\n"
                "Use /start to try again.",
                reply_markup=main_kb
            )

        user_state[uid]["txn_id"] = result["txnId"]
        user_state[uid]["step"]   = "otp_eid_download"

        await message.answer(
            "✅ OTP sent to mobile linked with this EID.\n\n"
            "📩 Send OTP now.",
            reply_markup=cancel_kb
        )

# ================= COMMANDS =================

@dp.message(Command("start"))
async def start_cmd(message: Message):
    await add_user(message.from_user.id, message.from_user.username)
    user_state.pop(message.from_user.id, None)

    approved = await is_approved(message.from_user.id)

    if not approved and message.from_user.id != OWNER_ID:
        return await message.answer(
            PREMIUM_TEXT.format(user_id=message.from_user.id)
        )

    text = "✅ <b>Welcome</b>\n\nChoose an option below."

    if message.from_user.id == OWNER_ID:
        await message.answer(text, reply_markup=owner_kb)
    else:
        await message.answer(text, reply_markup=main_kb)


@dp.message(Command("approve"))
async def approve_cmd(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Usage: /approve USER_ID")

    user_id = int(args[1])
    await approve_user(user_id)

    try:
        await bot.send_message(
            user_id,
            "✅ <b>Premium activated!</b>\n\nYou now have full access.\nSend /start to begin."
        )
    except Exception:
        pass

    await message.answer(f"✅ Approved: <code>{user_id}</code>")


@dp.message(Command("disapprove"))
async def disapprove_cmd(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Usage: /disapprove USER_ID")

    user_id = int(args[1])
    await disapprove_user(user_id)

    try:
        await bot.send_message(user_id, "⛔ Your premium access has been removed.")
    except Exception:
        pass

    await message.answer(f"✅ Disapproved: <code>{user_id}</code>")

# ================= OWNER BUTTONS =================

@dp.message(F.text == "👥 Users")
async def users_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    users = await get_all_users()
    text  = f"👥 <b>Total Users: {len(users)}</b>\n\n"
    for user in users[:50]:
        text += f"• <code>{user[0]}</code>\n"

    await message.answer(text)


@dp.message(F.text == "📢 Broadcast")
async def broadcast_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    broadcast_mode[message.from_user.id] = True
    await message.answer("📢 Send broadcast message now.")

# ================= CANCEL =================

@dp.message(F.text == "❌ Cancel")
async def cancel_handler(message: Message):
    user_state.pop(message.from_user.id, None)
    await message.answer("❌ Cancelled.", reply_markup=main_kb)

# ================= MAIN HANDLER =================

@dp.message()
async def all_messages(message: Message):
    uid = message.from_user.id

    # --- BROADCAST ---
    if broadcast_mode.get(uid):
        users   = await get_all_users()
        success = 0
        for user in users:
            try:
                await bot.send_message(user[0], message.text)
                success += 1
            except Exception:
                pass
        broadcast_mode.pop(uid)
        return await message.answer(f"✅ Broadcast sent to {success} users.")

    # --- MENU BUTTONS ---

    if message.text == "📄 Retrieve EID + Download PDF":
        if not await check_access(message):
            return
        user_state[uid] = {"step": "mobile", "mode": "eid_pdf"}
        return await message.answer(
            "📱 <b>Step 1/3</b>\n\nSend your <b>registered mobile number</b>.",
            reply_markup=cancel_kb
        )

    if message.text == "🪪 Retrieve EID only":
        if not await check_access(message):
            return
        user_state[uid] = {"step": "mobile", "mode": "eid_only"}
        return await message.answer(
            "📱 <b>Step 1/3</b>\n\nSend your <b>registered mobile number</b>.",
            reply_markup=cancel_kb
        )

    if message.text == "⬇ Download Aadhaar by EID":
        if not await check_access(message):
            return
        user_state[uid] = {"step": "eid_input", "mode": "download_eid"}
        return await message.answer(
            "🪪 Send your <b>EID number</b>.",
            reply_markup=cancel_kb
        )

    # --- STEP: EID INPUT (for download by EID) ---

    if user_state.get(uid, {}).get("step") == "eid_input":
        eid = message.text.strip()
        if not eid.isdigit() or len(eid) < 16:
            return await message.answer("❌ Invalid EID. Please send correct EID number.")
        user_state[uid]["eid"]  = eid
        user_state[uid]["step"] = "captcha_pending"
        await start_captcha_flow(message, mode="download_eid")
        return

    # --- STEP: MOBILE ---

    if user_state.get(uid, {}).get("step") == "mobile":
        mobile = message.text.strip()
        if not mobile.isdigit() or len(mobile) != 10:
            return await message.answer("❌ Invalid mobile number. Send 10-digit mobile number.")
        user_state[uid]["mobile"] = mobile
        user_state[uid]["step"]   = "name"
        return await message.answer(
            "👤 <b>Step 2/3</b>\n\nSend your <b>full name</b> (as on Aadhaar)."
        )

    # --- STEP: NAME ---

    if user_state.get(uid, {}).get("step") == "name":
        name = message.text.strip()
        if len(name) < 2:
            return await message.answer(