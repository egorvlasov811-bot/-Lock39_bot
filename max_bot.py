"""
MAX-бот для камеры хранения. Работает с той же базой bookings.json, что и Telegram-бот.
Запуск: python max_bot.py
"""

import os
import json
import asyncio
import datetime
from pathlib import Path
from typing import Optional

from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, Command, MessageCreated, MessageCallback
from maxapi.types.input_media import InlineKeyboardBuilder
from maxapi.filters import F

# ───── ОБЩИЕ КОНСТАНТЫ (синхронизировать с app.py) ─────
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
MAX_ADMIN_CHAT_ID = os.getenv("MAX_ADMIN_CHAT_ID", "")  # ID админа в MAX
DB_FILE = "bookings.json"
TOTAL_PLACES = 500

ADDRESS = "г. Зеленоградск, ул. Железнодорожная, 2Б корп. 1"
ADDRESS_HINT = "Ориентир: железнодорожный вокзал Зеленоградска"
MAPS_URL_YANDEX = "https://yandex.ru/maps/org/kamera_khraneniya_bagazha/245433262999"
MAPS_URL_2GIS = "https://2gis.ru/kaliningrad/geo/70000001101819705"
PLACE_EXAMPLES = "чемодан, рюкзак, пакет, велосипед, самокат, коробка, сумка"

# ───── ДОСТУП К БД ─────

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
    db = load_db()
    today = datetime.date.today().isoformat()
    count = 0
    for b in db.get("bookings", []):
        if b.get("status") == "cancelled":
            continue
        if b.get("date") == today or "Сутки" in b.get("tariff", "") or "Суточное" in b.get("tariff", ""):
            count += b.get("items", 1)
    return count

def get_active_booking_for_max_user(user_id) -> Optional[dict]:
    """Активная бронь юзера MAX (отдельное поле max_user_id)."""
    if not user_id:
        return None
    db = load_db()
    today = datetime.date.today().isoformat()
    for b in db.get("bookings", []):
        if b.get("max_user_id") != user_id:
            continue
        if b.get("status") != "active":
            continue
        if b.get("date") >= today or "Сутки" in b.get("tariff", "") or "Суточное" in b.get("tariff", ""):
            return b
    return None

def fmt_date_ru(s: str) -> str:
    if not s:
        return "—"
    try:
        y, m, d = s.split("-")
        return f"{d}.{m}.{y}"
    except Exception:
        return s

# ───── ТАРИФЫ ─────

TARIFFS = {
    "1": {"name": "1 час — 100 ₽/место", "price": 100},
    "2": {"name": "3 часа — 200 ₽/место", "price": 200},
    "3": {"name": "Весь день (09:00–19:00) — 300 ₽/место", "price": 300},
    "4": {"name": "Вечерний — 100 ₽/час", "price": 100},
    "5": {"name": "Сутки — 600 ₽/место", "price": 600},
}

def get_available_tariffs() -> list:
    """Скрываем 'Весь день' после 19:00."""
    avail = ["1", "2"]
    if datetime.datetime.now().hour < 19:
        avail.append("3")
    avail.extend(["4", "5"])
    return avail

def calc_total(state: dict) -> tuple[int, int, int]:
    """Возвращает (base, evening_extra, total)."""
    t = state["tariff"]
    items = state["items"]
    base_price = TARIFFS[t]["price"]
    if t == "4":
        base = base_price * state.get("hours", 1) * items
        extra = 0
    elif t == "5":
        base = base_price * state.get("days", 1) * items
        extra = 0
    else:
        base = base_price * items
        extra_hours = state.get("evening_extra_hours", 0)
        extra = 100 * extra_hours * items
    return base, extra, base + extra

