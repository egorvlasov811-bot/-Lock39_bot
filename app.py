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
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import uvicorn

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, WebAppInfo, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand, BotCommandScopeChat
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import qrcode

# ─────────── НАСТРОЙКИ ───────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", "8000"))
DB_FILE = "bookings.json"
TOTAL_PLACES = 500


# ─────────── АДМИНЫ ───────────
# MAIN_ADMIN_ID — главный админ (владелец). Может всё, получает личные
# сообщения от клиентов через /contact.
# ADMIN_IDS — список через запятую: "111,222,333". Видят брони, получают
# уведомления о новых бронях/отменах/no-show и могут управлять бронями.
# Главный админ автоматически добавляется в ADMIN_IDS.
# Для обратной совместимости: если задан старый ADMIN_CHAT_ID — используем его как MAIN_ADMIN_ID.

def _parse_int_or_zero(s: str) -> int:
    try:
        return int((s or "").strip())
    except (TypeError, ValueError):
        return 0


def _parse_admin_id_list(s: str) -> set:
    ids = set()
    for part in (s or "").replace(";", ",").split(","):
        n = _parse_int_or_zero(part)
        if n:
            ids.add(n)
    return ids


MAIN_ADMIN_ID: int = _parse_int_or_zero(os.getenv("MAIN_ADMIN_ID", "")) \
                     or _parse_int_or_zero(os.getenv("ADMIN_CHAT_ID", ""))
ADMIN_IDS: set = _parse_admin_id_list(os.getenv("ADMIN_IDS", ""))
if MAIN_ADMIN_ID:
    ADMIN_IDS.add(MAIN_ADMIN_ID)

# Старая переменная — оставляем как алиас на главного, чтобы не ломать вызовы.
ADMIN_CHAT_ID = str(MAIN_ADMIN_ID) if MAIN_ADMIN_ID else ""


def is_admin(user_id) -> bool:
    """Любой админ (главный или приёмщик)."""
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


def is_main_admin(user_id) -> bool:
    """Только главный админ (владелец)."""
    try:
        return MAIN_ADMIN_ID != 0 and int(user_id) == MAIN_ADMIN_ID
    except (TypeError, ValueError):
        return False

# ─────────── ИНФО О КАМЕРЕ ХРАНЕНИЯ ───────────
ADDRESS = "г. Зеленоградск, ул. Железнодорожная, 2Б корп. 1"
ADDRESS_HINT = "Ориентир: железнодорожный вокзал Зеленоградска"
MAPS_URL_YANDEX = "https://yandex.ru/maps/org/kamera_khraneniya_bagazha/245433262999"
MAPS_URL_2GIS = "https://2gis.ru/kaliningrad/geo/70000001101819705"
REVIEW_URL_YANDEX = "https://yandex.ru/maps/org/245433262999/reviews/?add-review=true"
REVIEW_URL_2GIS = "https://2gis.ru/zelenogradsk/firm/70000001101819705/tab/reviews"
# Универсальный URL для кнопки "Карта" (Яндекс по умолчанию — работает в РФ хорошо)
MAPS_URL = MAPS_URL_YANDEX

# Что считается "местом" (единицей)
PLACE_EXAMPLES = "чемодан, рюкзак, пакет, велосипед, самокат, коробка, сумка"

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

def count_active_today() -> int:
    """Сколько броней активны сегодня."""
    db = load_db()
    today = datetime.date.today().isoformat()
    count = 0
    for b in db.get("bookings", []):
        if b.get("status") == "cancelled":
            continue
        if b.get("date") == today or "Суточное" in b.get("tariff", ""):
            count += b.get("items", 1)
    return count


def get_active_booking_for_user(user_id: int) -> Optional[dict]:
    """Возвращает активную бронь юзера, если есть."""
    if not user_id:
        return None
    db = load_db()
    today = datetime.date.today().isoformat()
    for b in db.get("bookings", []):
        if b.get("telegram_user_id") != user_id:
            continue
        if b.get("status") != "active":
            continue
        # Активной считаем, если дата >= сегодня (или суточная и ещё не истекла)
        if b.get("date") >= today or "Суточное" in b.get("tariff", ""):
            return b
    return None


def fmt_date_ru(s: str) -> str:
    """ГГГГ-ММ-ДД → ДД.ММ.ГГГГ"""
    if not s:
        return "—"
    try:
        y, m, d = s.split("-")
        return f"{d}.{m}.{y}"
    except Exception:
        return s


# ─────────── ПРОМОКОДЫ ───────────

PROMO_DISCOUNT = 0.10  # 10%
PROMO_VALID_DAYS = 365  # 1 год
PROMO_EXCLUDED_TARIFFS = ("Вечерний",)  # на эти не действует

def generate_promo_code(user_id: int) -> str:
    """Создаёт промокод вида LOCK39-XXXX-YYYY"""
    import random, string
    chars = string.ascii_uppercase + string.digits
    suffix = "".join(random.choices(chars, k=4))
    return f"LOCK39-{user_id % 10000:04d}-{suffix}"


def issue_promo_for_user(user_id: int, source_booking_id: str) -> dict:
    """Выдаёт промокод пользователю за выданную бронь."""
    db = load_db()
    promos = db.setdefault("promo_codes", [])
    # Проверяем: уже есть неиспользованный промокод?
    for p in promos:
        if p["user_id"] == user_id and not p.get("used"):
            return p
    code = generate_promo_code(user_id)
    promo = {
        "code": code,
        "user_id": user_id,
        "discount": PROMO_DISCOUNT,
        "source_booking": source_booking_id,
        "issued_at": datetime.datetime.now().isoformat(),
        "expires_at": (datetime.datetime.now() + datetime.timedelta(days=PROMO_VALID_DAYS)).isoformat(),
        "used": False,
    }
    promos.append(promo)
    save_db(db)
    return promo


def get_valid_promo_for_user(user_id: int) -> Optional[dict]:
    """Возвращает действующий неиспользованный промокод юзера, если есть."""
    db = load_db()
    now = datetime.datetime.now()
    for p in db.get("promo_codes", []):
        if p["user_id"] != user_id:
            continue
        if p.get("used"):
            continue
        try:
            expires = datetime.datetime.fromisoformat(p["expires_at"])
            if expires < now:
                continue
        except Exception:
            continue
        return p
    return None


def find_promo_by_code(code: str) -> Optional[dict]:
    """Ищет промокод по строке-коду (без учёта регистра)."""
    db = load_db()
    code_up = code.strip().upper()
    for p in db.get("promo_codes", []):
        if p["code"].upper() == code_up:
            return p
    return None


def mark_promo_used(code: str, booking_id: str):
    """Помечает промокод использованным."""
    db = load_db()
    for p in db.get("promo_codes", []):
        if p["code"].upper() == code.strip().upper():
            p["used"] = True
            p["used_at"] = datetime.datetime.now().isoformat()
            p["used_in_booking"] = booking_id
            save_db(db)
            return


def is_promo_applicable(tariff_text: str) -> bool:
    """Можно ли применить промокод к этому тарифу."""
    for excluded in PROMO_EXCLUDED_TARIFFS:
        if excluded in tariff_text:
            return False
    return True


# ─────────── ВРЕМЯ ОКОНЧАНИЯ БРОНИ ───────────

def calc_booking_end(booking: dict) -> Optional[datetime.datetime]:
    """Вычисляет ожидаемое время окончания брони для авто-выдачи."""
    try:
        date_str = booking.get("date")
        time_str = booking.get("time") or "09:00"
        if not date_str:
            return None
        y, m, d = date_str.split("-")
        try:
            h, mi = time_str.split(":")
            start = datetime.datetime(int(y), int(m), int(d), int(h), int(mi))
        except Exception:
            start = datetime.datetime(int(y), int(m), int(d), 9, 0)

        tariff = booking.get("tariff", "")
        evening_extra = booking.get("evening_extra_hours", 0)

        if "1 час" in tariff and "100" in tariff:
            end = start + datetime.timedelta(hours=1)
        elif "3 часа" in tariff:
            end = start + datetime.timedelta(hours=3)
        elif "Весь день" in tariff:
            # До 19:00 + вечерняя доплата
            end = datetime.datetime(int(y), int(m), int(d), 19 + evening_extra, 0)
        elif "Вечерний" in tariff:
            # start + hours из тарифа (вытащим число)
            import re
            m_ = re.search(r"(\d+)\s*ч", tariff)
            hours = int(m_.group(1)) if m_ else 1
            end = start + datetime.timedelta(hours=hours)
        elif "Сутки" in tariff or "сут" in tariff:
            import re
            m_ = re.search(r"(\d+)\s*сут", tariff)
            days = int(m_.group(1)) if m_ else 1
            end = start + datetime.timedelta(days=days)
        else:
            end = start + datetime.timedelta(hours=1)
        return end
    except Exception as e:
        print(f"[calc_end] {e}")
        return None


# ─────────── QR ───────────

