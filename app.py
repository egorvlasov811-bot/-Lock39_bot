"""
Камера хранения — единый процесс: Telegram бот + API сервер + статика
Запуск: python app.py
"""

import os
import io
import json
import asyncio
import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, WebAppInfo, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

import qrcode

# ─────────── НАСТРОЙКИ ───────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
PORT = int(os.getenv("PORT", "8000"))
DB_FILE = "bookings.json"

# ─────────── БАЗА (JSON) ───────────

def load_db() -> dict:
    if not Path(DB_FILE).exists():
        return {"bookings": []}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"bookings": []}

def save_db(data: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─────────── QR ───────────

def make_qr_image(data: str) -> bytes:
    """Генерация QR-кода как PNG в байтах."""
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

# ─────────── BOT ───────────

bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None

if BOT_TOKEN:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        kb = InlineKeyboardBuilder()
        if WEBAPP_URL:
            kb.add(InlineKeyboardButton(
                text="🔐 Открыть камеру хранения",
                web_app=WebAppInfo(url=WEBAPP_URL)
            ))
        text = (
            "👋 Добро пожаловать в камеру хранения!\n\n"
            "Здесь можно забронировать место для вещей.\n\n"
            "🕐 *Тарифы:*\n"
            "• 1 час — 100 ₽/шт\n"
            "• 3 часа — 200 ₽/шт\n"
            "• Весь день (09:00–19:00) — 300 ₽/шт\n"
            "• После 19:00 — 100 ₽/час\n"
            "• Суточное — 600 ₽/сут\n\n"
        )
        if WEBAPP_URL:
            text += "Нажмите кнопку ниже 👇"
        else:
            text += "⚠️ WEBAPP_URL не настроен."
        await message.answer(text, parse_mode="Markdown", reply_markup=kb.as_markup() if WEBAPP_URL else None)

    @dp.message(Command("help"))
    async def cmd_help(message: types.Message):
        await message.answer(
            "ℹ️ *Помощь*\n\n"
            "/start — открыть бронирование\n"
            "/mybookings — мои бронирования\n"
            "/help — эта справка",
            parse_mode="Markdown"
        )

    @dp.message(Command("mybookings"))
    async def cmd_mybookings(message: types.Message):
        db = load_db()
        my = [b for b in db.get("bookings", []) if b.get("telegram_user_id") == message.from_user.id]
        if not my:
            await message.answer("У вас пока нет активных бронирований.\nЧтобы создать — нажмите /start")
            return
        # Последние 5 броней
        for b in my[-5:]:
            text = (
                f"🔐 *Бронирование*\n"
                f"🆔 `{b['booking_id']}`\n"
                f"📦 Ячейка №{b['cell']}\n"
                f"💰 {b['tariff']}\n"
                f"📅 {b['date']} {b.get('time') or ''}\n"
                f"🎒 {b['items']} шт\n"
                f"💵 {b['total']:,} ₽"
            )
            await message.answer(text, parse_mode="Markdown")


async def send_user_confirmation(booking: dict):
    """Шлёт пользователю подтверждение брони с QR-кодом."""
    if not bot:
        return
    user_id = booking.get("telegram_user_id")
    if not user_id:
        print("[user_confirm] нет telegram_user_id, пропускаем")
        return
    try:
        qr_data = json.dumps({
            "id": booking["booking_id"],
            "cell": booking["cell"],
            "tariff": booking["tariff"],
            "date": booking["date"],
            "time": booking.get("time"),
            "items": booking["items"],
            "name": booking["name"],
            "total": booking["total"]
        }, ensure_ascii=False)

        qr_bytes = make_qr_image(qr_data)

        caption = (
            f"✅ *Бронирование подтверждено!*\n"
            f"━━━━━━━━━━━━━━\n"
            f"🆔 `{booking['booking_id']}`\n"
            f"📦 Ячейка: *№{booking['cell']}*\n"
            f"💰 Тариф: {booking['tariff']}\n"
            f"📅 Дата: {booking['date']}\n"
            f"⏰ Время: {booking.get('time') or '—'}\n"
            f"🎒 Вещей: {booking['items']} шт\n"
            f"👤 Имя: {booking['name']}\n"
            f"💵 К оплате: *{booking['total']:,} ₽*\n\n"
            f"📲 *Покажите этот QR-код на стойке* при получении ячейки.\n"
            f"Сохраните это сообщение или сделайте скриншот."
        )

        photo = BufferedInputFile(qr_bytes, filename=f"booking_{booking['booking_id']}.png")
        await bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=caption,
            parse_mode="Markdown"
        )
        print(f"[user_confirm] отправлено пользователю {user_id}")
    except Exception as e:
        print(f"[user_confirm] ошибка: {e}")