def tariff_label(state: dict) -> str:
    t = state["tariff"]
    if t == "4":
        return f"Вечерний, {state.get('hours',1)} ч × 100 ₽"
    if t == "5":
        return f"Сутки, {state.get('days',1)} сут × 600 ₽"
    if t == "3":
        extra = state.get("evening_extra_hours", 0)
        if extra > 0:
            return f"Весь день до {19 + extra}:00 (с вечерней доплатой)"
    return TARIFFS[t]["name"]

# ───── BOT ─────

bot = Bot(MAX_BOT_TOKEN)
dp = Dispatcher()

# Простое FSM на словарях (maxapi свой FSM имеет, но для простоты — in-memory)
USER_STATES = {}  # {user_id: {"step": ..., "tariff": ..., ...}}

def set_state(user_id, **kwargs):
    USER_STATES.setdefault(user_id, {}).update(kwargs)

def get_state(user_id) -> dict:
    return USER_STATES.get(user_id, {})

def clear_state(user_id):
    USER_STATES.pop(user_id, None)

# ───── ВСПОМОГАТЕЛЬНЫЕ КНОПКИ ─────

def main_menu_kb():
    """Постоянная клавиатура снизу (если поддерживается). Иначе — inline."""
    kb = InlineKeyboardBuilder()
    kb.row("📅 Забронировать", payload="menu:book")
    kb.row("📋 Мои брони", payload="menu:my")
    kb.row("❌ Отменить бронь", payload="menu:cancel")
    kb.row("💰 Тарифы", payload="menu:tariffs")
    kb.row("📍 Адрес", payload="menu:address")
    kb.row("📞 Связаться", payload="menu:contact")
    kb.row("ℹ️ Помощь", payload="menu:help")
    return kb.build()

# ───── WELCOME ─────

WELCOME = (
    "👋 *Добро пожаловать в камеру хранения Зеленоградска!*\n\n"
    "Оставьте багаж под надёжным присмотром, пока гуляете по городу или ждёте поезд.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    f"📍 *Адрес:* {ADDRESS}\n"
    f"_{ADDRESS_HINT}_\n\n"
    f"🕐 *Режим:* круглосуточно\n"
    f"📦 *Вместимость:* 500 мест\n"
    f"🔒 *Охрана:* видеонаблюдение 24/7\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "💰 *Тарифы:*\n"
    "⏱ 1 час — *100 ₽* за место\n"
    "🕒 3 часа — *200 ₽* за место\n"
    "☀️ Весь день (09–19) — *300 ₽* за место\n"
    "🌙 После 19:00 — *100 ₽/час* за место\n"
    "📦 Сутки — *600 ₽* за место\n\n"
    f"💡 *1 место* = {PLACE_EXAMPLES}\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🌙 *Дневной + вечерний — в одной брони!*\n"
    "Выберите «Весь день» и укажите время окончания — бот сам добавит вечернюю доплату.\n\n"
    "Используйте кнопки ниже 👇"
)

@dp.bot_started()
async def on_started(event: BotStarted):
    await event.bot.send_message(
        chat_id=event.chat_id,
        text=WELCOME,
        attachments=[main_menu_kb()]
    )

@dp.message_created(Command("start"))
async def cmd_start(event: MessageCreated):
    clear_state(event.message.sender.user_id)
    await event.message.answer(WELCOME, attachments=[main_menu_kb()])

# ───── РОУТИНГ КНОПОК ГЛАВНОГО МЕНЮ ─────

@dp.message_callback(F.payload.startswith("menu:"))
async def menu_router(event: MessageCallback):
    action = event.callback.payload.split(":")[1]
    uid = event.callback.user.user_id
    if action == "book":
        await start_booking(event, uid)
    elif action == "my":
        await show_mybookings(event, uid)
    elif action == "cancel":
        await ask_cancel(event, uid)
    elif action == "tariffs":
        await event.answer(make_tariffs_text(), attachments=[main_menu_kb()])
    elif action == "address":
        await event.answer(make_address_text(), attachments=[address_kb()])
    elif action == "contact":
        set_state(uid, step="contact")
        await event.answer(
            "📞 Напишите одним сообщением — мы получим ваш вопрос и ответим.",
            attachments=[main_menu_kb()]
        )
    elif action == "help":
        await event.answer(make_help_text(), attachments=[main_menu_kb()])