def make_qr_image(data: str) -> bytes:
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
    dp = Dispatcher(storage=MemoryStorage())

    # ──────── FSM: пошаговое бронирование в чате ────────
    class BookFSM(StatesGroup):
        tariff = State()
        hours = State()
        days = State()
        evening = State()
        date = State()
        time = State()
        items = State()
        name = State()
        phone = State()
        promo = State()
        confirm = State()

    # ──────── FSM: связь с админом ────────
    class ContactFSM(StatesGroup):
        message = State()

    def main_menu_kb() -> ReplyKeyboardMarkup:
        """Постоянное меню с кнопками внизу экрана."""
        kb = ReplyKeyboardBuilder()
        kb.row(KeyboardButton(text="📅 Забронировать"))
        kb.row(
            KeyboardButton(text="📋 Мои брони"),
            KeyboardButton(text="❌ Отменить бронь")
        )
        kb.row(
            KeyboardButton(text="💰 Тарифы"),
            KeyboardButton(text="📍 Адрес")
        )
        kb.row(
            KeyboardButton(text="⭐ Оставить отзыв"),
            KeyboardButton(text="📞 Связаться")
        )
        kb.row(KeyboardButton(text="ℹ️ Помощь"))
        return kb.as_markup(resize_keyboard=True, persistent=True)

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message, state: FSMContext):
        await state.clear()
        ikb = InlineKeyboardBuilder()
        if WEBAPP_URL:
            ikb.row(InlineKeyboardButton(
                text="🔐 Открыть мини-приложение",
                web_app=WebAppInfo(url=WEBAPP_URL)
            ))
        ikb.row(InlineKeyboardButton(text="💬 Забронировать в чате", callback_data="start_book"))
        ikb.row(
            InlineKeyboardButton(text="🗺 Яндекс.Карты", url=MAPS_URL_YANDEX),
            InlineKeyboardButton(text="🗺 2ГИС", url=MAPS_URL_2GIS)
        )

        now = datetime.datetime.now()
        text = (
            "👋 *Добро пожаловать в камеру хранения Зеленоградска!*\n\n"
            "Оставьте багаж под надёжным присмотром, пока гуляете по городу или ждёте поезд. "
            "Чемоданы, рюкзаки, велосипеды — поможем сохранить всё.\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Адрес:*\n{ADDRESS}\n"
            f"_{ADDRESS_HINT}_\n\n"
            f"🕐 *Режим работы:* круглосуточно, без выходных\n"
            f"📦 *Вместимость:* 500 мест\n"
            f"🔒 *Охрана:* видеонаблюдение 24/7\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💰 *Наши тарифы:*\n\n"
            "⏱ 1 час — *100 ₽* за место\n"
            "🕒 3 часа — *200 ₽* за место\n"
            "☀️ Весь день (09:00–19:00) — *300 ₽* за место\n"
            "🌙 После 19:00 — *100 ₽/час* за место\n"
            "📦 Сутки — *600 ₽* за место\n\n"
            f"💡 *1 место* = {PLACE_EXAMPLES}\n"
            "_Например: 2 чемодана + 1 рюкзак = 3 места_\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🌙 *Хранение до позднего вечера — в одной брони!*\n"
            "Выберите тариф «☀️ Весь день» и укажите время окончания — "
            "если оно после 19:00, бот сам добавит вечернюю доплату "
            "(*100 ₽/час за место*). Никаких отдельных бронирований.\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🚀 *Как забронировать:*\n\n"
            "1️⃣ Нажмите *📅 Забронировать* внизу\n"
            "2️⃣ Выберите тариф и количество мест\n"
            "3️⃣ Получите QR-код в чат\n"
            "4️⃣ Покажите его при сдаче вещей\n\n"
            "Управление — *кнопками внизу* 👇"
        )
        await message.answer(
            "Меню активировано ✓",
            reply_markup=main_menu_kb()
        )
        await message.answer(text, parse_mode="Markdown", reply_markup=ikb.as_markup())

    # ── Обработчики текстовых кнопок reply-меню ──
    @dp.message(F.text == "📅 Забронировать")
    async def menu_book(message: types.Message, state: FSMContext):
        await cmd_book(message, state)

    @dp.message(F.text == "📋 Мои брони")
    async def menu_mybookings(message: types.Message):
        await cmd_mybookings(message)

    @dp.message(F.text == "❌ Отменить бронь")
    async def menu_cancel(message: types.Message):
        await cmd_cancel(message)

    def _tariffs_text() -> str:
        return (
            "💰 *Тарифы камеры хранения*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "⏱ *1 час* — 100 ₽ за место\n"
            "_Быстрое хранение_\n\n"
            "🕒 *3 часа* — 200 ₽ за место\n"
            "_Удобно для шопинга или экскурсии_\n\n"
            "☀️ *Весь день* — 300 ₽ за место\n"
            "_С 09:00 до 19:00. Самая выгодная цена_\n\n"
            "🌙 *Вечерний* — 100 ₽/час за место\n"
            "_Только по предварительному звонку, после 19:00_\n\n"
            "📦 *Сутки* — 600 ₽ за место\n"
            "_Длительное хранение_\n\n"
            "🚲 *Велочемодан / крупногабарит* — 800 ₽/день\n"
            "_Велосипеды, велочемоданы и прочие негабаритные вещи_\n\n"
            "📦 *Обмотка чемодана плёнкой* — 500 ₽/шт\n"
            "_Защита от грязи и царапин_\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💡 *Что значит «1 место»?*\n"
            f"Это один предмет: {PLACE_EXAMPLES}.\n"
            "Если у вас несколько предметов — выбирайте соответствующее количество мест.\n\n"
            "🎁 *Программа лояльности:*\n"
            "После выдачи багажа мы пришлём вам персональный промокод на скидку 10% на следующий визит. Действует 1 год.\n\n"
            "📌 *Лимиты:*\n"
            "• До 10 мест в одной брони\n"
            "• Одна активная бронь на аккаунт"
        )

    @dp.message(F.text == "💰 Тарифы")
    async def menu_tariffs(message: types.Message):
        await message.answer(_tariffs_text(), parse_mode="Markdown")

    @dp.message(Command("price"))
    async def cmd_price(message: types.Message):
        await message.answer(_tariffs_text(), parse_mode="Markdown")

    @dp.message(F.text == "📍 Адрес")
    async def menu_address(message: types.Message):
        ikb = InlineKeyboardBuilder()
        ikb.row(InlineKeyboardButton(text="🗺 Яндекс.Карты", url=MAPS_URL_YANDEX))
        ikb.row(InlineKeyboardButton(text="🗺 2ГИС", url=MAPS_URL_2GIS))
        await message.answer(
            f"📍 *Адрес камеры хранения*\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"*{ADDRESS}*\n\n"
            f"📌 {ADDRESS_HINT}\n\n"
            f"🕐 *Режим работы:*\n"
            f"Ежедневно, круглосуточно\n\n"
            f"⭐ *Найти нас на картах и оставить отзыв:*\n"
            f"Поможете нам расти 🙏",
            parse_mode="Markdown",
            reply_markup=ikb.as_markup()
        )

    @dp.message(F.text == "📞 Связаться")
    async def menu_contact(message: types.Message, state: FSMContext):
        await state.set_state(ContactFSM.message)
        await message.answer(
            "📞 *Связь с администрацией*\n\n"
            "Напишите ваш вопрос одним сообщением — мы ответим в ближайшее время.\n\n"
            "_Чтобы отменить — нажмите любую кнопку меню._",
            parse_mode="Markdown"
        )

    @dp.message(F.text == "⭐ Оставить отзыв")
    async def menu_review(message: types.Message):
        """Просим оставить отзыв. Отправляем QR + ссылки."""
        ikb = InlineKeyboardBuilder()
        ikb.row(InlineKeyboardButton(text="⭐ Отзыв на Яндекс.Картах", url=REVIEW_URL_YANDEX))
        ikb.row(InlineKeyboardButton(text="⭐ Отзыв на 2ГИС", url=REVIEW_URL_2GIS))

        caption = (
            "⭐ *Ваш отзыв очень важен для нас!*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Понравился сервис? Поделитесь впечатлениями — это помогает нам "
            "становиться лучше и помогает другим путешественникам найти нас.\n\n"
            "🙏 *Спасибо, что вы с нами!*\n\n"
            "Нажмите одну из кнопок ниже или отсканируйте QR-код 👇"
        )

        # Создаём QR с двумя ссылками (по одной за раз — Telegram не даст две картинки + caption в одном сообщении)
        # Шлём отзыв-Яндекс с QR, потом отзыв-2ГИС с QR
        try:
            # Объединённый QR — на Яндекс (приоритетнее по объёму отзывов)
            qr_bytes = make_qr_image(REVIEW_URL_YANDEX)
            photo = BufferedInputFile(qr_bytes, filename="review_yandex.png")
            await message.answer_photo(
                photo=photo,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=ikb.as_markup()
            )
            # Второй QR — на 2ГИС отдельным сообщением
            qr_bytes_2gis = make_qr_image(REVIEW_URL_2GIS)
            photo_2gis = BufferedInputFile(qr_bytes_2gis, filename="review_2gis.png")
            await message.answer_photo(
                photo=photo_2gis,
                caption="📲 *QR-код для отзыва на 2ГИС*",
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"[review] {e}")
            await message.answer(caption, parse_mode="Markdown", reply_markup=ikb.as_markup())

    @dp.message(F.text == "ℹ️ Помощь")
    async def menu_help(message: types.Message):
        await cmd_help(message)

    @dp.callback_query(F.data == "start_book")
    async def cb_start_book(callback: types.CallbackQuery, state: FSMContext):
        """Запустить FSM-бронирование из меню /start."""
        await callback.answer()
        await cmd_book(callback.message, state)

    # ──────── /info — показать активную бронь ────────
    @dp.message(Command("info"))
    async def cmd_info(message: types.Message):
        booking = get_active_booking_for_user(message.from_user.id)
        if not booking:
            await message.answer(
                "У вас нет активной брони.\n\n"
                "Нажмите *📅 Забронировать* внизу, чтобы создать новую.",
                parse_mode="Markdown"
            )
            return
        await send_user_confirmation(booking)

    # ──────── /mypromo — мой промокод ────────
    @dp.message(Command("mypromo"))
    async def cmd_mypromo(message: types.Message):
        promo = get_valid_promo_for_user(message.from_user.id)
        if not promo:
            await message.answer(
                "🎁 *Программа лояльности*\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "У вас пока нет активного промокода.\n\n"
                "Воспользуйтесь нашей камерой хранения — после выдачи багажа мы автоматически "
                "пришлём вам персональный *промокод на скидку 10%* на следующий визит.\n\n"
                "Действует 1 год. Применяется ко всем тарифам, кроме вечернего.",
                parse_mode="Markdown"
            )
            return
        try:
            expires = datetime.datetime.fromisoformat(promo["expires_at"])
            expires_str = expires.strftime("%d.%m.%Y")
        except Exception:
            expires_str = "—"
        await message.answer(
            f"🎁 *Ваш промокод*\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"`{promo['code']}`\n\n"
            f"💸 Скидка: *10%*\n"
            f"📅 Действует до: *{expires_str}*\n\n"
            f"_Применяется ко всем тарифам, кроме вечернего._\n\n"
            f"При следующем бронировании введите этот код, когда бот спросит «Есть ли у вас промокод?».",
            parse_mode="Markdown"
        )

    # ──────── /contact — связь с админом ────────
    @dp.message(Command("contact"))
    async def cmd_contact(message: types.Message, state: FSMContext):
        await state.set_state(ContactFSM.message)
        await message.answer(
            "📞 *Связь с администрацией*\n\n"
            "Напишите ваш вопрос одним сообщением — мы получим его и ответим.\n\n"
            "_Чтобы отменить — нажмите любую кнопку меню._",
            parse_mode="Markdown"
        )

    @dp.message(ContactFSM.message, F.text & ~F.text.startswith("/"))
    async def contact_send(message: types.Message, state: FSMContext):
        # Игнорируем нажатия на кнопки меню — они обработаются своими handler'ами
        menu_buttons = ["📅 Забронировать", "📋 Мои брони", "❌ Отменить бронь",
                       "💰 Тарифы", "📍 Адрес", "⭐ Оставить отзыв", "📞 Связаться", "ℹ️ Помощь"]
        if message.text in menu_buttons:
            await state.clear()
            return

        text = (message.text or "").strip()
        if not text:
            await message.answer("Сообщение пустое. Напишите текст:")
            return

        # Пересылаем всем админам
        if ADMIN_IDS:
            try:
                user = message.from_user
                user_info = f"@{user.username}" if user.username else f"id {user.id}"
                name = " ".join(filter(None, [user.first_name, user.last_name])) or "—"
                # Кнопка "Ответить" — открывает чат
                rkb = InlineKeyboardBuilder()
                rkb.add(InlineKeyboardButton(
                    text="💬 Ответить клиенту",
                    url=f"tg://user?id={user.id}"
                ))
                sent = await broadcast_to_admins(
                    (
                        f"📩 *Сообщение от клиента*\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"👤 {name} ({user_info})\n\n"
                        f"💬 {text}"
                    ),
                    reply_markup=rkb.as_markup(),
                )
                await message.answer(
                    "✅ Спасибо! Ваше сообщение отправлено.\n"
                    "Мы ответим в ближайшее время."
                )
            except Exception as e:
                print(f"[contact] {e}")
                await message.answer("❌ Не удалось отправить сообщение. Попробуйте позже.")
        else:
            await message.answer(
                "⚠️ Связь с админом временно недоступна.\n"
                "Попробуйте позже или позвоните по телефону."
            )
        await state.clear()

    # ──────── /admin — мини-CRM (только для админа) ────────
    @dp.message(Command("admin"))
    async def cmd_admin(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещён.")
            return
        db = load_db()
        bookings = db.get("bookings", [])
        promos = db.get("promo_codes", [])
        today = datetime.date.today().isoformat()
        active = [b for b in bookings if b.get("status") == "active"]
        today_b = [b for b in bookings if b.get("date") == today]
        cancelled = [b for b in bookings if b.get("status") == "cancelled"]
        completed = [b for b in bookings if b.get("status") == "completed"]
        total_revenue = sum(b.get("total", 0) for b in bookings if b.get("status") in ("active", "completed"))
        occupied = count_active_today()

        # Промокоды
        promos_active = [p for p in promos if not p.get("used")]
        promos_used = [p for p in promos if p.get("used")]
        # Скидок отдано
        total_discount = sum(b.get("discount_amount", 0) for b in bookings)

        text = (
            f"🔧 *Админ-панель*\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 *Брони:*\n"
            f"• Всего: {len(bookings)}\n"
            f"• Активных: {len(active)}\n"
            f"• На сегодня: {len(today_b)}\n"
            f"• Завершено: {len(completed)}\n"
            f"• Отменено: {len(cancelled)}\n\n"
            f"📦 *Загрузка:*\n"
            f"• Занято: *{occupied}* из {TOTAL_PLACES}\n"
            f"• Свободно: *{TOTAL_PLACES - occupied}*\n\n"
            f"🎁 *Программа лояльности:*\n"
            f"• Выдано промокодов: {len(promos)}\n"
            f"• Использовано: {len(promos_used)}\n"
            f"• Активных у клиентов: {len(promos_active)}\n"
            f"• Сумма скидок: {total_discount:,} ₽\n\n"
            f"💵 *Выручка:* {total_revenue:,} ₽"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📋 Активные брони (выдать)", callback_data="adm:active"))
        kb.row(InlineKeyboardButton(text="📅 Брони на сегодня", callback_data="adm:today"))
        await message.answer(text, parse_mode="Markdown", reply_markup=kb.as_markup())

    @dp.callback_query(F.data == "adm:active")
    async def adm_active(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        db = load_db()
        active = [b for b in db.get("bookings", []) if b.get("status") == "active"]
        if not active:
            await callback.answer("Активных броней нет", show_alert=True)
            return
        for b in active[-10:]:
            text = (
                f"🆔 `{b['booking_id']}`\n"
                f"📦 Место №{b.get('place_num','—')} • {b['items']} шт\n"
                f"💰 {b['tariff']}\n"
                f"📅 {fmt_date_ru(b['date'])} {b.get('time') or ''}\n"
                f"👤 {b['name']} • 📞 {b['phone']}\n"
                f"💵 {b['total']:,} ₽"
            )
            kb_b = InlineKeyboardBuilder()
            kb_b.row(InlineKeyboardButton(
                text="✅ Выдан — отправить отзыв и промокод",
                callback_data=f"adm:pickup:{b['booking_id']}"
            ))
            await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb_b.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("adm:pickup:"))
    async def adm_pickup(callback: types.CallbackQuery):
        """Ручная выдача багажа — шлёт юзеру отзыв и промокод."""
        if not is_admin(callback.from_user.id):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        bid = callback.data.split(":", 2)[2]
        db = load_db()
        booking = next((b for b in db.get("bookings", []) if b["booking_id"] == bid), None)
        if not booking:
            await callback.answer("Бронь не найдена", show_alert=True)
            return
        if booking.get("status") != "active":
            await callback.answer(f"Бронь уже не активна (статус: {booking.get('status')})", show_alert=True)
            return
        # Шлём отзыв и промокод
        try:
            await send_review_and_promo(booking)
            booking["status"] = "completed"
            booking["review_requested"] = True
            booking["completed_at"] = datetime.datetime.now().isoformat()
            booking["completed_by"] = "admin_manual"
            save_db(db)
            await callback.answer("✅ Отправлено клиенту", show_alert=True)
            try:
                await callback.message.edit_text(
                    callback.message.text + "\n\n☑️ *Выдан, отзыв и промокод отправлены*",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        except Exception as e:
            print(f"[adm_pickup] {e}")
            await callback.answer(f"Ошибка: {e}", show_alert=True)

    @dp.callback_query(F.data == "adm:today")
    async def adm_today(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        db = load_db()
        today = datetime.date.today().isoformat()
        todays = [b for b in db.get("bookings", []) if b.get("date") == today]
        if not todays:
            await callback.answer("На сегодня броней нет", show_alert=True)
            return
        for b in todays:
            status = {"active":"✅","cancelled":"❌","completed":"☑️"}.get(b.get("status","active"),"")
            text = (
                f"{status} `{b['booking_id']}`\n"
                f"📦 Место №{b.get('place_num','—')} • {b['items']} шт\n"
                f"⏰ {b.get('time') or '—'}\n"
                f"👤 {b['name']} • 📞 {b['phone']}\n"
                f"💵 {b['total']:,} ₽"
            )
            await callback.message.answer(text, parse_mode="Markdown")
        await callback.answer()

    # ──────── РАСШИРЕННАЯ CRM ДЛЯ АДМИНОВ ────────

    def _booking_short_line(b: dict) -> str:
        """Однострочный формат брони для списков."""
        st = {"active": "🟢", "cancelled": "❌", "completed": "✅"}.get(b.get("status", "active"), "•")
        place = b.get("place_num", "—")
        items = b.get("items", 1)
        time = b.get("time") or "—"
        total = b.get("total", 0)
        return (
            f"{st} `{b['booking_id']}` №{place} · {items}шт · {time}\n"
            f"   👤 {b.get('name','—')} · 📞 {b.get('phone','—')} · 💵 {total:,}₽"
        )

    async def _send_bookings_list(message: types.Message, bookings: list, title: str):
        """Отправляет админу список броней с разбивкой по 10 в сообщении."""
        if not bookings:
            await message.answer(f"{title}\n\n_Броней нет._", parse_mode="Markdown")
            return
        # Сортировка: по дате, потом по времени
        bookings = sorted(bookings, key=lambda x: (x.get("date", ""), x.get("time") or "00:00"))
        header = f"{title}\n_Всего: {len(bookings)}_\n━━━━━━━━━━━━━━\n"
        lines = [header]
        for b in bookings:
            line = _booking_short_line(b)
            # Дата строкой, если в списке смешано несколько дней
            line = f"📅 {fmt_date_ru(b.get('date','—'))}\n" + line
            lines.append(line)
            if sum(len(x) for x in lines) > 3500:
                await message.answer("\n\n".join(lines), parse_mode="Markdown")
                lines = []
        if lines:
            await message.answer("\n\n".join(lines), parse_mode="Markdown")

    @dp.message(Command("today"))
    async def cmd_today(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещён.")
            return
        today = datetime.date.today().isoformat()
        bookings = [b for b in load_db().get("bookings", []) if b.get("date") == today]
        await _send_bookings_list(message, bookings, f"📅 *Брони на сегодня* ({fmt_date_ru(today)})")

    @dp.message(Command("tomorrow"))
    async def cmd_tomorrow(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещён.")
            return
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        bookings = [b for b in load_db().get("bookings", []) if b.get("date") == tomorrow]
        await _send_bookings_list(message, bookings, f"📅 *Брони на завтра* ({fmt_date_ru(tomorrow)})")

    @dp.message(Command("week"))
    async def cmd_week(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещён.")
            return
        today = datetime.date.today()
        end = today + datetime.timedelta(days=7)
        bookings = [b for b in load_db().get("bookings", [])
                    if b.get("date") and today.isoformat() <= b["date"] <= end.isoformat()]
        await _send_bookings_list(
            message, bookings,
            f"📅 *Брони на неделю* ({fmt_date_ru(today.isoformat())} — {fmt_date_ru(end.isoformat())})"
        )

    @dp.message(Command("stats"))
    async def cmd_stats(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещён.")
            return
        bookings = load_db().get("bookings", [])
        today = datetime.date.today()
        week_ago = (today - datetime.timedelta(days=7)).isoformat()
        month_ago = (today - datetime.timedelta(days=30)).isoformat()

        def stats_for(filter_fn) -> dict:
            bs = [b for b in bookings if filter_fn(b)]
            revenue = sum(b.get("total", 0) for b in bs if b.get("status") in ("active", "completed"))
            return {
                "total": len(bs),
                "active": sum(1 for b in bs if b.get("status") == "active"),
                "completed": sum(1 for b in bs if b.get("status") == "completed"),
                "cancelled": sum(1 for b in bs if b.get("status") == "cancelled"),
                "revenue": revenue,
                "items": sum(b.get("items", 0) for b in bs if b.get("status") in ("active", "completed")),
            }

        s_today = stats_for(lambda b: b.get("date") == today.isoformat())
        s_week = stats_for(lambda b: b.get("date") and b["date"] >= week_ago)
        s_month = stats_for(lambda b: b.get("date") and b["date"] >= month_ago)
        occupied = count_active_today()

        def fmt(s, label):
            return (
                f"*{label}*\n"
                f"  💵 Выручка: *{s['revenue']:,} ₽*\n"
                f"  📋 Броней: {s['total']} (🟢{s['active']} ✅{s['completed']} ❌{s['cancelled']})\n"
                f"  🎒 Мест занято: {s['items']}\n"
            )

        text = (
            f"📊 *Статистика*\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 *Сейчас занято:* {occupied} из {TOTAL_PLACES} мест\n\n"
            f"{fmt(s_today, 'Сегодня')}\n"
            f"{fmt(s_week, 'За 7 дней')}\n"
            f"{fmt(s_month, 'За 30 дней')}"
        )
        await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("find"))
    async def cmd_find(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещён.")
            return
        query = (message.text or "").partition(" ")[2].strip().lower()
        if not query or len(query) < 2:
            await message.answer(
                "🔍 *Поиск клиента*\n\n"
                "Использование: `/find <телефон, имя или ID брони>`\n"
                "Например: `/find Иванов`  или  `/find 79991234567`",
                parse_mode="Markdown"
            )
            return
        # Нормализуем телефон (только цифры)
        query_digits = "".join(c for c in query if c.isdigit())
        bookings = load_db().get("bookings", [])
        matches = []
        for b in bookings:
            hay_name = (b.get("name") or "").lower()
            hay_phone = "".join(c for c in (b.get("phone") or "") if c.isdigit())
            hay_id = (b.get("booking_id") or "").lower()
            if query in hay_name or query in hay_id:
                matches.append(b)
            elif query_digits and len(query_digits) >= 4 and query_digits in hay_phone:
                matches.append(b)
        if not matches:
            await message.answer(f"🔍 По запросу *{query}* ничего не найдено.", parse_mode="Markdown")
            return
        await _send_bookings_list(message, matches, f"🔍 *Результаты поиска:* «{query}»")

    # /newbooking — ручное создание брони админом одной строкой
    _NB_TARIFFS = {
        # ключ → (id_тарифа в TARIFFS_BOT, цена за 1 место за 1 день)
        "hour": ("1", 100, "1 час"),
        "1h":   ("1", 100, "1 час"),
        "1ч":   ("1", 100, "1 час"),
        "3h":   ("2", 200, "3 часа"),
        "3ч":   ("2", 200, "3 часа"),
        "day":  ("3", 300, "Весь день"),
        "день": ("3", 300, "Весь день"),
        "night":("4", 100, "После 19:00"),
        "вечер":("4", 100, "После 19:00"),
        "24h":  ("5", 600, "Сутки"),
        "сутки":("5", 600, "Сутки"),
        "big":  ("5", 800, "Велочемодан/крупногабарит"),
        "вело": ("5", 800, "Велочемодан/крупногабарит"),
    }

    import re as _re

    @dp.message(Command("newbooking"))
    async def cmd_newbooking(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещён.")
            return

        args = (message.text or "").partition(" ")[2].strip()
        usage = (
            "📝 *Создание брони вручную*\n\n"
            "Формат: `/newbooking <тариф> <мест> <имя> <телефон>`\n\n"
            "*Тарифы:*\n"
            "  • `hour` — 1 час (100 ₽)\n"
            "  • `3h` — 3 часа (200 ₽)\n"
            "  • `day` — весь день (300 ₽)\n"
            "  • `night` — после 19:00 (100 ₽/ч)\n"
            "  • `24h` — сутки (600 ₽)\n"
            "  • `big` — велочемодан (800 ₽)\n\n"
            "*Пример:*\n"
            "`/newbooking day 3 Иванов Иван +79991234567`\n\n"
            "_Дата — сегодня, время — текущее. Цена считается автоматически._"
        )
        if not args:
            await message.answer(usage, parse_mode="Markdown")
            return

        tokens = args.split()
        if len(tokens) < 4:
            await message.answer("⚠️ Слишком мало аргументов.\n\n" + usage, parse_mode="Markdown")
            return

        # 1) Тариф
        tariff_key = tokens[0].lower()
        if tariff_key not in _NB_TARIFFS:
            await message.answer(f"⚠️ Неизвестный тариф: `{tokens[0]}`\n\n" + usage, parse_mode="Markdown")
            return
        tariff_id, price_per_item, tariff_label = _NB_TARIFFS[tariff_key]

        # 2) Количество мест
        try:
            items = int(tokens[1])
        except ValueError:
            await message.answer(f"⚠️ Количество мест должно быть числом, не `{tokens[1]}`.", parse_mode="Markdown")
            return
        if items < 1 or items > 10:
            await message.answer("⚠️ Мест должно быть от 1 до 10.")
            return

        # 3) Телефон — последний токен с >=10 цифрами
        rest = tokens[2:]
        phone = None
        phone_idx = None
        for idx in range(len(rest) - 1, -1, -1):
            digits = "".join(c for c in rest[idx] if c.isdigit())
            if len(digits) >= 10:
                phone = rest[idx]
                phone_idx = idx
                break
        if phone is None:
            await message.answer(
                "⚠️ Не нашла номер телефона. "
                "Укажите его последним аргументом (минимум 10 цифр, например `+79991234567`).",
            )
            return

        # 4) Имя — всё что между мест и телефоном
        name_tokens = rest[:phone_idx]
        name = " ".join(name_tokens).strip()
        if not name:
            await message.answer("⚠️ Не нашла имя клиента.")
            return

        # Проверка свободных мест
        free = TOTAL_PLACES - count_active_today()
        if items > free:
            await message.answer(f"⚠️ Недостаточно мест. Свободно: {free}.")
            return

        # Собираем бронь
        now = datetime.datetime.now()
        booking_id = f"L{now.strftime('%y%m%d%H%M%S')}{message.from_user.id % 100:02d}"
        total = price_per_item * items
        place_num = (TOTAL_PLACES - free) + 1

        booking = {
            "booking_id": booking_id,
            "place_num": place_num,
            "tariff": f"{tariff_label} — {price_per_item} ₽/место",
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M"),
            "items": items,
            "name": name,
            "phone": phone,
            "total": total,
            "telegram_user_id": None,
            "telegram_username": None,
            "created_at": now.isoformat(),
            "created_by_admin": message.from_user.id,
            "status": "active",
        }

        db = load_db()
        db.setdefault("bookings", []).append(booking)
        save_db(db)

        await message.answer(
            f"✅ *Бронь создана*\n"
            f"━━━━━━━━━━━━━━\n"
            f"🆔 `{booking_id}`\n"
            f"📦 Место №{place_num}\n"
            f"💰 {tariff_label}\n"
            f"🎒 {items} шт\n"
            f"👤 {name}\n"
            f"📞 {phone}\n"
            f"💵 *{total:,} ₽*",
            parse_mode="Markdown"
        )
        # Уведомить остальных админов о ручном создании
        await broadcast_to_admins(
            f"📝 *Бронь создана вручную*\n"
            f"━━━━━━━━━━━━━━\n"
            f"🆔 `{booking_id}`\n"
            f"👤 {name} · 📞 {phone}\n"
            f"💰 {tariff_label} · 🎒 {items} шт · 💵 {total:,} ₽\n"
            f"_Админ: {message.from_user.full_name or message.from_user.id}_"
        )

    @dp.message(Command("help"))
    async def cmd_help(message: types.Message):
        await message.answer(
            "ℹ️ *Справка по боту*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📍 *Адрес:* {ADDRESS}\n"
            f"_{ADDRESS_HINT}_\n\n"
            f"💡 *1 место* = {PLACE_EXAMPLES}\n"
            "_Например: 2 чемодана + 1 рюкзак = 3 места_\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📲 *3 способа управления:*\n\n"
            "*1️⃣ Кнопки внизу экрана* — самый простой\n"
            "Используйте постоянное меню под полем ввода.\n\n"
            "*2️⃣ Кнопка «/» слева от поля ввода*\n"
            "Открывает список всех команд.\n\n"
            "*3️⃣ Команды вручную:*\n"
            "/start — главное меню\n"
            "/book — забронировать через чат\n"
            "/price — тарифы и цены\n"
            "/info — посмотреть мою активную бронь\n"
            "/mybookings — история бронирований\n"
            "/mypromo — мой промокод на 10% скидку\n"
            "/cancel — отменить активную бронь\n"
            "/contact — написать администрации\n"
            "/help — эта справка\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎁 *Программа лояльности:*\n"
            "После каждой выдачи багажа мы пришлём вам персональный промокод на 10% на следующий визит. Действует 1 год.\n\n"
            "🔐 *Мини-приложение* — открывается через синюю кнопку «Меню» снизу слева или из /start.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )

    # Русские алиасы команд (для удобства)
    @dp.message(F.text.in_(["/прайс", "/Прайс", "/ПРАЙС"]))
    async def ru_alias_price(message: types.Message):
        await cmd_price(message)

    @dp.message(F.text.in_(["/забронировать", "/Забронировать"]))
    async def ru_alias_book(message: types.Message, state: FSMContext):
        await cmd_book(message, state)

    @dp.message(F.text.in_(["/тарифы", "/Тарифы"]))
    async def ru_alias_tariffs(message: types.Message):
        await cmd_price(message)

    @dp.message(F.text.in_(["/промокод", "/Промокод"]))
    async def ru_alias_promo(message: types.Message):
        await cmd_mypromo(message)

    @dp.message(Command("mybookings"))
    async def cmd_mybookings(message: types.Message):
        db = load_db()
        my = [b for b in db.get("bookings", []) if b.get("telegram_user_id") == message.from_user.id]
        if not my:
            await message.answer("У вас пока нет бронирований.\nЧтобы создать — /start")
            return
        for b in my[-5:]:
            status_label = {"active":"✅ Активна","cancelled":"❌ Отменена","completed":"☑️ Завершена"}.get(b.get("status","active"),"")
            text = (
                f"🔐 *Бронирование* {status_label}\n"
                f"🆔 `{b['booking_id']}`\n"
                f"📦 Место №{b.get('place_num','—')}\n"
                f"💰 {b['tariff']}\n"
                f"📅 {fmt_date_ru(b['date'])} {b.get('time') or ''}\n"
                f"🎒 {b['items']} шт\n"
                f"💵 {b['total']:,} ₽"
            )
            await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: types.Message):
        """Отмена активной брони с подтверждением."""
        booking = get_active_booking_for_user(message.from_user.id)
        if not booking:
            await message.answer(
                "У вас нет активной брони, нечего отменять.\n"
                "Чтобы создать новую — /start"
            )
            return

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"cancel_yes:{booking['booking_id']}"),
            InlineKeyboardButton(text="↩️ Нет, оставить", callback_data="cancel_no")
        )
        text = (
            f"⚠️ *Отмена бронирования*\n\n"
            f"Вы действительно хотите отменить бронь?\n\n"
            f"🆔 `{booking['booking_id']}`\n"
            f"📦 Место №{booking.get('place_num','—')}\n"
            f"💰 {booking['tariff']}\n"
            f"📅 {fmt_date_ru(booking['date'])} {booking.get('time') or ''}\n"
            f"🎒 {booking['items']} шт\n"
            f"💵 {booking['total']:,} ₽\n\n"
            f"⚠️ *Это действие необратимо.* После отмены вы сможете создать новую бронь."
        )
        await message.answer(text, parse_mode="Markdown", reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith("ask_cancel:"))
    async def cb_ask_cancel(callback: types.CallbackQuery):
        """Запрос подтверждения отмены (нажата кнопка под QR)."""
        booking_id = callback.data.split(":", 1)[1]
        db = load_db()
        target = None
        for b in db.get("bookings", []):
            if b["booking_id"] == booking_id and b.get("telegram_user_id") == callback.from_user.id:
                target = b
                break
        if not target:
            await callback.answer("Бронь не найдена", show_alert=True)
            return
        if target.get("status") == "cancelled":
            await callback.answer("Эта бронь уже отменена", show_alert=True)
            return

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"cancel_yes:{booking_id}"),
            InlineKeyboardButton(text="↩️ Нет", callback_data="cancel_no")
        )
        await callback.message.answer(
            f"⚠️ *Подтвердите отмену*\n\n"
            f"Точно отменить бронь?\n"
            f"🆔 `{booking_id}`\n"
            f"📦 Место №{target.get('place_num','—')}\n"
            f"💵 {target['total']:,} ₽\n\n"
            f"⚠️ Действие необратимо.",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("cancel_yes:"))
    async def cb_cancel_yes(callback: types.CallbackQuery):
        """Подтверждение отмены."""
        booking_id = callback.data.split(":", 1)[1]
        db = load_db()
        target = None
        for b in db.get("bookings", []):
            if b["booking_id"] == booking_id and b.get("telegram_user_id") == callback.from_user.id:
                target = b
                break

        if not target:
            await callback.answer("Бронь не найдена", show_alert=True)
            return
        if target.get("status") == "cancelled":
            await callback.answer("Эта бронь уже отменена", show_alert=True)
            return

        target["status"] = "cancelled"
        target["cancelled_at"] = datetime.datetime.now().isoformat()
        save_db(db)

        # Уведомить юзера
        await callback.message.edit_text(
            f"❌ *Бронь отменена*\n\n"
            f"🆔 `{booking_id}`\n\n"
            f"Места освобождены. Теперь вы можете создать новую бронь через /start",
            parse_mode="Markdown"
        )
        await callback.answer("Бронь отменена")

        # Уведомить всех админов
        if ADMIN_IDS:
            user_info = f" (@{target.get('telegram_username')})" if target.get("telegram_username") else ""
            await broadcast_to_admins(
                f"❌ *Бронь отменена пользователем*\n"
                f"━━━━━━━━━━━━━━\n"
                f"🆔 `{booking_id}`\n"
                f"📦 Место №{target.get('place_num','—')}\n"
                f"👤 {target['name']}{user_info}\n"
                f"📞 {target['phone']}\n"
                f"💵 Сумма была: {target['total']:,} ₽"
            )

    @dp.callback_query(F.data == "cancel_no")
    async def cb_cancel_no(callback: types.CallbackQuery):
        """Передумали отменять."""
        await callback.message.edit_text(
            "✅ Хорошо, бронь сохранена.\n\n"
            "Чтобы посмотреть детали — /mybookings"
        )
        await callback.answer("Бронь не отменена")

    # ════════════════════════════════════════
    # БРОНИРОВАНИЕ ЧЕРЕЗ ЧАТ (FSM)
    # ════════════════════════════════════════

    TARIFFS_BOT = {
        "1": {"name": "1 час — 100 ₽/место", "price_per_item": 100, "needs_time": True, "hours_duration": 1},
        "2": {"name": "3 часа — 200 ₽/место", "price_per_item": 200, "needs_time": True, "hours_duration": 3},
        "3": {"name": "Весь день (09:00–19:00) — 300 ₽/место", "price_per_item": 300, "needs_time": False, "fixed_window": (9, 19)},
        "4": {"name": "Вечерний (после 19:00) — 100 ₽/час", "price_per_item": 100, "needs_time": True, "needs_hours": True, "evening_only": True},
        "5": {"name": "Сутки — 600 ₽/место", "price_per_item": 600, "needs_time": False, "needs_days": True},
    }

    # ⏰ ВЕЧЕРНЕЕ ОКНО — после 19:00 каждый час по 100 ₽/место
    EVENING_START = 19  # час, с которого начинается вечерний тариф

    def get_available_tariffs(now: datetime.datetime = None) -> list:
        """
        Возвращает список доступных тарифов на сегодня с учётом текущего времени.
        Логика:
        - Тариф 1 (1 час): доступен только если до 19:00 осталось хотя бы 1 час
        - Тариф 2 (3 часа): доступен только если до 19:00 осталось хотя бы 3 часа (или если хочется до вечера)
        - Тариф 3 (весь день): доступен только если сейчас < 19:00 (можно начать)
        - Тариф 4 (вечерний): доступен в любое время (для брони на вечер)
        - Тариф 5 (сутки): всегда доступен
        """
        if now is None:
            now = datetime.datetime.now()
        h = now.hour
        available = []
        # Тариф 1 — нужен хотя бы 1 час до 19:00 (либо брать на завтра)
        # На сегодня доступен если сейчас < 18:00, иначе только на завтра/вечер
        available.append("1")
        # Тариф 2 — 3 часа подряд
        available.append("2")
        # Тариф 3 — весь день. Логичен только если ещё ДЕНЬ (до 19:00)
        if h < 19:
            available.append("3")
        # Тариф 4 — вечерний, всегда есть
        available.append("4")
        # Тариф 5 — сутки, всегда
        available.append("5")
        return available

    def calc_booking_total(data: dict) -> int:
        """Базовая сумма по выбранному тарифу."""
        t = data["tariff"]
        items = data["items"]
        price = TARIFFS_BOT[t]["price_per_item"]
        if t == "4":
            return price * data.get("hours", 1) * items
        if t == "5":
            return price * data.get("days", 1) * items
        return price * items

    def calc_evening_extra(data: dict) -> int:
        """
        Доплата за хранение после 19:00 для тарифов 1/2/3.
        Считаем: end_time = start_time + длительность тарифа.
        Если end_time > 19:00 — доплата по 100 ₽/час/место за каждый час сверх 19:00.
        Для тарифа 3 (весь день) считаем end = до желаемого времени окончания.
        """
        t = data["tariff"]
        if t in ("4", "5"):
            return 0  # вечерний/суточный — без доплаты
        items = data["items"]
        # extra_hours указано в data?
        extra_hours = data.get("evening_extra_hours", 0)
        return 100 * extra_hours * items

    def calc_full_total(data: dict) -> int:
        """Полная сумма с учётом возможной вечерней доплаты."""
        return calc_booking_total(data) + calc_evening_extra(data)

    def tariff_label_bot(data: dict) -> str:
        t = data["tariff"]
        if t == "4":
            return f"Вечерний, {data.get('hours',1)} ч × 100 ₽"
        if t == "5":
            return f"Сутки, {data.get('days',1)} сут × 600 ₽"
        if t == "3":
            extra = data.get("evening_extra_hours", 0)
            if extra > 0:
                end_time = 19 + extra
                return f"Весь день до {end_time}:00 (с вечерней доплатой)"
            return TARIFFS_BOT[t]["name"]
        return TARIFFS_BOT[t]["name"]

    @dp.message(Command("book"))
    async def cmd_book(message: types.Message, state: FSMContext):
        """Начало бронирования через чат."""
        existing = get_active_booking_for_user(message.from_user.id)
        if existing:
            await message.answer(
                f"⚠️ *У вас уже есть активная бронь:*\n"
                f"🆔 `{existing['booking_id']}`\n"
                f"📦 Мест: {existing.get('items',1)}\n\n"
                f"Чтобы создать новую — сначала отмените текущую\n"
                f"(нажмите *❌ Отменить бронь* внизу или /cancel).",
                parse_mode="Markdown"
            )
            return

        free = TOTAL_PLACES - count_active_today()
        if free <= 0:
            await message.answer("😔 К сожалению, все места заняты. Попробуйте позже.")
            return

        await state.clear()
        now = datetime.datetime.now()
        available = get_available_tariffs(now)

        # Создаём кнопки только доступных тарифов
        kb = InlineKeyboardBuilder()
        tariff_buttons = {
            "1": "⏱ 1 час — 100 ₽ за место",
            "2": "🕒 3 часа — 200 ₽ за место",
            "3": "☀️ Весь день — 300 ₽ за место",
            "4": "🌙 Вечерний — 100 ₽/час за место",
            "5": "📦 Сутки — 600 ₽ за место",
        }
        for t_id in ["1", "2", "3", "4", "5"]:
            if t_id in available:
                kb.row(InlineKeyboardButton(text=tariff_buttons[t_id], callback_data=f"bt:{t_id}"))
        kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="bt:cancel"))

        # Подсказка про вечернее время
        time_hint = ""
        if now.hour >= 19:
            time_hint = (
                f"\n🌙 *Сейчас уже вечер* ({now.strftime('%H:%M')}).\n"
                f"Тариф «Весь день» недоступен — используйте *🌙 Вечерний* "
                f"или *📦 Сутки*.\n"
            )
        elif now.hour >= 16:
            hours_left = 19 - now.hour
            time_hint = (
                f"\n⏰ *До 19:00 осталось ~{hours_left} ч.*\n"
                f"Если хранение нужно дольше — выбирайте тариф «Весь день» "
                f"(*с авто-доплатой 100 ₽/час* после 19:00) или «Сутки».\n"
            )

        await message.answer(
            f"🔐 *Новое бронирование*\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📍 {ADDRESS}\n"
            f"_{ADDRESS_HINT}_\n\n"
            f"🕐 Сейчас: *{now.strftime('%H:%M')}*\n"
            f"📦 Свободно мест: *{free}* из {TOTAL_PLACES}\n"
            f"{time_hint}\n"
            f"💡 *1 место* = {PLACE_EXAMPLES}\n\n"
            f"🌙 *Нужно хранение и днём, и вечером?*\n"
            f"Выберите *☀️ Весь день* — на следующем шаге укажете "
            f"время окончания (хоть до 23:00). Бот сам посчитает доплату.\n\n"
            f"*Шаг 1:* Выберите тариф 👇",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        await state.set_state(BookFSM.tariff)

    @dp.callback_query(F.data == "bt:cancel")
    async def bt_cancel(callback: types.CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.edit_text("❌ Бронирование отменено.")
        await callback.answer()

    @dp.callback_query(BookFSM.tariff, F.data.startswith("bt:"))
    async def bt_tariff(callback: types.CallbackQuery, state: FSMContext):
        t = callback.data.split(":")[1]
        if t not in TARIFFS_BOT:
            await callback.answer("Неверный тариф", show_alert=True)
            return
        await state.update_data(tariff=t)
        tariff_info = TARIFFS_BOT[t]
        await callback.answer()

        # Тариф 4 — спросить количество часов
        if tariff_info.get("needs_hours"):
            kb = InlineKeyboardBuilder()
            for h in [1, 2, 3, 4, 5, 6]:
                kb.button(text=f"{h} ч", callback_data=f"bh:{h}")
            kb.adjust(3)
            await callback.message.edit_text(
                f"✓ Тариф: *{tariff_info['name']}*\n\n"
                f"🌙 *Вечерний тариф работает с 19:00*\n"
                f"_Стоимость: 100 ₽/час за каждое место_\n\n"
                f"Сколько часов хранения нужно?",
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
            await state.set_state(BookFSM.hours)
            return

        # Тариф 5 — спросить количество суток
        if tariff_info.get("needs_days"):
            kb = InlineKeyboardBuilder()
            for d in [1, 2, 3, 5, 7, 14]:
                kb.button(text=f"{d} сут", callback_data=f"bd:{d}")
            kb.adjust(3)
            await callback.message.edit_text(
                f"✓ Тариф: *{tariff_info['name']}*\n\n"
                f"Сколько суток хранения?",
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
            await state.set_state(BookFSM.days)
            return

        # Тариф 3 — спросить, нужна ли доплата за вечер
        if t == "3":
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="✓ Только день — до 19:00", callback_data="bevening:0"))
            kb.row(InlineKeyboardButton(text="🌙 День + до 20:00 (+100 ₽/место)", callback_data="bevening:1"))
            kb.row(InlineKeyboardButton(text="🌙 День + до 21:00 (+200 ₽/место)", callback_data="bevening:2"))
            kb.row(InlineKeyboardButton(text="🌙 День + до 22:00 (+300 ₽/место)", callback_data="bevening:3"))
            kb.row(InlineKeyboardButton(text="🌙 День + до 23:00 (+400 ₽/место)", callback_data="bevening:4"))
            await callback.message.edit_text(
                f"✓ Тариф: *{tariff_info['name']}*\n\n"
                f"☀️ Базовый «Весь день» = *09:00–19:00*, *300 ₽ за место*.\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🌙 *Можно продлить до позднего вечера!*\n"
                f"Не нужно создавать вторую бронь — просто выберите время окончания. "
                f"К базовой сумме автоматически добавится вечерний тариф "
                f"(*+100 ₽/час за каждое место* после 19:00).\n\n"
                f"💡 *Пример:* 2 чемодана с 12:00 до 22:00\n"
                f"  • День (09–19): 2 × 300 = 600 ₽\n"
                f"  • Вечер (19–22, 3 ч): 2 × 3 × 100 = 600 ₽\n"
                f"  • *Итого: 1 200 ₽*\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"До какого времени вам нужно хранение?",
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
            await state.set_state(BookFSM.evening)
            return

        # Иначе — сразу к дате
        await ask_date(callback.message, state, edit=True, tariff_name=tariff_info['name'])

    @dp.callback_query(BookFSM.evening, F.data.startswith("bevening:"))
    async def bt_evening(callback: types.CallbackQuery, state: FSMContext):
        """Выбор доплаты за вечер для тарифа «Весь день»."""
        extra_hours = int(callback.data.split(":")[1])
        await state.update_data(evening_extra_hours=extra_hours)
        await callback.answer()
        data = await state.get_data()
        await ask_date(callback.message, state, edit=True, tariff_name=tariff_label_bot(data))

    @dp.callback_query(BookFSM.hours, F.data.startswith("bh:"))
    async def bt_hours(callback: types.CallbackQuery, state: FSMContext):
        hours = int(callback.data.split(":")[1])
        await state.update_data(hours=hours)
        await callback.answer()
        data = await state.get_data()
        await ask_date(callback.message, state, edit=True, tariff_name=tariff_label_bot(data))

    @dp.callback_query(BookFSM.days, F.data.startswith("bd:"))
    async def bt_days(callback: types.CallbackQuery, state: FSMContext):
        days = int(callback.data.split(":")[1])
        await state.update_data(days=days)
        await callback.answer()
        data = await state.get_data()
        await ask_date(callback.message, state, edit=True, tariff_name=tariff_label_bot(data))

    async def ask_date(message: types.Message, state: FSMContext, edit: bool = False, tariff_name: str = ""):
        """Спрашивает дату — кнопки на ближайшие 7 дней."""
        kb = InlineKeyboardBuilder()
        today = datetime.date.today()
        for i in range(7):
            d = today + datetime.timedelta(days=i)
            label = "Сегодня" if i == 0 else ("Завтра" if i == 1 else d.strftime("%d.%m"))
            kb.button(text=label, callback_data=f"bdate:{d.isoformat()}")
        kb.adjust(3)
        text = f"✓ Тариф: *{tariff_name}*\n\nВыберите дату хранения:"
        if edit:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb.as_markup())
        else:
            await message.answer(text, parse_mode="Markdown", reply_markup=kb.as_markup())
        await state.set_state(BookFSM.date)

    @dp.callback_query(BookFSM.date, F.data.startswith("bdate:"))
    async def bt_date(callback: types.CallbackQuery, state: FSMContext):
        date_iso = callback.data.split(":", 1)[1]
        await state.update_data(date=date_iso)
        await callback.answer()
        data = await state.get_data()
        t = data["tariff"]

        # Тариф 3 — время фиксировано 09:00
        if t == "3":
            await state.update_data(time="09:00")
            await ask_items(callback.message, state, edit=True, data=data)
            return
        # Тариф 5 — без времени
        if t == "5":
            await state.update_data(time=None)
            await ask_items(callback.message, state, edit=True, data=data)
            return

        # Иначе — выбираем время кнопками
        kb = InlineKeyboardBuilder()
        if t == "4":
            # Вечерние слоты
            slots = ["19:00", "19:30", "20:00", "20:30", "21:00", "22:00"]
        else:
            # Дневные слоты
            slots = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]
        for s in slots:
            kb.button(text=s, callback_data=f"btime:{s}")
        kb.adjust(3)
        await callback.message.edit_text(
            f"✓ Тариф: *{tariff_label_bot(data)}*\n"
            f"✓ Дата: *{fmt_date_ru(date_iso)}*\n\n"
            f"Выберите время начала:",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        await state.set_state(BookFSM.time)

    @dp.callback_query(BookFSM.time, F.data.startswith("btime:"))
    async def bt_time(callback: types.CallbackQuery, state: FSMContext):
        time_str = callback.data.split(":", 1)[1]
        await state.update_data(time=time_str)
        await callback.answer()
        data = await state.get_data()
        await ask_items(callback.message, state, edit=True, data=data)

    async def ask_items(message: types.Message, state: FSMContext, edit: bool = False, data: dict = None):
        """Спрашивает количество мест."""
        kb = InlineKeyboardBuilder()
        for n in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
            kb.button(text=str(n), callback_data=f"bitems:{n}")
        kb.adjust(5)
        text = (
            f"✓ Тариф: *{tariff_label_bot(data)}*\n"
            f"✓ Дата: *{fmt_date_ru(data['date'])}*"
        )
        if data.get("time"):
            text += f"\n✓ Время: *{data['time']}*"
        text += (
            f"\n\n*Сколько мест нужно?* _(до 10)_\n\n"
            f"💡 *1 место* = {PLACE_EXAMPLES}\n"
            f"_Например: 2 чемодана и рюкзак = 3 места_"
        )
        if edit:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb.as_markup())
        else:
            await message.answer(text, parse_mode="Markdown", reply_markup=kb.as_markup())
        await state.set_state(BookFSM.items)

    @dp.callback_query(BookFSM.items, F.data.startswith("bitems:"))
    async def bt_items(callback: types.CallbackQuery, state: FSMContext):
        items = int(callback.data.split(":")[1])
        # Проверим что мест хватает
        free = TOTAL_PLACES - count_active_today()
        if items > free:
            await callback.answer(f"Свободно только {free} мест", show_alert=True)
            return
        await state.update_data(items=items)
        await callback.answer()
        # Имя — берём из профиля автоматически или просим ввести
        user = callback.from_user
        suggested_name = " ".join(filter(None, [user.first_name, user.last_name]))
        await state.update_data(suggested_name=suggested_name)

        kb = InlineKeyboardBuilder()
        if suggested_name:
            kb.button(text=f"✓ {suggested_name}", callback_data="bname:use")
        kb.button(text="✏️ Ввести другое", callback_data="bname:custom")
        kb.adjust(1)

        await callback.message.edit_text(
            f"Введите ваше имя или используйте имя из профиля Telegram:",
            reply_markup=kb.as_markup()
        )
        await state.set_state(BookFSM.name)

    @dp.callback_query(BookFSM.name, F.data == "bname:use")
    async def bt_name_use(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.update_data(name=data.get("suggested_name", ""))
        await callback.answer()
        await callback.message.edit_text(
            f"✓ Имя: *{data.get('suggested_name','')}*\n\n"
            f"📞 Введите номер телефона:\n"
            f"_Например: +79001234567_",
            parse_mode="Markdown"
        )
        await state.set_state(BookFSM.phone)

    @dp.callback_query(BookFSM.name, F.data == "bname:custom")
    async def bt_name_custom(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.edit_text("Введите ваше имя:")
        # Ждём текстовое сообщение в состоянии name

    @dp.message(BookFSM.name)
    async def bt_name_text(message: types.Message, state: FSMContext):
        name = (message.text or "").strip()
        if len(name) < 2:
            await message.answer("Имя слишком короткое, введите ещё раз:")
            return
        await state.update_data(name=name)
        await message.answer(
            f"✓ Имя: *{name}*\n\n"
            f"📞 Введите номер телефона:\n"
            f"_Например: +79001234567_",
            parse_mode="Markdown"
        )
        await state.set_state(BookFSM.phone)

    @dp.message(BookFSM.phone)
    async def bt_phone(message: types.Message, state: FSMContext):
        phone = (message.text or "").strip()
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) < 10:
            await message.answer("❌ Некорректный номер. Должно быть минимум 10 цифр. Введите ещё раз:")
            return
        await state.update_data(phone=phone)

        data = await state.get_data()
        tariff_text = tariff_label_bot(data)

        # Если тариф вечерний — промокод не применить, пропускаем шаг
        if not is_promo_applicable(tariff_text):
            await state.update_data(promo_code=None, promo_discount=0)
            await _show_booking_summary(message, state)
            return

        # Проверяем, есть ли у юзера действующий промокод
        promo = get_valid_promo_for_user(message.from_user.id)

        kb = InlineKeyboardBuilder()
        if promo:
            try:
                expires = datetime.datetime.fromisoformat(promo["expires_at"])
                expires_str = expires.strftime("%d.%m.%Y")
            except Exception:
                expires_str = "—"
            kb.row(InlineKeyboardButton(
                text=f"🎁 Применить мой код {promo['code']} (−10%)",
                callback_data="promo:auto"
            ))
            kb.row(InlineKeyboardButton(text="✏️ Ввести другой код", callback_data="promo:manual"))
            kb.row(InlineKeyboardButton(text="⏭ Без промокода", callback_data="promo:skip"))
            await message.answer(
                f"🎁 *У вас есть промокод!*\n"
                f"━━━━━━━━━━━━━━\n\n"
                f"`{promo['code']}` — скидка *10%*\n"
                f"_Действует до {expires_str}_\n\n"
                f"Применить его к этому заказу?",
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
        else:
            kb.row(InlineKeyboardButton(text="✏️ Ввести промокод", callback_data="promo:manual"))
            kb.row(InlineKeyboardButton(text="⏭ Без промокода", callback_data="promo:skip"))
            await message.answer(
                "🎁 *Промокод на скидку*\n"
                "━━━━━━━━━━━━━━\n\n"
                "Если у вас есть промокод от прошлого визита — введите его сейчас, чтобы получить скидку 10%.",
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
        await state.set_state(BookFSM.promo)

    @dp.callback_query(BookFSM.promo, F.data == "promo:skip")
    async def bt_promo_skip(callback: types.CallbackQuery, state: FSMContext):
        await state.update_data(promo_code=None, promo_discount=0)
        await callback.answer()
        await _show_booking_summary(callback.message, state)

    @dp.callback_query(BookFSM.promo, F.data == "promo:auto")
    async def bt_promo_auto(callback: types.CallbackQuery, state: FSMContext):
        promo = get_valid_promo_for_user(callback.from_user.id)
        if not promo:
            await callback.answer("Промокод не найден", show_alert=True)
            return
        await state.update_data(promo_code=promo["code"], promo_discount=promo["discount"])
        await callback.answer("✅ Промокод применён")
        await _show_booking_summary(callback.message, state)

    @dp.callback_query(BookFSM.promo, F.data == "promo:manual")
    async def bt_promo_manual(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.answer(
            "✏️ Введите промокод текстом (например `LOCK39-1234-AB12`):",
            parse_mode="Markdown"
        )

    @dp.message(BookFSM.promo, F.text)
    async def bt_promo_input(message: types.Message, state: FSMContext):
        code = (message.text or "").strip()
        # Игнорируем кнопки меню
        menu_buttons = ["📅 Забронировать", "📋 Мои брони", "❌ Отменить бронь",
                       "💰 Тарифы", "📍 Адрес", "⭐ Оставить отзыв", "📞 Связаться", "ℹ️ Помощь"]
        if code in menu_buttons:
            return
        promo = find_promo_by_code(code)
        if not promo:
            await message.answer(
                "❌ Промокод не найден. Проверьте написание или нажмите *⏭ Без промокода* выше.",
                parse_mode="Markdown"
            )
            return
        if promo.get("used"):
            await message.answer("❌ Этот промокод уже использован.")
            return
        try:
            expires = datetime.datetime.fromisoformat(promo["expires_at"])
            if expires < datetime.datetime.now():
                await message.answer("❌ Срок действия этого промокода истёк.")
                return
        except Exception:
            pass
        # Промокод принадлежит другому юзеру — на всякий случай разрешаем (вдруг друг поделился)
        await state.update_data(promo_code=promo["code"], promo_discount=promo["discount"])
        await message.answer(f"✅ Промокод *{promo['code']}* применён! Скидка 10%.", parse_mode="Markdown")
        await _show_booking_summary(message, state)

    async def _show_booking_summary(message_or_msg, state: FSMContext):
        """Показывает финальное резюме брони с учётом промокода."""
        data = await state.get_data()
        base = calc_booking_total(data)
        extra = calc_evening_extra(data)
        subtotal = base + extra
        discount_rate = data.get("promo_discount", 0)
        discount_amount = int(subtotal * discount_rate) if discount_rate else 0
        total = subtotal - discount_amount
        await state.update_data(
            total=total, base_price=base, evening_extra=extra,
            discount_amount=discount_amount,
        )

        price_lines = []
        if extra > 0:
            extra_hours = data.get("evening_extra_hours", 0)
            end_time = 19 + extra_hours
            price_lines.append(f"  • Базовый тариф (09:00–19:00): *{base:,} ₽*")
            price_lines.append(f"  • Доплата за вечер (19:00–{end_time}:00, {extra_hours} ч × 100 ₽ × {data['items']} мест): *{extra:,} ₽*")
        if discount_amount > 0:
            price_lines.append(f"  • Промокод `{data.get('promo_code')}`: *−{discount_amount:,} ₽* _(скидка 10%)_")
        price_breakdown = "\n".join(price_lines)

        text = (
            f"📋 *Проверьте данные брони*\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 Тариф: {tariff_label_bot(data)}\n"
            f"📅 Дата: {fmt_date_ru(data['date'])}\n"
            f"⏰ Время: {data.get('time') or '—'}\n"
            f"🎒 Мест: {data['items']} шт\n"
            f"👤 Имя: {data['name']}\n"
            f"📞 Тел.: {data['phone']}\n"
        )
        if price_breakdown:
            text += f"\n💵 *Расчёт цены:*\n{price_breakdown}\n"
        text += f"\n💳 *Итого: {total:,} ₽*\n\nВсё верно?"

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="bconfirm:yes"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="bconfirm:no")
        )
        await message_or_msg.answer(text, parse_mode="Markdown", reply_markup=kb.as_markup())
        await state.set_state(BookFSM.confirm)

    @dp.callback_query(BookFSM.confirm, F.data == "bconfirm:no")
    async def bt_confirm_no(callback: types.CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.edit_text("❌ Бронирование отменено.\n\nНачать заново — /book")
        await callback.answer()

    @dp.callback_query(BookFSM.confirm, F.data == "bconfirm:yes")
    async def bt_confirm_yes(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await callback.answer("Создаём бронь...")

        # Финальная проверка: ещё раз убеждаемся что нет активной брони и хватает мест
        existing = get_active_booking_for_user(callback.from_user.id)
        if existing:
            await callback.message.edit_text(
                f"⚠️ У вас уже появилась активная бронь:\n"
                f"🆔 `{existing['booking_id']}`\n\n"
                f"Отмените её через /cancel, чтобы создать новую.",
                parse_mode="Markdown"
            )
            await state.clear()
            return

        free = TOTAL_PLACES - count_active_today()
        if data["items"] > free:
            await callback.message.edit_text(
                f"😔 К сожалению, свободно только {free} мест, а вы выбрали {data['items']}.\n"
                f"Начните заново — /book"
            )
            await state.clear()
            return

        # Генерируем ID
        now = datetime.datetime.now()
        bid = f"LS-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')[-4:]}"
        place_num = (TOTAL_PLACES - free) + 1

        booking = {
            "booking_id": bid,
            "place_num": place_num,
            "tariff": tariff_label_bot(data),
            "date": data["date"],
            "time": data.get("time"),
            "items": data["items"],
            "name": data["name"],
            "phone": data["phone"],
            "total": data["total"],
            "base_price": data.get("base_price", data["total"]),
            "evening_extra": data.get("evening_extra", 0),
            "evening_extra_hours": data.get("evening_extra_hours", 0),
            "promo_code": data.get("promo_code"),
            "discount_amount": data.get("discount_amount", 0),
            "telegram_user_id": callback.from_user.id,
            "telegram_username": callback.from_user.username,
            "created_at": now.isoformat(),
            "status": "active",
            "review_requested": False,
            "promo_issued": False,
            "source": "bot_chat",
        }

        db = load_db()
        db.setdefault("bookings", []).append(booking)
        save_db(db)

        # Помечаем промокод использованным
        if data.get("promo_code"):
            mark_promo_used(data["promo_code"], bid)

        await state.clear()

        # Удаляем экран подтверждения
        try:
            await callback.message.edit_text(
                f"✅ Бронь *{bid}* создана!\n\nСейчас пришлю QR-код...",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        # Шлём пользователю подтверждение с QR и админу уведомление
        await send_user_confirmation(booking)
        await notify_admin(booking)


async def send_user_confirmation(booking: dict):
    """Шлёт пользователю подтверждение с QR-кодом."""
    if not bot:
        return
    user_id = booking.get("telegram_user_id")
    if not user_id:
        return
    try:
        base = WEBAPP_URL.rstrip("/") if WEBAPP_URL else ""
        qr_url = f"{base}/b/{booking['booking_id']}" if base else booking['booking_id']
        qr_bytes = make_qr_image(qr_url)

        caption = (
            f"✅ *Бронирование подтверждено!*\n"
            f"━━━━━━━━━━━━━━\n"
            f"🆔 `{booking['booking_id']}`\n"
            f"📦 Место: *№{booking.get('place_num','—')}*\n"
            f"💰 Тариф: {booking['tariff']}\n"
            f"📅 Дата: {fmt_date_ru(booking['date'])}\n"
            f"⏰ Время: {booking.get('time') or '—'}\n"
            f"🎒 Вещей: {booking['items']} шт\n"
            f"👤 Имя: {booking['name']}\n"
            f"📞 Телефон: {booking['phone']}\n"
            f"💵 К оплате: *{booking['total']:,} ₽*\n\n"
            f"📲 Покажите этот QR-код на стойке — сотрудник отсканирует смартфоном."
        )
        photo = BufferedInputFile(qr_bytes, filename=f"booking_{booking['booking_id']}.png")

        # Inline-кнопка отмены прямо под сообщением (с подтверждением)
        kb = InlineKeyboardBuilder()
        kb.add(InlineKeyboardButton(
            text="❌ Отменить бронь",
            callback_data=f"ask_cancel:{booking['booking_id']}"
        ))

        await bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        print(f"[user_confirm] {e}")


async def broadcast_to_admins(text: str, reply_markup=None, parse_mode: str = "Markdown",
                              only_main: bool = False):
    """Шлёт сообщение всем админам (или только главному).
    Возвращает количество успешно доставленных сообщений."""
    if not bot:
        return 0
    targets = {MAIN_ADMIN_ID} if only_main else set(ADMIN_IDS)
    targets.discard(0)
    sent = 0
    for aid in targets:
        try:
            await bot.send_message(chat_id=aid, text=text,
                                   parse_mode=parse_mode, reply_markup=reply_markup)
            sent += 1
        except Exception as e:
            print(f"[broadcast_admins {aid}] {e}")
    return sent


async def notify_admin(booking: dict):
    """Уведомление о новой брони — всем админам."""
    if not bot or not ADMIN_IDS:
        return
    user_info = f" (@{booking['telegram_username']})" if booking.get("telegram_username") else ""
    msg = (
        f"🔔 *Новое бронирование!*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{booking['booking_id']}`\n"
        f"📦 Место: *№{booking.get('place_num','—')}*\n"
        f"💰 Тариф: {booking['tariff']}\n"
        f"📅 Дата: {fmt_date_ru(booking['date'])}\n"
        f"⏰ Время: {booking.get('time') or '—'}\n"
        f"🎒 Вещей: {booking['items']} шт\n"
        f"👤 Имя: {booking['name']}{user_info}\n"
        f"📞 Тел.: {booking['phone']}\n"
        f"💵 Итого: *{booking['total']:,} ₽*"
    )
    await broadcast_to_admins(msg)


async def set_bot_commands():
    """Регистрируем нативное меню команд (кнопка '/' слева от поля ввода).
    Обычные пользователи видят базовый набор, админы — расширенный."""
    if not bot:
        return
    base_commands = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="book", description="📅 Забронировать место"),
        BotCommand(command="price", description="💰 Тарифы и цены"),
        BotCommand(command="info", description="🎫 Моя активная бронь + QR"),
        BotCommand(command="mybookings", description="📋 История бронирований"),
        BotCommand(command="mypromo", description="🎁 Мой промокод (скидка 10%)"),
        BotCommand(command="cancel", description="❌ Отменить активную бронь"),
        BotCommand(command="contact", description="📞 Связаться с администрацией"),
        BotCommand(command="help", description="ℹ️ Помощь и список команд"),
    ]
    admin_commands = base_commands + [
        BotCommand(command="admin",      description="🔧 Админ-панель и сводка"),
        BotCommand(command="today",      description="📅 Брони на сегодня"),
        BotCommand(command="tomorrow",   description="📅 Брони на завтра"),
        BotCommand(command="week",       description="📅 Брони на неделю"),
        BotCommand(command="stats",      description="📊 Статистика и выручка"),
        BotCommand(command="find",       description="🔍 Найти клиента"),
        BotCommand(command="newbooking", description="📝 Создать бронь вручную"),
    ]
    try:
        # Базовое меню — всем
        await bot.set_my_commands(base_commands)
        # Расширенное меню — каждому админу персонально
        for aid in ADMIN_IDS:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=aid))
            except Exception as e:
                print(f"[set_commands admin {aid}] {e}")
        print(f"📋 Меню команд зарегистрировано (базовое + расширенное для {len(ADMIN_IDS)} админов)")
    except Exception as e:
        print(f"[set_commands] {e}")


async def start_bot():
    if not bot or not dp:
        return
    print("🤖 Bot polling запущен")
    await set_bot_commands()
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"[bot] {e}")


# ─────────── ФОНОВАЯ ЗАДАЧА: запрос отзыва + промокод ───────────

async def auto_review_loop():
    """
    Каждые 5 минут проверяет брони, у которых:
    - status == 'active'
    - время окончания + 15 минут уже наступило
    - review_requested != True
    Шлёт юзеру просьбу об отзыве + промокод, помечает бронь как 'completed'.
    """
    if not bot:
        return
    print("🔁 Auto-review loop запущен")
    while True:
        try:
            await asyncio.sleep(300)  # 5 минут
            db = load_db()
            now = datetime.datetime.now()
            changed = False
            for b in db.get("bookings", []):
                if b.get("status") != "active":
                    continue
                if b.get("review_requested"):
                    continue
                end = calc_booking_end(b)
                if not end:
                    continue
                # Через 15 минут после окончания тарифа — пора
                trigger_time = end + datetime.timedelta(minutes=15)
                if now < trigger_time:
                    continue
                # Шлём отзыв + промокод
                user_id = b.get("telegram_user_id")
                if user_id:
                    try:
                        await send_review_and_promo(b)
                        b["review_requested"] = True
                        b["status"] = "completed"
                        b["completed_at"] = now.isoformat()
                        changed = True
                    except Exception as e:
                        print(f"[auto-review] {e}")
            if changed:
                save_db(db)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[auto-review-loop] {e}")


# ─────────── ФОНОВАЯ ЗАДАЧА: уведомления админов ───────────

# Через сколько минут после окончания тарифа считать клиента не пришедшим
NOSHOW_AFTER_MINUTES = 30
# Час, в который отправлять ежедневную сводку (по серверному времени)
DAILY_SUMMARY_HOUR = 20
DAILY_SUMMARY_MINUTE = 0


async def admin_notify_loop():
    """
    Раз в минуту:
    - Проверяет активные брони и шлёт админам пинг по тем, где клиент не пришёл
      (время окончания + NOSHOW_AFTER_MINUTES уже наступило, ставит флаг noshow_notified).
    - В DAILY_SUMMARY_HOUR:DAILY_SUMMARY_MINUTE — отправляет ежедневную сводку (1 раз в сутки).
    """
    if not bot:
        return
    print(f"🔁 Admin-notify loop запущен (сводка в {DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d})")
    while True:
        try:
            await asyncio.sleep(60)
            if not ADMIN_IDS:
                continue
            db = load_db()
            now = datetime.datetime.now()
            changed = False

            # 1) No-show пинги
            for b in db.get("bookings", []):
                if b.get("status") != "active":
                    continue
                if b.get("noshow_notified"):
                    continue
                end = calc_booking_end(b)
                if not end:
                    continue
                if now < end + datetime.timedelta(minutes=NOSHOW_AFTER_MINUTES):
                    continue
                # Пинг админам
                try:
                    user_tag = f" (@{b.get('telegram_username')})" if b.get("telegram_username") else ""
                    minutes_late = int((now - end).total_seconds() // 60)
                    await broadcast_to_admins(
                        f"⏰ *Клиент не пришёл?*\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"🆔 `{b['booking_id']}`\n"
                        f"📦 Место №{b.get('place_num','—')} · {b.get('items',1)} шт\n"
                        f"📅 {fmt_date_ru(b.get('date','—'))} {b.get('time') or ''}\n"
                        f"👤 {b.get('name','—')}{user_tag}\n"
                        f"📞 {b.get('phone','—')}\n"
                        f"⌛ Окончание тарифа было *{minutes_late} мин назад*\n\n"
                        f"_Проверьте в /admin: бронь всё ещё активна._"
                    )
                    b["noshow_notified"] = True
                    b["noshow_notified_at"] = now.isoformat()
                    changed = True
                except Exception as e:
                    print(f"[noshow notify] {e}")

            # 2) Ежедневная сводка в 20:00
            meta = db.setdefault("meta", {})
            today_iso = now.date().isoformat()
            already_sent = meta.get("last_daily_summary_date") == today_iso
            in_window = (
                now.hour == DAILY_SUMMARY_HOUR
                and DAILY_SUMMARY_MINUTE <= now.minute <= DAILY_SUMMARY_MINUTE + 5
            )
            if in_window and not already_sent:
                try:
                    bookings = db.get("bookings", [])
                    todays = [b for b in bookings if b.get("date") == today_iso]
                    revenue = sum(
                        b.get("total", 0) for b in todays
                        if b.get("status") in ("active", "completed")
                    )
                    completed_n = sum(1 for b in todays if b.get("status") == "completed")
                    active_n = sum(1 for b in todays if b.get("status") == "active")
                    cancelled_n = sum(1 for b in todays if b.get("status") == "cancelled")
                    noshow_n = sum(
                        1 for b in todays
                        if b.get("status") == "active" and b.get("noshow_notified")
                    )
                    items_total = sum(
                        b.get("items", 0) for b in todays
                        if b.get("status") in ("active", "completed")
                    )
                    avg_check = (revenue // len(todays)) if todays else 0

                    summary = (
                        f"📊 *Сводка за {fmt_date_ru(today_iso)}*\n"
                        f"━━━━━━━━━━━━━━━━━━\n\n"
                        f"💵 *Выручка:* {revenue:,} ₽\n"
                        f"📋 *Броней всего:* {len(todays)}\n"
                        f"  ✅ Выполнено: {completed_n}\n"
                        f"  🟢 Ещё активны: {active_n}\n"
                        f"  ❌ Отменено: {cancelled_n}\n"
                        f"  ⏰ Не пришли: {noshow_n}\n\n"
                        f"🎒 *Мест занято:* {items_total}\n"
                        f"💰 *Средний чек:* {avg_check:,} ₽\n\n"
                        f"_Хорошего вечера! 🌙_"
                    )
                    await broadcast_to_admins(summary)
                    meta["last_daily_summary_date"] = today_iso
                    changed = True
                except Exception as e:
                    print(f"[daily summary] {e}")

            if changed:
                save_db(db)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[admin-notify-loop] {e}")


async def send_review_and_promo(booking: dict):
    """Шлёт юзеру: спасибо + просьба об отзыве + промокод."""
    if not bot:
        return
    user_id = booking.get("telegram_user_id")
    if not user_id:
        return

    # Создаём промокод (если ещё нет)
    promo = issue_promo_for_user(user_id, booking["booking_id"])
    try:
        expires = datetime.datetime.fromisoformat(promo["expires_at"])
        expires_str = expires.strftime("%d.%m.%Y")
    except Exception:
        expires_str = "—"

    ikb = InlineKeyboardBuilder()
    ikb.row(InlineKeyboardButton(text="⭐ Отзыв на Яндекс.Картах", url=REVIEW_URL_YANDEX))
    ikb.row(InlineKeyboardButton(text="⭐ Отзыв на 2ГИС", url=REVIEW_URL_2GIS))

    text = (
        f"🙏 *Спасибо, что были с нами!*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Надеемся, ваша прогулка по Зеленоградску прошла отлично, "
        f"а багаж ждал вас в полной сохранности.\n\n"
        f"⭐ *Ваш отзыв очень важен для нас.*\n"
        f"Поделитесь впечатлениями — это помогает нам становиться лучше "
        f"и помогает другим путешественникам найти нас.\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎁 *Подарок: ваш промокод*\n\n"
        f"`{promo['code']}`\n\n"
        f"💸 Скидка *10%* на следующий визит\n"
        f"📅 Действует до *{expires_str}*\n\n"
        f"_Скидка применяется ко всем тарифам, кроме вечернего. "
        f"Просто введите код при следующем бронировании._\n\n"
        f"До новых встреч! 🌊"
    )
    try:
        await bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=ikb.as_markup())
    except Exception as e:
        print(f"[review-msg] {e}")


# ─────────── API ───────────

# Максимум на сколько дней вперёд можно бронировать
BOOKING_HORIZON_DAYS = 30


class BookingRequest(BaseModel):
    booking_id: str
    place_num: int
    tariff: str
    date: str
    time: Optional[str] = None
    items: int
    name: str
    phone: str
    total: int
    telegram_user_id: Optional[int] = None
    telegram_username: Optional[str] = None

    @field_validator("date")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        # Формат YYYY-MM-DD
        try:
            d = datetime.date.fromisoformat(v)
        except (ValueError, TypeError):
            raise ValueError("Дата должна быть в формате YYYY-MM-DD")
        today = datetime.date.today()
        # Запрет броней задним числом
        if d < today:
            raise ValueError("Нельзя забронировать прошедшую дату")
        # Запрет броней «в далёкое будущее»
        if d > today + datetime.timedelta(days=BOOKING_HORIZON_DAYS):
            raise ValueError(
                f"Бронирование больше чем на {BOOKING_HORIZON_DAYS} дней вперёд недоступно"
            )
        return v

    @field_validator("items")
    @classmethod
    def _validate_items(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Минимум 1 место")
        if v > 10:
            raise ValueError("Максимум 10 мест в одной брони")
        return v


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task = None
    review_task = None
    admin_notify_task = None
    if bot and dp:
        bot_task = asyncio.create_task(start_bot())
        review_task = asyncio.create_task(auto_review_loop())
        admin_notify_task = asyncio.create_task(admin_notify_loop())
    yield
    for t in (bot_task, review_task, admin_notify_task):
        if t:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    if bot:
        await bot.session.close()


app = FastAPI(title="Камера хранения", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    """Главная страница — лендинг сайта."""
    if Path("landing.html").exists():
        return FileResponse("landing.html")
    if Path("index.html").exists():
        return FileResponse("index.html")
    return {"status": "ok"}


SITE_URL = "https://lock39.ru"


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    """robots.txt — разрешаем индексировать всё, кроме API и приватных страниц."""
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /app\n"
        "Disallow: /b/\n"
        "\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap_xml():
    """sitemap.xml — карта сайта для поисковиков."""
    today = datetime.date.today().isoformat()
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""
    return Response(content=xml, media_type="application/xml")


@app.get("/app")
def webapp():
    """Мини-приложение для Telegram-бота."""
    if Path("index.html").exists():
        return FileResponse("index.html")
    return {"status": "ok"}


@app.get("/api/availability")
def get_availability(user_id: Optional[int] = None):
    active = count_active_today()
    response = {"free": max(0, TOTAL_PLACES - active), "total": TOTAL_PLACES, "occupied": active}
    # Если запросили с user_id — добавим инфу про активную бронь юзера
    if user_id:
        existing = get_active_booking_for_user(user_id)
        if existing:
            response["has_active"] = True
            response["active_booking_id"] = existing["booking_id"]
            response["active_items"] = existing.get("items", 1)
        else:
            response["has_active"] = False
    return response


@app.post("/api/booking")
async def create_booking(req: BookingRequest):
    # Лимит мест в одной брони
    if req.items > 10:
        raise HTTPException(400, "Максимум 10 мест в одной брони")
    if req.items < 1:
        raise HTTPException(400, "Минимум 1 место")

    db = load_db()
    # Защита от дубля по booking_id
    for b in db.get("bookings", []):
        if b["booking_id"] == req.booking_id:
            raise HTTPException(409, "Это бронирование уже создано")

    # Проверка: у юзера уже есть активная бронь?
    if req.telegram_user_id:
        existing = get_active_booking_for_user(req.telegram_user_id)
        if existing:
            raise HTTPException(
                409,
                f"У вас уже есть активная бронь {existing['booking_id']}. "
                f"Дождитесь её окончания или отмените, чтобы создать новую."
            )

    # Проверка свободных мест
    if count_active_today() + req.items > TOTAL_PLACES:
        free_now = max(0, TOTAL_PLACES - count_active_today())
        raise HTTPException(409, f"Недостаточно мест. Свободно: {free_now}")

    booking = req.model_dump()
    booking["created_at"] = datetime.datetime.now().isoformat()
    booking["status"] = "active"
    db.setdefault("bookings", []).append(booking)
    save_db(db)
    asyncio.create_task(send_user_confirmation(booking))
    asyncio.create_task(notify_admin(booking))
    free = max(0, TOTAL_PLACES - count_active_today())
    return {"success": True, "booking_id": req.booking_id, "free": free}


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
    found = False
    for b in bookings:
        if b["booking_id"] == booking_id:
            b["status"] = "cancelled"
            found = True
            break
    if not found:
        raise HTTPException(404, "Бронирование не найдено")
    save_db(db)
    return {"success": True}


# ─────────── СТРАНИЦА БРОНИ (по QR-коду) ───────────

@app.get("/b/{booking_id}", response_class=HTMLResponse)
def view_booking(booking_id: str):
    """Открывается при сканировании QR-кода смартфоном."""
    db = load_db()
    booking = next((b for b in db.get("bookings", []) if b["booking_id"] == booking_id), None)
    if not booking:
        return HTMLResponse(_not_found_html(booking_id), status_code=404)
    return HTMLResponse(_booking_html(booking))


def _booking_html(b: dict) -> str:
    status = b.get("status", "active")
    status_label = {"active": "Активна", "cancelled": "Отменена", "completed": "Завершена"}.get(status, status)
    status_color = {"active": "#16a34a", "cancelled": "#dc2626", "completed": "#6b7280"}.get(status, "#16a34a")
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Бронирование {b['booking_id']}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #f4f4f5; color: #111; min-height: 100vh;
  padding: 16px; max-width: 480px; margin: 0 auto;
}}
.card {{
  background: white; border-radius: 16px; padding: 22px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.06); margin-bottom: 14px;
}}
.head {{ text-align: center; margin-bottom: 20px; }}
.head .icon {{
  width: 64px; height: 64px; border-radius: 50%;
  background: #dcfce7; display: flex; align-items: center;
  justify-content: center; font-size: 32px; margin: 0 auto 10px;
}}
.head .title {{ font-size: 18px; font-weight: 700; }}
.head .sub {{ font-size: 13px; color: #888; margin-top: 4px; }}
.bid {{
  text-align: center; font-family: monospace; font-size: 16px;
  font-weight: 700; color: #2678b6; letter-spacing: 1px;
  background: rgba(38,120,182,0.08); padding: 10px;
  border-radius: 10px; margin-bottom: 14px;
}}
.status {{
  display: inline-block; padding: 4px 12px; border-radius: 20px;
  font-size: 12px; font-weight: 600; color: white;
  background: {status_color}; margin-bottom: 16px;
}}
.row {{
  display: flex; justify-content: space-between; padding: 10px 0;
  border-bottom: 1px solid #f0f0f0; font-size: 14px;
}}
.row:last-child {{ border-bottom: none; }}
.row .lbl {{ color: #888; }}
.row .val {{ font-weight: 500; text-align: right; max-width: 65%; }}
.row.total .val {{ color: #2678b6; font-weight: 700; font-size: 16px; }}
.note {{
  font-size: 12px; color: #888; text-align: center;
  margin-top: 16px; padding: 10px;
}}
</style>
</head>
<body>
  <div class="card">
    <div class="head">
      <div class="icon">🔐</div>
      <div class="title">Бронирование</div>
      <div class="sub">Камера хранения</div>
    </div>
    <div style="text-align:center;">
      <div class="status">{status_label}</div>
    </div>
    <div class="bid">{b['booking_id']}</div>
    <div class="row"><span class="lbl">Место</span><span class="val">№{b.get('place_num','—')}</span></div>
    <div class="row"><span class="lbl">Тариф</span><span class="val">{b['tariff']}</span></div>
    <div class="row"><span class="lbl">Дата</span><span class="val">{fmt_date_ru(b['date'])}</span></div>
    <div class="row"><span class="lbl">Время</span><span class="val">{b.get('time') or '—'}</span></div>
    <div class="row"><span class="lbl">Количество вещей</span><span class="val">{b['items']} шт</span></div>
    <div class="row"><span class="lbl">Имя клиента</span><span class="val">{b['name']}</span></div>
    <div class="row"><span class="lbl">Телефон</span><span class="val">{b['phone']}</span></div>
    <div class="row total"><span class="lbl">К оплате</span><span class="val">{b['total']:,} ₽</span></div>
  </div>
  <div class="note">Эта страница доступна только при сканировании QR-кода клиента</div>
</body>
</html>"""


def _not_found_html(bid: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Не найдено</title>
<style>body{{font-family:sans-serif;text-align:center;padding:40px 20px;background:#f4f4f5;}}
.box{{background:white;padding:32px;border-radius:16px;max-width:400px;margin:0 auto;}}
.icon{{font-size:48px;margin-bottom:12px;}}h1{{font-size:18px;margin-bottom:8px;}}
p{{color:#888;font-size:14px;}}code{{background:#f0f0f0;padding:2px 8px;border-radius:4px;}}</style>
</head><body><div class="box"><div class="icon">❌</div><h1>Бронирование не найдено</h1>
<p>Бронирование <code>{bid}</code> не существует или было удалено.</p></div></body></html>"""


# ─────────── ЗАПУСК ───────────

if __name__ == "__main__":
    print(f"🚀 Старт на порту {PORT}")
    print(f"   BOT_TOKEN: {'✅ задан' if BOT_TOKEN else '❌ НЕ задан'}")
    print(f"   WEBAPP_URL: {WEBAPP_URL or '❌ НЕ задан'}")
    print(f"   MAIN_ADMIN_ID: {MAIN_ADMIN_ID or '❌ НЕ задан'}")
    print(f"   ADMIN_IDS: {sorted(ADMIN_IDS) if ADMIN_IDS else '❌ пуст'}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
