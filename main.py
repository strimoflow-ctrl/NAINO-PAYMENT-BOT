import logging
import time
import asyncio
import os
import base64
import json
import tempfile
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, FSInputFile
)
from aiogram.filters import Command
import razorpay
import firebase_admin
from firebase_admin import credentials, db
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─────────────────────────────────────────────
#  CONFIGURATION  (100% ENV-based, Render-ready)
# ─────────────────────────────────────────────
BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
RAZORPAY_KEY_ID    = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET= os.environ.get("RAZORPAY_KEY_SECRET", "")
FIREBASE_DB_URL    = os.environ.get("FIREBASE_DB_URL", "")
ADMIN_PASS         = os.environ.get("ADMIN_PASS", "admin123")
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")
APK_FILE_ID        = os.environ.get("APK_FILE_ID", "")
# Firebase service account JSON supplied as base64 string in env
FIREBASE_CRED_B64  = os.environ.get("FIREBASE_CRED_BASE64", "")
# Render public URL (used for Telegram webhook registration)
RENDER_URL         = os.environ.get("RENDER_URL", "")  # e.g. https://yourapp.onrender.com

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("naino_bot")

# ─────────────────────────────────────────────
#  FIREBASE INIT
# ─────────────────────────────────────────────
def _init_firebase():
    if firebase_admin._apps:
        return
    try:
        if FIREBASE_CRED_B64:
            cred_json = json.loads(base64.b64decode(FIREBASE_CRED_B64).decode())
            # Write to temp file (firebase SDK needs a file path or dict)
            cred = credentials.Certificate(cred_json)
        else:
            cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
        logger.info("Firebase initialized successfully.")
    except Exception as e:
        logger.error(f"Firebase Init Error: {e}")

_init_firebase()

# ─────────────────────────────────────────────
#  RAZORPAY CLIENT
# ─────────────────────────────────────────────
rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ─────────────────────────────────────────────
#  BOT + DISPATCHER
# ─────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ─────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────
app = FastAPI(title="Naino Academy API", version="2.0.0")

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
import random, string

def generate_code() -> str:
    return str(random.randint(100000, 999999))

def get_remote_config(key: str, default=None):
    """Read a value from admin_settings/ Firebase node."""
    try:
        val = db.reference(f"admin_settings/{key}").get()
        return val if val is not None else default
    except Exception:
        return default

def plan_price(plan_key: str) -> int:
    """Return price in paise from remote config or default."""
    defaults = {"Silver": 100, "Gold": 100, "Diamond": 100}
    return int(get_remote_config(f"prices/{plan_key}", defaults.get(plan_key, 100)))

# ─────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Subscription Plans", callback_data="btn_plans"),
            InlineKeyboardButton(text="👤 My Status",          callback_data="btn_status"),
        ],
        [
            InlineKeyboardButton(text="📱 Download App",        callback_data="btn_app"),
            InlineKeyboardButton(text="💬 Feedback",            url="https://nainoacademy.netlify.app/feedback"),
        ],
        [
            InlineKeyboardButton(text="🔑 Get Demo Key (5m)",   callback_data="btn_demo"),
            InlineKeyboardButton(text="🆘 Help Support",        url="https://t.me/nainochatbot"),
        ],
    ])

def get_plans_keyboard():
    silver_price  = plan_price("Silver")  // 100
    gold_price    = plan_price("Gold")    // 100
    diamond_price = plan_price("Diamond") // 100
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🥈 Silver  (1 Month)  – ₹{silver_price}",  callback_data="select_30_Silver")],
        [InlineKeyboardButton(text=f"🥇 Gold    (6 Months) – ₹{gold_price}",   callback_data="select_180_Gold")],
        [InlineKeyboardButton(text=f"💎 Diamond (1 Year)   – ₹{diamond_price}", callback_data="select_365_Diamond")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_main")],
    ])