def make_tariffs_text() -> str:
    return (
        "💰 *Тарифы камеры хранения*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⏱ *1 час* — 100 ₽/место\n"
        "🕒 *3 часа* — 200 ₽/место\n"
        "☀️ *Весь день* (09–19) — 300 ₽/место\n"
        "🌙 *Вечерний* — 100 ₽/час/место\n"
        "📦 *Сутки* — 600 ₽/место\n\n"
        f"💡 *1 место* = {PLACE_EXAMPLES}\n\n"
        "📌 До 10 мест в одной брони\n"
        "📌 Одна активная бронь на аккаунт"
    )

def address_kb():
    kb = InlineKeyboardBuilder()
    kb.row_link("🗺 Яндекс.Карты", url=MAPS_URL_YANDEX)
    kb.row_link("🗺 2ГИС", url=MAPS_URL_2GIS)
    return kb.build()

def make_address_text() -> str:
    return (
        f"📍 *Адрес*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"*{ADDRESS}*\n\n"
        f"📌 {ADDRESS_HINT}\n\n"
        f"🕐 Режим: круглосуточно"
    )

def make_help_text() -> str:
    return (
        "ℹ️ *Команды:*\n\n"
        "/start — главное меню\n"
        "/book — забронировать\n"
        "/cancel — отменить активную бронь\n"
        "/info — моя активная бронь\n"
        "/mybookings — история\n"
        "/help — справка\n\n"
        "Или используйте кнопки 👇"
    )

# ───── ПРОЦЕСС БРОНИРОВАНИЯ ─────

async def start_booking(event, uid):
    existing = get_active_booking_for_max_user(uid)
    if existing:
        await event.answer(
            f"⚠️ У вас уже есть бронь `{existing['booking_id']}`.\n"
            f"Отмените её, чтобы создать новую.",
            attachments=[main_menu_kb()]
        )
        return
    free = TOTAL_PLACES - count_active_today()
    if free <= 0:
        await event.answer("😔 Все места заняты.", attachments=[main_menu_kb()])
        return

    now = datetime.datetime.now()
    available = get_available_tariffs()
    kb = InlineKeyboardBuilder()
    labels = {
        "1": "⏱ 1 час — 100 ₽",
        "2": "🕒 3 часа — 200 ₽",
        "3": "☀️ Весь день — 300 ₽",
        "4": "🌙 Вечерний — 100 ₽/ч",
        "5": "📦 Сутки — 600 ₽",
    }
    for t in available:
        kb.row(labels[t], payload=f"bk:t:{t}")
    kb.row("❌ Отмена", payload="bk:cancel")

    set_state(uid, step="tariff")
    hint = ""
    if now.hour >= 16 and now.hour < 19:
        hint = f"\n⏰ До 19:00 ~{19 - now.hour} ч. Для долгого хранения берите «Весь день» или «Сутки».\n"

    await event.answer(
        f"🔐 *Новая бронь*\n📦 Свободно: *{free}* из {TOTAL_PLACES}\n🕐 Сейчас: *{now.strftime('%H:%M')}*\n{hint}\n"
        f"🌙 *Нужно днём + вечером?* Выберите «Весь день» — на следующем шаге добавите доплату до 23:00.\n\n"
        "*Шаг 1:* Тариф 👇",
        attachments=[kb.build()]
    )

@dp.message_callback(F.payload == "bk:cancel")
async def bk_cancel(event: MessageCallback):
    clear_state(event.callback.user.user_id)
    await event.answer("❌ Отменено.", attachments=[main_menu_kb()])