async def notify_admin(booking: dict):
    """Шлёт админу уведомление о новой брони."""
    if not bot or not ADMIN_CHAT_ID:
        return
    try:
        user_info = ""
        if booking.get("telegram_username"):
            user_info = f" (@{booking['telegram_username']})"
        msg = (
            f"🔔 *Новое бронирование!*\n"
            f"━━━━━━━━━━━━━━\n"
            f"🆔 `{booking['booking_id']}`\n"
            f"📦 Ячейка: *№{booking['cell']}*\n"
            f"💰 Тариф: {booking['tariff']}\n"
            f"📅 Дата: {booking['date']}\n"
            f"⏰ Время: {booking.get('time') or '—'}\n"
            f"🎒 Вещей: {booking['items']} шт\n"
            f"👤 Имя: {booking['name']}{user_info}\n"
            f"📞 Тел.: {booking.get('phone') or '—'}\n"
            f"💵 Итого: *{booking['total']:,} ₽*"
        )
        await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=msg, parse_mode="Markdown")
    except Exception as e:
        print(f"[admin] error: {e}")


async def start_bot():
    if not bot or not dp:
        print("⚠️ BOT_TOKEN не задан — бот не запускается")
        return
    print("🤖 Bot polling запущен")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"[bot] error: {e}")


# ─────────── API ───────────

class BookingRequest(BaseModel):
    booking_id: str
    cell: int
    tariff: str
    date: str
    time: Optional[str] = None
    items: int
    name: str
    phone: Optional[str] = None
    total: int
    telegram_user_id: Optional[int] = None
    telegram_username: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task = None
    if bot and dp:
        bot_task = asyncio.create_task(start_bot())
    yield
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
    if bot:
        await bot.session.close()


app = FastAPI(title="Камера хранения", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    if Path("index.html").exists():
        return FileResponse("index.html")
    return {"status": "ok"}


@app.get("/api/cells")
def get_cells():
    db = load_db()
    today = datetime.date.today().isoformat()
    busy = set()
    for b in db.get("bookings", []):
        if b.get("date") == today or "Суточное" in b.get("tariff", ""):
            busy.add(b["cell"])
    return {"busy": sorted(busy), "total": 100}


@app.post("/api/booking")
async def create_booking(req: BookingRequest):
    db = load_db()
    for b in db.get("bookings", []):
        if b["cell"] == req.cell and b.get("date") == req.date:
            raise HTTPException(409, "Ячейка уже занята на эту дату")
    booking = req.model_dump()
    booking["created_at"] = datetime.datetime.now().isoformat()
    booking["status"] = "active"
    db.setdefault("bookings", []).append(booking)
    save_db(db)
    # Уведомления в фоне
    asyncio.create_task(send_user_confirmation(booking))
    asyncio.create_task(notify_admin(booking))
    return {"success": True, "booking_id": req.booking_id}


@app.get("/api/bookings")
def list_bookings(date: Optional[str] = None):
    db = load_db()
    bookings = db.get("bookings", [])
    if date:
        bookings = [b for b in bookings if b.get("date") == date]
    return {"bookings": bookings, "count": len(bookings)}


@app.delete("/api/booking/{booking_id}")
def cancel_booking(booking_id: str):
    db = load_db()
    bookings = db.get("bookings", [])
    new_list = [b for b in bookings if b["booking_id"] != booking_id]
    if len(new_list) == len(bookings):
        raise HTTPException(404, "Бронирование не найдено")
    db["bookings"] = new_list
    save_db(db)
    return {"success": True}


# ─────────── ЗАПУСК ───────────

if __name__ == "__main__":
    print(f"🚀 Старт на порту {PORT}")
    print(f"   BOT_TOKEN: {'✅ задан' if BOT_TOKEN else '❌ НЕ задан'}")
    print(f"   WEBAPP_URL: {WEBAPP_URL or '❌ НЕ задан'}")
    print(f"   ADMIN_CHAT_ID: {ADMIN_CHAT_ID or '❌ НЕ задан'}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
