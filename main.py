import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "bot.db"

pending_choices: dict[int, list] = {}          # user_id -> [Location...]
awaiting_input: dict[int, str] = {}            # user_id -> "city"|"time"|"tz"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🌤 Погода сейчас")],
        [KeyboardButton(text="🏙 Город"), KeyboardButton(text="⏰ Время")],
        [KeyboardButton(text="🕒 Таймзона"), KeyboardButton(text="⚙️ Статус")],
        [KeyboardButton(text="✅ Вкл"), KeyboardButton(text="❌ Выкл")],
    ],
    resize_keyboard=True
)

# ---------- Weather ----------

@dataclass
class Location:
    name: str
    lat: float
    lon: float


async def geocode_city_candidates(session: aiohttp.ClientSession, query: str) -> list[Location]:
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": query, "count": 15, "language": "en", "format": "json"}

    async with session.get(url, params=params, timeout=15) as r:
        data = await r.json()

    results = data.get("results") or []
    out: list[Location] = []

    for x in results:
        name_parts = [x.get("name"), x.get("admin1"), x.get("country")]
        name = ", ".join([p for p in name_parts if p])
        out.append(Location(name=name, lat=float(x["latitude"]), lon=float(x["longitude"])))

    return out


def wmo_to_text(code: int) -> str:
    mapping = {
        0: "Ясно",
        1: "В основном ясно",
        2: "Переменная облачность",
        3: "Пасмурно",
        45: "Туман",
        48: "Туман (изморозь)",
        51: "Морось (слабая)",
        53: "Морось (умеренная)",
        55: "Морось (сильная)",
        61: "Дождь (слабый)",
        63: "Дождь (умеренный)",
        65: "Дождь (сильный)",
        71: "Снег (слабый)",
        73: "Снег (умеренный)",
        75: "Снег (сильный)",
        80: "Ливень (слабый)",
        81: "Ливень (умеренный)",
        82: "Ливень (сильный)",
        95: "Гроза",
    }
    return mapping.get(code, f"Погода (код {code})")


async def get_weather_text(session: aiohttp.ClientSession, lat: float, lon: float) -> str:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    async with session.get(url, params=params, timeout=15) as r:
        data = await r.json()

    cur = data.get("current") or {}
    t = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    code = cur.get("weather_code")
    wind = cur.get("wind_speed_10m")

    if t is None or code is None:
        return "Не получилось получить погоду 😕 Попробуй позже."

    desc = wmo_to_text(int(code))
    parts = [desc]
    parts.append(f"🌡 Темп.: {t}°C (ощущается как {feels}°C)" if feels is not None else f"🌡 Темп.: {t}°C")
    if wind is not None:
        parts.append(f"💨 Ветер: {wind} м/с")
    return "\n".join(parts)


# ---------- DB ----------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                city_name TEXT,
                lat REAL,
                lon REAL,
                notify_time TEXT DEFAULT '09:00',
                tz_offset_min INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                last_sent_date TEXT
            )
            """
        )
        await db.commit()


async def upsert_user(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, chat_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id
            """,
            (user_id, chat_id),
        )
        await db.commit()