@dp.message_callback(F.payload.startswith("bk:t:"))
async def bk_tariff(event: MessageCallback):
    uid = event.callback.user.user_id
    t = event.callback.payload.split(":")[2]
    set_state(uid, tariff=t)

    if t == "4":
        kb = InlineKeyboardBuilder()
        for h in [1, 2, 3, 4, 5, 6]:
            kb.row(f"{h} ч", payload=f"bk:h:{h}")
        await event.answer(
            f"✓ Тариф: *{TARIFFS[t]['name']}*\n\n🌙 Вечерний действует с 19:00.\nСколько часов?",
            attachments=[kb.build()]
        )
        return
    if t == "5":
        kb = InlineKeyboardBuilder()
        for d in [1, 2, 3, 5, 7, 14]:
            kb.row(f"{d} сут", payload=f"bk:d:{d}")
        await event.answer(
            f"✓ Тариф: *{TARIFFS[t]['name']}*\n\nСколько суток?",
            attachments=[kb.build()]
        )
        return
    if t == "3":
        kb = InlineKeyboardBuilder()
        kb.row("✓ Только день — до 19:00", payload="bk:e:0")
        kb.row("🌙 День + до 20:00 (+100 ₽/место)", payload="bk:e:1")
        kb.row("🌙 День + до 21:00 (+200 ₽/место)", payload="bk:e:2")
        kb.row("🌙 День + до 22:00 (+300 ₽/место)", payload="bk:e:3")
        kb.row("🌙 День + до 23:00 (+400 ₽/место)", payload="bk:e:4")
        await event.answer(
            "☀️ Базовый: 09:00–19:00 за 300 ₽/место.\n\n"
            "🌙 Можно продлить до позднего вечера: +100 ₽/час за каждое место.\n\n"
            "💡 *Пример:* 2 чемодана 12:00–22:00 = 600 ₽ (день) + 600 ₽ (3 ч × 2 × 100) = *1 200 ₽*\n\n"
            "До какого времени?",
            attachments=[kb.build()]
        )
        return
    # Остальные — сразу к дате
    await ask_date(event, uid)

@dp.message_callback(F.payload.startswith("bk:h:"))
async def bk_hours(event: MessageCallback):
    uid = event.callback.user.user_id
    set_state(uid, hours=int(event.callback.payload.split(":")[2]))
    await ask_date(event, uid)

@dp.message_callback(F.payload.startswith("bk:d:"))
async def bk_days(event: MessageCallback):
    uid = event.callback.user.user_id
    set_state(uid, days=int(event.callback.payload.split(":")[2]))
    await ask_date(event, uid)

@dp.message_callback(F.payload.startswith("bk:e:"))
async def bk_evening(event: MessageCallback):
    uid = event.callback.user.user_id
    set_state(uid, evening_extra_hours=int(event.callback.payload.split(":")[2]))
    await ask_date(event, uid)

async def ask_date(event, uid):
    kb = InlineKeyboardBuilder()
    today = datetime.date.today()
    for i in range(7):
        d = today + datetime.timedelta(days=i)
        label = "Сегодня" if i == 0 else ("Завтра" if i == 1 else d.strftime("%d.%m"))
        kb.row(label, payload=f"bk:date:{d.isoformat()}")
    state = get_state(uid)
    await event.answer(
        f"✓ Тариф: *{tariff_label(state)}*\n\nВыберите дату:",
        attachments=[kb.build()]
    )

@dp.message_callback(F.payload.startswith("bk:date:"))
async def bk_date(event: MessageCallback):
    uid = event.callback.user.user_id
    date_iso = event.callback.payload.split(":", 2)[2]
    set_state(uid, date=date_iso)
    state = get_state(uid)
    t = state["tariff"]
    if t == "3":
        set_state(uid, time="09:00")
        await ask_items(event, uid)
        return
    if t == "5":
        set_state(uid, time=None)
        await ask_items(event, uid)
        return

    kb = InlineKeyboardBuilder()
    if t == "4":
        slots = ["19:00", "19:30", "20:00", "20:30", "21:00", "22:00"]
    else:
        slots = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]
    for s in slots:
        kb.row(s, payload=f"bk:time:{s}")
    await event.answer(
        f"✓ Дата: *{fmt_date_ru(date_iso)}*\n\nВремя начала:",
        attachments=[kb.build()]
    )