# ─────────────────────────────────────────────
#  BOT HANDLERS
# ─────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    uid  = str(message.from_user.id)
    name = message.from_user.first_name or "User"
    # Register user in Firebase (background-safe, fire-and-forget)
    try:
        db.reference(f"users/{uid}").update({
            "first_name": name,
            "username":   message.from_user.username or "",
            "joined_at":  int(time.time() * 1000),
        })
    except Exception as e:
        logger.warning(f"User register error: {e}")

    await message.answer(
        f"👋 *Welcome, {name}!*\n\n"
        "🎓 Naino Academy Sales Bot में आपका स्वागत है।\n"
        "अपना प्लान चुनें या नीचे दिए गए विकल्पों का उपयोग करें:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "btn_plans")
async def show_plans(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "✨ *Premium Plans चुनें:*\n\n"
        "हर प्लान में मिलेगा: HD Videos, Live Classes, PDF Notes & App Access",
        reply_markup=get_plans_keyboard(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("select_"))
async def choose_payment_method(callback: CallbackQuery):
    await callback.answer()
    _, days, plan_name = callback.data.split("_")
    price_inr = plan_price(plan_name) // 100

    method_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 GPay / PhonePe / Paytm", callback_data=f"pay_link_{days}_{plan_name}")],
        [InlineKeyboardButton(text="🖼️ QR Code दिखाएं",          callback_data=f"pay_qr_{days}_{plan_name}")],
        [InlineKeyboardButton(text="⬅️ Back to Plans",           callback_data="btn_plans")],
    ])
    await callback.message.edit_text(
        f"🏆 *Plan:* {plan_name}  ({days} Days)\n"
        f"💰 *Price:* ₹{price_inr}\n\n"
        "पेमेंट करने का तरीका चुनें:",
        reply_markup=method_kb,
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("pay_"))
async def generate_final_pay(callback: CallbackQuery):
    await callback.answer("Please wait…")
    parts     = callback.data.split("_")
    mode      = parts[1]   # 'link' or 'qr'
    days      = parts[2]
    plan_name = parts[3]
    price     = plan_price(plan_name)

    try:
        bot_info  = await bot.get_me()
        rzp_data  = {
            "amount":          price,
            "currency":        "INR",
            "expire_by":       int(time.time()) + 1200,
            "description":     f"Naino Academy – {plan_name} Plan",
            "notes": {
                "user_id":   str(callback.from_user.id),
                "days":      days,
                "plan_type": plan_name,
            },
            "callback_url":    f"https://t.me/{bot_info.username}",
            "callback_method": "get",
        }
        payment_link = rzp_client.payment_link.create(rzp_data)
        short_url    = payment_link["short_url"]

        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Cancel & Back", callback_data="btn_plans")]
        ])

        if mode == "link":
            await callback.message.edit_text(
                f"✅ *Payment Link Ready!*\n\n"
                f"🔗 [Click here to Pay ₹{price//100}]({short_url})\n\n"
                "पेमेंट के बाद 10 सेकंड रुकें, आपका कोड यहाँ आ जाएगा।",
                reply_markup=back_kb,
                parse_mode="Markdown",
            )
        else:
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={short_url}"
            await callback.message.delete()
            await bot.send_photo(
                callback.from_user.id,
                photo=qr_url,
                caption=(
                    f"📸 *QR Scan करके Pay करें – ₹{price//100}*\n\n"
                    f"Plan: {plan_name} ({days} Days)\n\n"
                    "पेमेंट होने के बाद कोड अपने आप आएगा।"
                ),
                reply_markup=back_kb,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Pay Gen Error: {e}")
        await callback.message.answer("❌ पेमेंट लिंक बनाने में समस्या आई। कृपया फिर कोशिश करें।")


@dp.callback_query(F.data == "btn_status")
async def check_status(callback: CallbackQuery):
    await callback.answer()
    user_id = str(callback.from_user.id)
    try:
        all_codes = db.reference("access_codes").order_by_child("telegram_id").equal_to(user_id).get()
        if not all_codes:
            await callback.message.answer(
                "❌ आपका कोई एक्टिव प्लान नहीं मिला।\n"
                "Plans बटन से नया प्लान खरीदें।",
                reply_markup=get_main_keyboard(),
            )
            return

        # Find latest non-expired code
        best = None
        for key, val in all_codes.items():
            if best is None or val.get("expires_at", 0) > best[1].get("expires_at", 0):
                best = (key, val)

        code_key, data = best
        exp_ts   = data.get("expires_at", 0)
        exp_dt   = datetime.fromtimestamp(exp_ts / 1000)
        remaining = (exp_dt - datetime.now()).days

        status = "✅ Active" if remaining > 0 else "❌ Expired"
        await callback.message.answer(
            f"👤 *Your Profile*\n━━━━━━━━━━━━━━\n"
            f"🔑 *Code:* `{code_key}`\n"
            f"📊 *Plan:* {data.get('plan_type','N/A')}\n"
            f"🟢 *Status:* {status}\n"
            f"⏳ *Remaining:* {max(0, remaining)} Days\n"
            f"📅 *Expiry:* {exp_dt.strftime('%d-%m-%Y')}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Status Error: {e}")
        await callback.message.answer("⚠️ स्टेटस चेक करने में एरर आया।")


@dp.callback_query(F.data == "btn_demo")
async def get_demo(callback: CallbackQuery):
    user_id  = str(callback.from_user.id)
    user_ref = db.reference(f"users/{user_id}")
    user_data = user_ref.get()

    if user_data and user_data.get("demo_taken"):
        await callback.answer("❌ आप पहले ही डेमो ले चुके हैं!", show_alert=True)
        return

    demo_code   = generate_code()
    expiry_time = int((datetime.now() + timedelta(minutes=5)).timestamp() * 1000)

    db.reference(f"access_codes/{demo_code}").set({
        "status":      "active",
        "telegram_id": user_id,
        "created_at":  int(time.time() * 1000),
        "expires_at":  expiry_time,
        "is_demo":     True,
        "plan_type":   "Demo",
    })
    user_ref.update({"demo_taken": True})

    await callback.message.answer(
        f"🎁 *5-Min Demo Key!*\n\n🔑 Code: `{demo_code}`\n\n"
        "यह कोड केवल 5 मिनट के लिए valid है।",
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "btn_app")
async def send_app(callback: CallbackQuery):
    await callback.answer()
    try:
        if APK_FILE_ID:
            await bot.send_document(
                callback.from_user.id, APK_FILE_ID,
                caption="📥 *Naino Academy App*\n\nInstall करें और सीखना शुरू करें!",
                parse_mode="Markdown",
            )
        else:
            raise ValueError("No APK_FILE_ID")
    except Exception:
        await callback.message.answer("🌐 App Download: https://nainoacademy.netlify.app/")


@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "🏠 *Main Menu*\nनीचे से अपना विकल्प चुनें:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


# Utility: capture file_id when admin sends a document
@dp.message(F.document)
async def get_file_id(message: types.Message):
    await message.answer(f"📎 File ID:\n`{message.document.file_id}`", parse_mode="Markdown")


# ─────────────────────────────────────────────
#  SMART RETENTION ENGINE  (APScheduler)
# ─────────────────────────────────────────────
scheduler = AsyncIOScheduler()

async def send_expiry_reminders():
    """Run daily at 10:00 AM IST to warn expiring users."""
    logger.info("Running expiry reminder job…")
    try:
        all_codes = db.reference("access_codes").get() or {}
        now_ms    = int(datetime.now().timestamp() * 1000)

        for code, data in all_codes.items():
            if data.get("is_demo") or data.get("status") == "expired":
                continue

            exp_ms    = data.get("expires_at", 0)
            days_left = (exp_ms - now_ms) / (1000 * 86400)
            uid       = data.get("telegram_id")
            if not uid:
                continue

            notif_sent = data.get("notif_sent", {})

            try:
                if 2.9 <= days_left <= 3.1 and not notif_sent.get("day3"):
                    msg = get_remote_config(
                        "reminder_day3",
                        "⏰ *Reminder:* आपका plan 3 दिनों में expire होगा!\n\n"
                        "अभी Renew करें और बिना रुकावट पढ़ते रहें। 🎓"
                    )
                    await bot.send_message(uid, msg + "\n\n/start", parse_mode="Markdown")
                    db.reference(f"access_codes/{code}/notif_sent").update({"day3": True})

                elif 1.9 <= days_left <= 2.1 and not notif_sent.get("day2"):
                    msg = get_remote_config(
                        "reminder_day2",
                        "🚨 *Alert:* सिर्फ 48 घंटे बचे हैं!\n\n"
                        "Access खोने से पहले Renew करें।"
                    )
                    await bot.send_message(uid, msg + "\n\n/start", parse_mode="Markdown")
                    db.reference(f"access_codes/{code}/notif_sent").update({"day2": True})

                elif 0.9 <= days_left <= 1.1 and not notif_sent.get("day1"):
                    plan = data.get("plan_type", "Silver")
                    price_inr = plan_price(plan) // 100
                    msg = get_remote_config(
                        "reminder_day1",
                        f"🔴 *LAST CHANCE!* आपका plan कल expire हो रहा है!\n\n"
                        f"अभी ₹{price_inr} में renew करें।"
                    )
                    renew_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Renew Now", callback_data="btn_plans")]
                    ])
                    await bot.send_message(uid, msg, reply_markup=renew_kb, parse_mode="Markdown")
                    db.reference(f"access_codes/{code}/notif_sent").update({"day1": True})

                elif days_left <= 0 and data.get("status") != "expired":
                    db.reference(f"access_codes/{code}").update({"status": "expired"})

            except Exception as e:
                logger.warning(f"Reminder send error for {uid}: {e}")

    except Exception as e:
        logger.error(f"Expiry reminder job error: {e}")