async def set_city(user_id: int, loc: Location):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET city_name=?, lat=?, lon=?, last_sent_date=NULL
            WHERE user_id=?
            """,
            (loc.name, loc.lat, loc.lon, user_id),
        )
        await db.commit()


async def set_time(user_id: int, hhmm: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET notify_time=?, last_sent_date=NULL WHERE user_id=?",
            (hhmm, user_id),
        )
        await db.commit()


async def set_tz(user_id: int, offset_min: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET tz_offset_min=? WHERE user_id=?",
            (offset_min, user_id),
        )
        await db.commit()


async def set_enabled(user_id: int, enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET enabled=? WHERE user_id=?",
            (1 if enabled else 0, user_id),
        )
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return await cur.fetchone()


async def get_all_enabled_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE enabled=1 AND lat IS NOT NULL AND lon IS NOT NULL")
        return await cur.fetchall()


async def update_last_sent(user_id: int, local_date_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET last_sent_date=? WHERE user_id=?",
            (local_date_str, user_id),
        )
        await db.commit()


async def reset_last_sent(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_sent_date=NULL WHERE user_id=?", (user_id,))
        await db.commit()


# ---------- Helpers ----------

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")  # HH:MM
TZ_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")  # +3 / +03:00 / -0530 etc.


def parse_tz_offset(s: str) -> int | None:
    m = TZ_RE.match(s.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    hh = int(m.group(2))
    mm = int(m.group(3) or "0")
    if hh > 14 or mm >= 60:
        return None
    return sign * (hh * 60 + mm)


def now_user_local(offset_min: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_min)


# ---------- Scheduler ----------

async def scheduler_loop(bot: Bot, session: aiohttp.ClientSession):
    while True:
        try:
            users = await get_all_enabled_users()
            for u in users:
                offset = int(u["tz_offset_min"] or 0)
                local_now = now_user_local(offset)

                hhmm = (u["notify_time"] or "09:00").strip()
                target_h, target_m = map(int, hhmm.split(":"))

                today_str = local_now.date().isoformat()
                target_dt = local_now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)

                # если время прошло и ещё не отправляли сегодня
                if local_now >= target_dt and u["last_sent_date"] != today_str:
                    text = await get_weather_text(session, u["lat"], u["lon"])
                    try:
                        await bot.send_message(
                            chat_id=u["chat_id"],
                            text=f"Ежедневная погода для {u['city_name']}:\n\n{text}"
                        )
                        await update_last_sent(u["user_id"], today_str)
                    except Exception as e:
                        print("send error:", repr(e))

        except Exception as e:
            print("scheduler error:", repr(e))

        await asyncio.sleep(30)


# ---------- Bot ----------

async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Нет BOT_TOKEN в .env")

    await init_db()

    bot = Bot(token=token)
    dp = Dispatcher()
    session = aiohttp.ClientSession()

    # --- commands ---

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        await upsert_user(message.chat.id, message.from_user.id)
        await message.answer("Привет! Настроим ежедневную погоду 👇", reply_markup=main_kb)
        await message.answer(
            "Нажми кнопки снизу или используй команды:\n"
            "/setcity <город>\n"
            "/settime HH:MM\n"
            "/settz +02:00\n"
            "/weather\n"
            "/status\n"
            "/on /off\n"
        )

    @dp.message(Command("setcity"))
    async def cmd_setcity(message: Message):
        await upsert_user(message.chat.id, message.from_user.id)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Формат: /setcity Николаев")
            return

        query = parts[1].strip()
        candidates = await geocode_city_candidates(session, query)
        if not candidates:
            await message.answer("Не нашёл такой город 😕")
            return

        if len(candidates) == 1:
            await set_city(message.from_user.id, candidates[0])
            await message.answer(f"Ок! Город установлен: {candidates[0].name}")
            return

        pending_choices[message.from_user.id] = candidates
        lines = ["Нашёл несколько вариантов. Выбери: /choose N\n"]
        for i, loc in enumerate(candidates, 1):
            lines.append(f"{i}) {loc.name}")
        await message.answer("\n".join(lines))

    @dp.message(Command("choose"))
    async def cmd_choose(message: Message):
        user_id = message.from_user.id
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("Формат: /choose 2")
            return

        candidates = pending_choices.get(user_id)
        if not candidates:
            await message.answer("Сначала задай город: 🏙 Город или /setcity ...")
            return

        idx = int(parts[1])
        if not (1 <= idx <= len(candidates)):
            await message.answer(f"Номер должен быть от 1 до {len(candidates)}")
            return

        loc = candidates[idx - 1]
        await set_city(user_id, loc)
        pending_choices.pop(user_id, None)

        # остаёмся в режиме ввода города (как ты хотел)
        awaiting_input[user_id] = "city"
        await message.answer(f"Ок! Город установлен: {loc.name}\nМожешь сразу ввести другой город.")

    @dp.message(Command("settime"))
    async def cmd_settime(message: Message):
        await upsert_user(message.chat.id, message.from_user.id)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not TIME_RE.match(parts[1].strip()):
            await message.answer("Формат: /settime 09:30")
            return
        hhmm = parts[1].strip()
        await set_time(message.from_user.id, hhmm)
        await message.answer(f"Ок! Время уведомления: {hhmm}")

    @dp.message(Command("settz"))
    async def cmd_settz(message: Message):
        await upsert_user(message.chat.id, message.from_user.id)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Формат: /settz +02:00")
            return
        offset = parse_tz_offset(parts[1])
        if offset is None:
            await message.answer("Не понял таймзону. Пример: /settz +02:00")
            return
        await set_tz(message.from_user.id, offset)
        await message.answer(f"Ок! Таймзона: {parts[1].strip()}")

    @dp.message(Command("on"))
    async def cmd_on(message: Message):
        await upsert_user(message.chat.id, message.from_user.id)
        await set_enabled(message.from_user.id, True)
        await message.answer("Уведомления включены ✅")

    @dp.message(Command("off"))
    async def cmd_off(message: Message):
        await upsert_user(message.chat.id, message.from_user.id)
        await set_enabled(message.from_user.id, False)
        await message.answer("Уведомления выключены ❌")

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        await upsert_user(message.chat.id, message.from_user.id)
        u = await get_user(message.from_user.id)
        if not u:
            await message.answer("Нет настроек. Нажми /start")
            return

        tz_h = int(u["tz_offset_min"] or 0) / 60
        await message.answer(
            "⚙️ Настройки\n"
            f"🏙 Город: {u['city_name'] or 'не задан'}\n"
            f"⏰ Время: {u['notify_time']}\n"
            f"🕒 Таймзона: {tz_h:+.0f}h\n"
            f"🔔 Уведомления: {'вкл' if u['enabled'] else 'выкл'}"
        )

    @dp.message(Command("weather"))
    async def cmd_weather(message: Message):
        await upsert_user(message.chat.id, message.from_user.id)
        u = await get_user(message.from_user.id)
        if not u or u["lat"] is None or u["lon"] is None:
            await message.answer("Сначала задай город: 🏙 Город или /setcity ...")
            return
        text = await get_weather_text(session, u["lat"], u["lon"])
        await message.answer(f"Погода для {u['city_name']}:\n\n{text}")

    @dp.message(Command("reset_sent"))
    async def cmd_reset_sent(message: Message):
        await reset_last_sent(message.from_user.id)
        await message.answer("Сбросил отметку отправки ✅ Теперь уведомление может прийти сегодня.")

    # --- buttons (MUST be above handle_input) ---

    @dp.message(F.text == "🌤 Погода сейчас")
    async def weather_btn(message: Message):
        await cmd_weather(message)

    @dp.message(F.text == "⚙️ Статус")
    async def status_btn(message: Message):
        await cmd_status(message)

    @dp.message(F.text == "✅ Вкл")
    async def on_btn(message: Message):
        await cmd_on(message)

    @dp.message(F.text == "❌ Выкл")
    async def off_btn(message: Message):
        await cmd_off(message)

    @dp.message(F.text == "🏙 Город")
    async def ask_city(message: Message):
        awaiting_input[message.from_user.id] = "city"
        await message.answer("Вводи город (можно много раз). Например: Николаев")

    @dp.message(F.text == "⏰ Время")
    async def ask_time(message: Message):
        awaiting_input[message.from_user.id] = "time"
        await message.answer("Введи время HH:MM, например 08:30")

    @dp.message(F.text == "🕒 Таймзона")
    async def ask_tz(message: Message):
        awaiting_input[message.from_user.id] = "tz"
        await message.answer("Введи таймзону, например +02:00")

    # --- input handler (MUST be last) ---

    @dp.message(F.text)
    async def handle_input(message: Message):
        mode = awaiting_input.get(message.from_user.id)
        if not mode:
            return

        text = message.text.strip()

        if mode == "time":
            if not TIME_RE.match(text):
                await message.answer("Нужно HH:MM, например 09:30")
                return
            await set_time(message.from_user.id, text)
            awaiting_input.pop(message.from_user.id, None)
            await message.answer(f"Готово! Время: {text}")
            return

        if mode == "tz":
            off = parse_tz_offset(text)
            if off is None:
                await message.answer("Пример: +02:00")
                return
            await set_tz(message.from_user.id, off)
            awaiting_input.pop(message.from_user.id, None)
            await message.answer(f"Готово! Таймзона: {text}")
            return

        if mode == "city":
            candidates = await geocode_city_candidates(session, text)
            if not candidates:
                await message.answer("Не нашёл. Попробуй иначе.")
                return

            if len(candidates) == 1:
                await set_city(message.from_user.id, candidates[0])
                # НЕ выходим из режима city — можешь вводить города постоянно
                await message.answer(f"Город установлен: {candidates[0].name}\nМожешь ввести другой город.")
                return

            pending_choices[message.from_user.id] = candidates
            lines = ["Нашёл несколько вариантов. Выбери: /choose N\n"]
            for i, loc in enumerate(candidates, 1):
                lines.append(f"{i}) {loc.name}")
            await message.answer("\n".join(lines))
            return

    async def on_startup(**kwargs):
        print("Scheduler started")
        asyncio.create_task(scheduler_loop(bot, session))

    dp.startup.register(on_startup)

    try:
        await dp.start_polling(bot)
    finally:
        # закрываем твою aiohttp-сессию
        if not session.closed:
            await session.close()

        # закрываем aiohttp-сессию aiogram
        await bot.session.close()

    async def on_shutdown(**kwargs):
        print("Shutting down...")
        await session.close()

    dp.shutdown.register(on_shutdown)


if __name__ == "__main__":
    asyncio.run(main())