@dp.message_callback(F.payload.startswith("bk:time:"))
async def bk_time(event: MessageCallback):
    uid = event.callback.user.user_id
    set_state(uid, time=event.callback.payload.split(":", 2)[2])
    await ask_items(event, uid)

async def ask_items(event, uid):
    kb = InlineKeyboardBuilder()
    for n in range(1, 11):
        kb.row(str(n), payload=f"bk:i:{n}")
    state = get_state(uid)
    text = (
        f"✓ Тариф: *{tariff_label(state)}*\n"
        f"✓ Дата: *{fmt_date_ru(state['date'])}*\n"
    )
    if state.get("time"):
        text += f"✓ Время: *{state['time']}*\n"
    text += f"\n*Сколько мест?* (до 10)\n💡 1 место = {PLACE_EXAMPLES}"
    await event.answer(text, attachments=[kb.build()])

@dp.message_callback(F.payload.startswith("bk:i:"))
async def bk_items(event: MessageCallback):
    uid = event.callback.user.user_id
    items = int(event.callback.payload.split(":")[2])
    free = TOTAL_PLACES - count_active_today()
    if items > free:
        await event.answer(f"Свободно только {free}.")
        return
    set_state(uid, items=items, step="name")
    await event.answer("👤 Введите ваше имя:")

@dp.message_created(F.text)
async def handle_text(event: MessageCreated):
    uid = event.message.sender.user_id
    state = get_state(uid)
    step = state.get("step")
    text = (event.message.body.text or "").strip()

    # Игнор системного кликера кнопок (если приходит как текст)
    if text.startswith("/"):
        return

    # СВЯЗЬ С АДМИНОМ
    if step == "contact":
        if MAX_ADMIN_CHAT_ID:
            try:
                user = event.message.sender
                await bot.send_message(
                    chat_id=int(MAX_ADMIN_CHAT_ID),
                    text=(
                        f"📩 *Сообщение от клиента MAX*\n"
                        f"👤 {user.first_name or ''} {user.last_name or ''} (id {user.user_id})\n\n"
                        f"💬 {text}"
                    )
                )
                await event.message.answer("✅ Спасибо! Ваше сообщение отправлено.", attachments=[main_menu_kb()])
            except Exception as e:
                print(f"[contact] {e}")
                await event.message.answer("❌ Не удалось отправить, попробуйте позже.")
        else:
            await event.message.answer("⚠️ Связь временно недоступна.")
        clear_state(uid)
        return

    # ВВОД ИМЕНИ
    if step == "name":
        if len(text) < 2:
            await event.message.answer("Имя слишком короткое:")
            return
        set_state(uid, name=text, step="phone")
        await event.message.answer(f"✓ Имя: *{text}*\n\n📞 Введите телефон (+7...):")
        return

    # ВВОД ТЕЛЕФОНА
    if step == "phone":
        digits = "".join(c for c in text if c.isdigit())
        if len(digits) < 10:
            await event.message.answer("❌ Минимум 10 цифр. Попробуйте ещё:")
            return
        set_state(uid, phone=text, step="confirm")
        s = get_state(uid)
        base, extra, total = calc_total(s)

        breakdown = ""
        if extra > 0:
            end_h = 19 + s.get("evening_extra_hours", 0)
            breakdown = (
                f"\n💵 *Расчёт:*\n"
                f"  • День (09–19): *{base:,} ₽*\n"
                f"  • Вечер (19–{end_h}:00): *{extra:,} ₽*\n"
            )

        summary = (
            f"📋 *Проверьте бронь*\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 {tariff_label(s)}\n"
            f"📅 {fmt_date_ru(s['date'])}\n"
            f"⏰ {s.get('time') or '—'}\n"
            f"🎒 {s['items']} мест\n"
            f"👤 {s['name']}\n"
            f"📞 {text}\n"
            f"{breakdown}\n"
            f"💳 *Итого: {total:,} ₽*"
        )
        kb = InlineKeyboardBuilder()
        kb.row("✅ Подтвердить", payload="bk:confirm")
        kb.row("❌ Отменить", payload="bk:reject")
        await event.message.answer(summary, attachments=[kb.build()])
        return