# ─────────────────────────────────────────────
#  FASTAPI ROUTES
# ─────────────────────────────────────────────

# --- Telegram Webhook ---
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return {"ok": True}


# --- Razorpay Webhook ---
@app.post("/razorpay-webhook")
async def razorpay_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data  = await request.json()
        event = data.get("event", "")

        if event == "payment_link.paid":
            payload = data["payload"]["payment_link"]["entity"]
            notes   = payload.get("notes", {})
            u_id    = notes.get("user_id")
            days    = int(notes.get("days", 30))
            plan    = notes.get("plan_type", "Silver")
            amount  = payload.get("amount", 0)  # paise

            background_tasks.add_task(_process_payment, u_id, days, plan, amount)

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "detail": str(e)}


async def _process_payment(u_id: str, days: int, plan: str, amount_paise: int):
    """Async DB write + Telegram notification. Runs in background."""
    try:
        new_code   = generate_code()
        expiry_at  = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
        ts_now     = int(time.time() * 1000)

        db.reference(f"access_codes/{new_code}").set({
            "status":      "active",
            "telegram_id": u_id,
            "created_at":  ts_now,
            "expires_at":  expiry_at,
            "plan_type":   plan,
            "is_demo":     False,
            "notif_sent":  {},
        })

        # Write to sales_history for dashboard graph
        date_key = datetime.now().strftime("%Y-%m-%d")
        sale_ref = db.reference(f"sales_history/{date_key}")
        existing = sale_ref.get() or {}
        sale_ref.set({
            "revenue": existing.get("revenue", 0) + amount_paise,
            "count":   existing.get("count", 0) + 1,
        })

        await bot.send_message(
            u_id,
            f"🎉 *Payment Successful!*\n\n"
            f"🔑 *Your Code:* `{new_code}`\n"
            f"📊 *Plan:* {plan}\n"
            f"⏳ *Validity:* {days} Days\n\n"
            "Thank you for subscribing! 🙏\nHappy Learning 🎓",
            parse_mode="Markdown",
        )
        logger.info(f"Payment processed: {new_code} for {u_id}")
    except Exception as e:
        logger.error(f"_process_payment error: {e}")