@dp.message_callback(F.payload == "bk:reject")
async def bk_reject(event: MessageCallback):
    clear_state(event.callback.user.user_id)
    await event.answer("❌ Бронь отменена. Начать заново — /book", attachments=[main_menu_kb()])

@dp.message_callback(F.payload == "bk:confirm")
async def bk_confirm(event: MessageCallback):
    uid = event.callback.user.user_id
    s = get_state(uid)

    # Перепроверка
    if get_active_booking_for_max_user(uid):
        clear_state(uid)
        await event.answer("⚠️ У вас уже появилась активная бронь.", attachments=[main_menu_kb()])
        return
    free = TOTAL_PLACES - count_active_today()
    if s["items"] > free:
        clear_state(uid)
        await event.answer(f"😔 Свободно только {free} мест.", attachments=[main_menu_kb()])
        return

    base, extra, total = calc_total(s)
    now = datetime.datetime.now()
    bid = f"LS-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')[-4:]}"
    place_num = (TOTAL_PLACES - free) + 1

    user = event.callback.user
    booking = {
        "booking_id": bid,
        "place_num": place_num,
        "tariff": tariff_label(s),
        "date": s["date"],
        "time": s.get("time"),
        "items": s["items"],
        "name": s["name"],
        "phone": s["phone"],
        "total": total,
        "base_price": base,
        "evening_extra": extra,
        "evening_extra_hours": s.get("evening_extra_hours", 0),
        "max_user_id": uid,
        "max_user_name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "created_at": now.isoformat(),
        "status": "active",
        "source": "max_bot",
    }
    db = load_db()
    db.setdefault("bookings", []).append(booking)
    save_db(db)
    clear_state(uid)

    # Подтверждение пользователю
    confirm_text = (
        f"✅ *Бронь подтверждена!*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{bid}`\n"
        f"📦 Место №{place_num}\n"
        f"💰 {booking['tariff']}\n"
        f"📅 {fmt_date_ru(booking['date'])} {booking.get('time') or ''}\n"
        f"🎒 {booking['items']} мест\n"
        f"👤 {booking['name']} • 📞 {booking['phone']}\n"
        f"💵 *{total:,} ₽*\n\n"
        f"📍 {ADDRESS}\n\n"
        f"Покажите этот номер брони на стойке."
    )
    kb = InlineKeyboardBuilder()
    kb.row("❌ Отменить бронь", payload=f"cancel:{bid}")
    await event.answer(confirm_text, attachments=[kb.build()])

    # Уведомление админу
    if MAX_ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                chat_id=int(MAX_ADMIN_CHAT_ID),
                text=(
                    f"🔔 *Новая бронь (MAX)*\n"
                    f"🆔 `{bid}`\n"
                    f"📦 №{place_num} • {booking['items']} мест\n"
                    f"💰 {booking['tariff']}\n"
                    f"📅 {fmt_date_ru(booking['date'])} {booking.get('time') or ''}\n"
                    f"👤 {booking['name']} • 📞 {booking['phone']}\n"
                    f"💵 *{total:,} ₽*"
                )
            )
        except Exception as e:
            print(f"[admin notify] {e}")

    # Уведомление в Telegram-админа
    await notify_telegram_admin(booking)

async def notify_telegram_admin(booking: dict):
    """Дублируем уведомление в Telegram, чтобы админ видел брони из обоих ботов."""
    tg_token = os.getenv("BOT_TOKEN")
    tg_admin = os.getenv("ADMIN_CHAT_ID")
    if not tg_token or not tg_admin:
        return
    try:
        import httpx
        text = (
            f"🔔 *Новая бронь (из MAX)*\n"
            f"🆔 `{booking['booking_id']}`\n"
            f"📦 №{booking['place_num']} • {booking['items']} мест\n"
            f"💰 {booking['tariff']}\n"
            f"📅 {fmt_date_ru(booking['date'])} {booking.get('time') or ''}\n"
            f"👤 {booking['name']} • 📞 {booking['phone']}\n"
            f"💵 *{booking['total']:,} ₽*"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": int(tg_admin), "text": text, "parse_mode": "Markdown"}
            )
    except Exception as e:
        print(f"[tg notify] {e}")

# ───── ОТМЕНА БРОНИ ─────

@dp.message_created(Command("cancel"))
async def cmd_cancel(event: MessageCreated):
    uid = event.message.sender.user_id
    await ask_cancel(event.message, uid)

async def ask_cancel(event_or_msg, uid):
    booking = get_active_booking_for_max_user(uid)
    if not booking:
        if hasattr(event_or_msg, "answer"):
            await event_or_msg.answer("У вас нет активной брони.", attachments=[main_menu_kb()])
        return
    kb = InlineKeyboardBuilder()
    kb.row("✅ Да, отменить", payload=f"cancel_yes:{booking['booking_id']}")
    kb.row("↩️ Нет", payload="cancel_no")
    text = (
        f"⚠️ *Отменить бронь?*\n"
        f"🆔 `{booking['booking_id']}`\n"
        f"📦 №{booking.get('place_num','—')}\n"
        f"💵 {booking['total']:,} ₽\n\n"
        f"⚠️ Действие необратимо."
    )
    await event_or_msg.answer(text, attachments=[kb.build()])

@dp.message_callback(F.payload.startswith("cancel:"))
async def cb_ask_cancel(event: MessageCallback):
    bid = event.callback.payload.split(":")[1]
    db = load_db()
    target = next((b for b in db.get("bookings", []) if b["booking_id"] == bid and b.get("max_user_id") == event.callback.user.user_id), None)
    if not target:
        await event.answer("Бронь не найдена.")
        return
    kb = InlineKeyboardBuilder()
    kb.row("✅ Да, отменить", payload=f"cancel_yes:{bid}")
    kb.row("↩️ Нет", payload="cancel_no")
    await event.answer(
        f"⚠️ Точно отменить `{bid}`?\n💵 {target['total']:,} ₽",
        attachments=[kb.build()]
    )

@dp.message_callback(F.payload.startswith("cancel_yes:"))
async def cb_cancel_yes(event: MessageCallback):
    bid = event.callback.payload.split(":")[1]
    uid = event.callback.user.user_id
    db = load_db()
    target = next((b for b in db.get("bookings", []) if b["booking_id"] == bid and b.get("max_user_id") == uid), None)
    if not target or target.get("status") == "cancelled":
        await event.answer("Не найдено или уже отменено.")
        return
    target["status"] = "cancelled"
    target["cancelled_at"] = datetime.datetime.now().isoformat()
    save_db(db)
    await event.answer(f"❌ Бронь `{bid}` отменена. Места освобождены.\n\nСоздать новую — /book", attachments=[main_menu_kb()])

    # Уведомления админу в MAX и Telegram
    if MAX_ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                chat_id=int(MAX_ADMIN_CHAT_ID),
                text=f"❌ *Бронь отменена клиентом (MAX)*\n🆔 `{bid}`\n👤 {target['name']} • 📞 {target['phone']}"
            )
        except Exception:
            pass
    # И в Telegram
    tg_token = os.getenv("BOT_TOKEN")
    tg_admin = os.getenv("ADMIN_CHAT_ID")
    if tg_token and tg_admin:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={
                        "chat_id": int(tg_admin),
                        "text": f"❌ *Отмена брони (MAX)*\n🆔 `{bid}`\n👤 {target['name']} • 📞 {target['phone']}",
                        "parse_mode": "Markdown"
                    }
                )
        except Exception:
            pass