# --- Admin Dashboard ---
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    pwd = request.query_params.get("pass", "")
    if pwd != ADMIN_PASS:
        return HTMLResponse(
            "<html><body style='background:#0d0d0d;color:#ff4444;font-family:monospace;"
            "display:flex;align-items:center;justify-content:center;height:100vh;font-size:1.5rem'>"
            "🔒 Unauthorized. Provide ?pass= query param.</body></html>",
            status_code=403,
        )
    try:
        with open("admin.html", "r") as f:
            html = f.read()
        return HTMLResponse(html)
    except FileNotFoundError:
        return HTMLResponse("<h1>admin.html not found</h1>", status_code=500)


# --- Admin API Endpoints (called by dashboard JS) ---
@app.get("/api/admin/stats")
async def api_stats(request: Request):
    _require_admin(request)
    try:
        codes = db.reference("access_codes").get() or {}
        users = db.reference("users").get() or {}
        sales = db.reference("sales_history").get() or {}

        now_ms     = int(datetime.now().timestamp() * 1000)
        today_end  = int((datetime.now() + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        today_start= int(datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

        active_users  = sum(1 for c in codes.values()
                            if c.get("status") == "active" and c.get("expires_at", 0) > now_ms)
        expiring_today= sum(1 for c in codes.values()
                            if today_start <= c.get("expires_at", 0) <= today_end)
        total_revenue = sum(s.get("revenue", 0) for s in sales.values()) // 100  # ₹

        return {
            "total_revenue":   total_revenue,
            "active_users":    active_users,
            "total_users":     len(users),
            "expiring_today":  expiring_today,
            "total_codes":     len(codes),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/admin/sales-graph")
async def api_sales_graph(request: Request, period: str = "daily"):
    _require_admin(request)
    try:
        sales = db.reference("sales_history").get() or {}
        labels, revenues, counts = [], [], []

        if period == "daily":
            # Last 30 days
            for i in range(29, -1, -1):
                d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                entry = sales.get(d, {})
                labels.append(d[5:])   # MM-DD
                revenues.append(entry.get("revenue", 0) // 100)
                counts.append(entry.get("count", 0))
        else:
            # Monthly – last 12 months
            from collections import defaultdict
            monthly = defaultdict(lambda: {"revenue": 0, "count": 0})
            for date_key, val in sales.items():
                month = date_key[:7]
                monthly[month]["revenue"] += val.get("revenue", 0)
                monthly[month]["count"]   += val.get("count", 0)
            for m in sorted(monthly)[-12:]:
                labels.append(m)
                revenues.append(monthly[m]["revenue"] // 100)
                counts.append(monthly[m]["count"])

        return {"labels": labels, "revenues": revenues, "counts": counts}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/admin/broadcast")
async def api_broadcast(request: Request, background_tasks: BackgroundTasks):
    _require_admin(request)
    body    = await request.json()
    message = body.get("message", "").strip()
    image   = body.get("image_url", "").strip()
    if not message:
        raise HTTPException(400, "Message required")
    background_tasks.add_task(_do_broadcast, message, image)
    return {"status": "queued"}


async def _do_broadcast(message: str, image_url: str = ""):
    users = db.reference("users").get() or {}
    sent, failed = 0, 0
    for uid in users:
        try:
            if image_url:
                await bot.send_photo(uid, photo=image_url, caption=message, parse_mode="Markdown")
            else:
                await bot.send_message(uid, message, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)   # throttle ~20 msg/s
        except Exception:
            failed += 1
    logger.info(f"Broadcast done: {sent} sent, {failed} failed")


@app.get("/api/admin/config")
async def api_get_config(request: Request):
    _require_admin(request)
    config = db.reference("admin_settings").get() or {}
    return config


@app.post("/api/admin/config")
async def api_save_config(request: Request):
    _require_admin(request)
    body = await request.json()
    db.reference("admin_settings").update(body)
    return {"status": "saved"}


@app.get("/api/admin/recent-sales")
async def api_recent_sales(request: Request):
    _require_admin(request)
    codes = db.reference("access_codes").order_by_child("created_at").limit_to_last(20).get() or {}
    result = []
    for k, v in reversed(list(codes.items())):
        if not v.get("is_demo"):
            result.append({
                "code":       k,
                "plan":       v.get("plan_type"),
                "user_id":    v.get("telegram_id"),
                "created_at": v.get("created_at"),
                "status":     v.get("status"),
            })
    return result


def _require_admin(request: Request):
    pwd = request.query_params.get("pass", "")
    if pwd != ADMIN_PASS:
        raise HTTPException(status_code=403, detail="Unauthorized")


# ─────────────────────────────────────────────
#  APP LIFECYCLE
# ─────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    # Register Telegram webhook
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/telegram-webhook"
        await bot.set_webhook(webhook_url)
        logger.info(f"Webhook set: {webhook_url}")
    # Start scheduler
    scheduler.add_job(send_expiry_reminders, "cron", hour=4, minute=30)  # 10:00 AM IST = 04:30 UTC
    scheduler.start()
    logger.info("Scheduler started.")


@app.on_event("shutdown")
async def on_shutdown():
    await bot.session.close()
    scheduler.shutdown()
    logger.info("Bot and scheduler shut down.")


# ─────────────────────────────────────────────
#  ENTRY POINT  (local dev with polling)
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    async def _local_main():
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(dp.start_polling(bot))
        scheduler.add_job(send_expiry_reminders, "cron", hour=4, minute=30)
        scheduler.start()
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
        await uvicorn.Server(config).serve()

    asyncio.run(_local_main())