@dp.message_callback(F.payload == "cancel_no")
async def cb_cancel_no(event: MessageCallback):
    await event.answer("✅ Бронь сохранена.", attachments=[main_menu_kb()])

# ───── ИНФО / МОИ БРОНИ ─────

@dp.message_created(Command("info"))
async def cmd_info(event: MessageCreated):
    uid = event.message.sender.user_id
    booking = get_active_booking_for_max_user(uid)
    if not booking:
        await event.message.answer("У вас нет активной брони. Создать — /book")
        return
    kb = InlineKeyboardBuilder()
    kb.row("❌ Отменить бронь", payload=f"cancel:{booking['booking_id']}")
    await event.message.answer(
        f"🎫 *Ваша активная бронь*\n━━━━━━━━━━━━━━\n"
        f"🆔 `{booking['booking_id']}`\n"
        f"📦 Место №{booking.get('place_num','—')} • {booking['items']} мест\n"
        f"💰 {booking['tariff']}\n"
        f"📅 {fmt_date_ru(booking['date'])} {booking.get('time') or ''}\n"
        f"💵 {booking['total']:,} ₽\n\n"
        f"📍 {ADDRESS}",
        attachments=[kb.build()]
    )

@dp.message_created(Command("mybookings"))
async def cmd_mybookings(event: MessageCreated):
    await show_mybookings(event.message, event.message.sender.user_id)

async def show_mybookings(event_or_msg, uid):
    db = load_db()
    my = [b for b in db.get("bookings", []) if b.get("max_user_id") == uid][-5:]
    if not my:
        await event_or_msg.answer("Бронирований пока нет.\nСоздать — /book", attachments=[main_menu_kb()])
        return
    for b in my:
        st = {"active": "✅", "cancelled": "❌", "completed": "☑️"}.get(b.get("status", "active"), "")
        await event_or_msg.answer(
            f"{st} `{b['booking_id']}`\n"
            f"📦 №{b.get('place_num','—')} • {b['items']} мест\n"
            f"💰 {b['tariff']}\n"
            f"📅 {fmt_date_ru(b['date'])} {b.get('time') or ''}\n"
            f"💵 {b['total']:,} ₽"
        )

# ───── /help ─────

@dp.message_created(Command("help"))
async def cmd_help(event: MessageCreated):
    await event.message.answer(make_help_text(), attachments=[main_menu_kb()])

# ───── /book ─────

@dp.message_created(Command("book"))
async def cmd_book(event: MessageCreated):
    await start_booking(event.message, event.message.sender.user_id)

# ───── ЗАПУСК ─────

async def main():
    print(f"🚀 MAX bot старт")
    print(f"   TOKEN: {'✅' if MAX_BOT_TOKEN else '❌'}")
    print(f"   ADMIN: {MAX_ADMIN_CHAT_ID or '❌'}")
    commands = [
        {"name": "start", "description": "🏠 Главное меню"},
        {"name": "book", "description": "📅 Забронировать"},
        {"name": "info", "description": "🎫 Моя бронь"},
        {"name": "mybookings", "description": "📋 История"},
        {"name": "cancel", "description": "❌ Отменить"},
        {"name": "help", "description": "ℹ️ Помощь"},
    ]
    try:
        await bot.api.set_my_commands(commands)
    except Exception as e:
        print(f"[commands] {e}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
