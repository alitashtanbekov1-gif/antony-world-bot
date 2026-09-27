import asyncio
import html

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = "8616236751:AAEnrz3fLDeSSCvmxhhv0bUkfrByJOaCSP0"
ADMIN_IDS = {5306960103}
DB_PATH = "antony.db"

router = Router()


# ==================== STATES ====================

class AddService(StatesGroup):
    title_ru = State()
    title_en = State()
    desc_ru = State()
    desc_en = State()
    price = State()
    photo_url = State()
    buy_url = State()


class EditService(StatesGroup):
    value = State()


class ReviewForm(StatesGroup):
    text = State()


class SupportForm(StatesGroup):
    text = State()


class AdminReply(StatesGroup):
    text = State()


class EditSetting(StatesGroup):
    value = State()


# ==================== DATABASE ====================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'ru',
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS services(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title_ru TEXT NOT NULL,
                title_en TEXT NOT NULL,
                desc_ru TEXT NOT NULL,
                desc_en TEXT NOT NULL,
                price TEXT NOT NULL,
                photo_url TEXT,
                buy_url TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reviews(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                text TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)

        defaults = {
            # Отдельные изображения RU / EN для каждого раздела.
            "language_photo_ru": "",
            "language_photo_en": "",
            "menu_photo_ru": "",
            "menu_photo_en": "",
            "services_photo_ru": "",
            "services_photo_en": "",
            "subscription_photo_ru": "",
            "subscription_photo_en": "",
            "reviews_photo_ru": "",
            "reviews_photo_en": "",
            "support_photo_ru": "",
            "support_photo_en": "",

            "global_buy_url": "",

            # Полная настройка раздела подписки.
            "subscription_title_ru": "ПОДПИСКА",
            "subscription_title_en": "SUBSCRIPTION",
            "subscription_ru": "Информация о подписке пока не добавлена.",
            "subscription_en": "Subscription information has not been added yet.",
            "subscription_price_ru": "",
            "subscription_price_en": "",
            "subscription_button_ru": "Оформить подписку",
            "subscription_button_en": "Subscribe",
            "subscription_url": "",
        }

        for key, value in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )

        # Миграция со старой версии: уже установленные русские фото
        # автоматически переносятся в новые RU-настройки.
        legacy_photo_map = {
            "language_photo_url": "language_photo_ru",
            "menu_photo_url": "menu_photo_ru",
            "services_photo_url": "services_photo_ru",
            "subscription_photo_url": "subscription_photo_ru",
            "reviews_photo_url": "reviews_photo_ru",
            "support_photo_url": "support_photo_ru",
        }

        for old_key, new_key in legacy_photo_map.items():
            cur = await db.execute(
                "SELECT value FROM settings WHERE key=?",
                (old_key,),
            )
            old_row = await cur.fetchone()

            cur = await db.execute(
                "SELECT value FROM settings WHERE key=?",
                (new_key,),
            )
            new_row = await cur.fetchone()

            old_value = old_row[0] if old_row else ""
            new_value = new_row[0] if new_row else ""

            if old_value and not new_value:
                await db.execute(
                    "UPDATE settings SET value=? WHERE key=?",
                    (old_value, new_key),
                )

        await db.commit()


async def ensure_user(user):
    if not user:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(user_id, language, username, first_name)
            VALUES(?, 'ru', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (user.id, user.username, user.first_name))
        await db.commit()


async def set_language(user_id: int, lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET language=? WHERE user_id=?",
            (lang, user_id),
        )
        await db.commit()


async def get_language(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT language FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()

    return row[0] if row else "ru"


async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        )
        row = await cur.fetchone()

    return row[0] if row else ""


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))
        await db.commit()


async def get_localized_photo(section: str, lang: str) -> str:
    """Возвращает фото раздела для выбранного языка.

    Если английское фото ещё не настроено, временно используется
    русское (и наоборот), чтобы экран не оставался без изображения.
    """
    primary = await get_setting(f"{section}_photo_{lang}")

    if primary:
        return primary

    fallback_lang = "ru" if lang == "en" else "en"
    return await get_setting(f"{section}_photo_{fallback_lang}")


async def get_services(active_only=True):
    query = """
        SELECT id, title_ru, title_en, desc_ru, desc_en,
               price, photo_url, buy_url, active
        FROM services
    """

    if active_only:
        query += " WHERE active=1"

    query += " ORDER BY id ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query)
        return await cur.fetchall()


async def get_service(service_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, title_ru, title_en, desc_ru, desc_en,
                   price, photo_url, buy_url, active
            FROM services
            WHERE id=?
        """, (service_id,))
        return await cur.fetchone()


async def add_service(data: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO services(
                title_ru, title_en, desc_ru, desc_en,
                price, photo_url, buy_url, active
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            data["title_ru"],
            data["title_en"],
            data["desc_ru"],
            data["desc_en"],
            data["price"],
            data.get("photo_url", ""),
            data.get("buy_url", ""),
        ))
        await db.commit()


async def update_service(service_id: int, field: str, value):
    allowed = {
        "title_ru",
        "title_en",
        "desc_ru",
        "desc_en",
        "price",
        "photo_url",
        "buy_url",
        "active",
    }

    if field not in allowed:
        raise ValueError("Unknown field")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE services SET {field}=? WHERE id=?",
            (value, service_id),
        )
        await db.commit()


async def delete_service(service_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM services WHERE id=?",
            (service_id,),
        )
        await db.commit()


async def add_review(user_id: int, username: str | None, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO reviews(user_id, username, text)
            VALUES(?, ?, ?)
        """, (user_id, username, text))
        await db.commit()


async def get_approved_reviews(limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, username, text, created_at
            FROM reviews
            WHERE approved=1
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        return await cur.fetchall()


async def get_pending_reviews():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, user_id, username, text, created_at
            FROM reviews
            WHERE approved=0
            ORDER BY id DESC
            LIMIT 30
        """)
        return await cur.fetchall()


async def set_review_approved(review_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reviews SET approved=1 WHERE id=?",
            (review_id,),
        )
        await db.commit()


async def delete_review(review_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM reviews WHERE id=?",
            (review_id,),
        )
        await db.commit()


async def add_ticket(user_id: int, username: str | None, message: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO tickets(user_id, username, message)
            VALUES(?, ?, ?)
        """, (user_id, username, message))
        await db.commit()
        return cur.lastrowid


async def get_open_tickets():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, user_id, username, message, created_at
            FROM tickets
            WHERE status='open'
            ORDER BY id DESC
            LIMIT 30
        """)
        return await cur.fetchall()


async def get_ticket(ticket_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, user_id, username, message, status, created_at
            FROM tickets
            WHERE id=?
        """, (ticket_id,))
        return await cur.fetchone()


async def close_ticket(ticket_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET status='closed' WHERE id=?",
            (ticket_id,),
        )
        await db.commit()


# ==================== HELPERS ====================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def esc(value) -> str:
    return html.escape(str(value or ""))


def normalize_url(value: str) -> str:
    value = value.strip()

    if not value or value == "-":
        return ""

    if value.startswith("@"):
        return "https://t.me/" + value[1:]

    if value.startswith("t.me/"):
        return "https://" + value

    return value


def valid_url(value: str) -> bool:
    return (
        value.startswith("https://")
        or value.startswith("http://")
        or value.startswith("tg://")
    )


async def safe_delete(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


# ==================== KEYBOARDS ====================

def language_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🇷🇺 Русский",
                    callback_data="lang:ru",
                ),
                InlineKeyboardButton(
                    text="🇬🇧 English",
                    callback_data="lang:en",
                ),
            ]
        ]
    )


def main_menu_kb(lang: str):
    if lang == "en":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📦 Services",
                        callback_data="services",
                    ),
                    InlineKeyboardButton(
                        text="💎 Subscription",
                        callback_data="subscription",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⭐ Reviews",
                        callback_data="reviews",
                    ),
                    InlineKeyboardButton(
                        text="💬 Support",
                        callback_data="support",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🌐 Language",
                        callback_data="language",
                    ),
                ],
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Услуги",
                    callback_data="services",
                ),
                InlineKeyboardButton(
                    text="💎 Подписка",
                    callback_data="subscription",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Отзывы",
                    callback_data="reviews",
                ),
                InlineKeyboardButton(
                    text="💬 Поддержка",
                    callback_data="support",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Язык",
                    callback_data="language",
                ),
            ],
        ]
    )


def back_home_kb(lang: str):
    text = "🏠 Main menu" if lang == "en" else "🏠 Главное меню"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data="home",
                )
            ]
        ]
    )


def services_section_kb(lang: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "📦 Open services"
                        if lang == "en"
                        else "📦 Открыть услуги"
                    ),
                    callback_data="services:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "🏠 Main menu"
                        if lang == "en"
                        else "🏠 Главное меню"
                    ),
                    callback_data="home",
                )
            ],
        ]
    )


def reviews_section_kb(lang: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "⭐ View reviews"
                        if lang == "en"
                        else "⭐ Посмотреть отзывы"
                    ),
                    callback_data="reviews:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "✍️ Leave a review"
                        if lang == "en"
                        else "✍️ Оставить отзыв"
                    ),
                    callback_data="review:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "🏠 Main menu"
                        if lang == "en"
                        else "🏠 Главное меню"
                    ),
                    callback_data="home",
                )
            ],
        ]
    )


async def send_section_card(
    message: Message,
    photo_url: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
    delete_old=True,
):
    sent = None

    if photo_url and valid_url(photo_url):
        try:
            sent = await message.answer_photo(
                photo=photo_url,
                caption=caption,
                reply_markup=reply_markup,
            )
        except Exception:
            sent = None

    if sent is None:
        sent = await message.answer(
            caption,
            reply_markup=reply_markup,
        )

    if delete_old:
        await safe_delete(message)

    return sent


def reviews_kb(lang: str):
    add_text = "✍️ Leave a review" if lang == "en" else "✍️ Оставить отзыв"
    home_text = "🏠 Main menu" if lang == "en" else "🏠 Главное меню"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=add_text,
                    callback_data="review:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text=home_text,
                    callback_data="home",
                )
            ],
        ]
    )


def support_kb(lang: str):
    write_text = (
        "✉️ Message operator"
        if lang == "en"
        else "✉️ Написать оператору"
    )
    home_text = "🏠 Main menu" if lang == "en" else "🏠 Главное меню"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=write_text,
                    callback_data="support:write",
                )
            ],
            [
                InlineKeyboardButton(
                    text=home_text,
                    callback_data="home",
                )
            ],
        ]
    )


async def service_kb(index: int, total: int, buy_url: str, lang: str):
    rows = []

    nav = []

    if index > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"svc:{index - 1}",
            )
        )

    if index < total - 1:
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"svc:{index + 1}",
            )
        )

    if nav:
        rows.append(nav)

    if buy_url and valid_url(buy_url):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🛒 Buy" if lang == "en" else "🛒 Купить",
                    url=buy_url,
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 Main menu" if lang == "en" else "🏠 Главное меню",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== USER MENU ====================

async def send_main_menu(
    message: Message,
    user_id: int,
    delete_old=False,
):
    lang = await get_language(user_id)
    photo_url = await get_localized_photo("menu", lang)

    if lang == "en":
        caption = (
            "🌍 <b>ANTONY WORLD</b>\n\n"
            "Welcome!\n"
            "Choose a section:"
        )
    else:
        caption = (
            "🌍 <b>ANTONY WORLD</b>\n\n"
            "Добро пожаловать!\n"
            "Выберите нужный раздел:"
        )

    sent = None

    if photo_url and valid_url(photo_url):
        try:
            sent = await message.answer_photo(
                photo=photo_url,
                caption=caption,
                reply_markup=main_menu_kb(lang),
            )
        except Exception:
            sent = None

    if sent is None:
        sent = await message.answer(
            caption,
            reply_markup=main_menu_kb(lang),
        )

    if delete_old:
        await safe_delete(message)

    return sent


async def show_service(
    message: Message,
    user_id: int,
    index: int,
    delete_old=True,
):
    services = await get_services(active_only=True)
    lang = await get_language(user_id)

    if not services:
        text = (
            "📦 Services have not been added yet."
            if lang == "en"
            else "📦 Услуги пока не добавлены."
        )

        await message.answer(
            text,
            reply_markup=back_home_kb(lang),
        )

        if delete_old:
            await safe_delete(message)

        return

    index = max(0, min(index, len(services) - 1))
    service = services[index]

    (
        service_id,
        title_ru,
        title_en,
        desc_ru,
        desc_en,
        price,
        photo_url,
        buy_url,
        active,
    ) = service

    title = title_en if lang == "en" else title_ru
    desc = desc_en if lang == "en" else desc_ru

    if not buy_url:
        buy_url = await get_setting("global_buy_url")

    count = f"{index + 1}/{len(services)}"

    if lang == "en":
        caption = (
            f"📦 <b>{esc(title)}</b>\n\n"
            f"{esc(desc)}\n\n"
            f"💰 <b>Price: {esc(price)}</b>\n\n"
            f"<i>{count}</i>"
        )
    else:
        caption = (
            f"📦 <b>{esc(title)}</b>\n\n"
            f"{esc(desc)}\n\n"
            f"💰 <b>Цена: {esc(price)}</b>\n\n"
            f"<i>{count}</i>"
        )

    kb = await service_kb(
        index,
        len(services),
        buy_url or "",
        lang,
    )

    sent = None

    if photo_url and valid_url(photo_url):
        try:
            sent = await message.answer_photo(
                photo=photo_url,
                caption=caption,
                reply_markup=kb,
            )
        except Exception:
            sent = None

    if sent is None:
        await message.answer(
            caption,
            reply_markup=kb,
        )

    if delete_old:
        await safe_delete(message)


async def show_language_screen(
    message: Message,
    user_id: int,
    delete_old=False,
):
    # При первом запуске язык пользователя = ru.
    # После выбора язык запоминается, поэтому при повторном открытии
    # экрана выбора языка показывается соответствующая RU/EN картинка.
    lang = await get_language(user_id)
    photo_url = await get_localized_photo("language", lang)

    if lang == "en":
        caption = (
            "🌍 <b>ANTONY WORLD</b>\n\n"
            "Welcome to ANTONY WORLD\n"
            "Choose your language:"
        )
    else:
        caption = (
            "🌍 <b>ANTONY WORLD</b>\n\n"
            "Вас приветствует ANTONY WORLD\n"
            "Выберите язык:"
        )

    await send_section_card(
        message,
        photo_url,
        caption,
        language_kb(),
        delete_old=delete_old,
    )


@router.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext,
):
    await state.clear()
    await ensure_user(message.from_user)

    await show_language_screen(
        message,
        message.from_user.id,
    )


@router.callback_query(F.data.startswith("lang:"))
async def language_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await ensure_user(callback.from_user)

    lang = callback.data.split(":", 1)[1]

    if lang not in {"ru", "en"}:
        await callback.answer()
        return

    await set_language(callback.from_user.id, lang)

    await send_main_menu(
        callback.message,
        callback.from_user.id,
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "language")
async def change_language_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await show_language_screen(
        callback.message,
        callback.from_user.id,
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "home")
async def home_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await send_main_menu(
        callback.message,
        callback.from_user.id,
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "services")
async def services_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    lang = await get_language(callback.from_user.id)
    photo_url = await get_localized_photo("services", lang)

    caption = (
        "📦 <b>SERVICES</b>\n\nChoose a service from the catalog."
        if lang == "en"
        else "📦 <b>УСЛУГИ</b>\n\nВыберите нужную услугу в каталоге."
    )

    await send_section_card(
        callback.message,
        photo_url,
        caption,
        services_section_kb(lang),
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "services:list")
async def services_list_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await show_service(
        callback.message,
        callback.from_user.id,
        0,
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data.startswith("svc:"))
async def service_page_handler(callback: CallbackQuery):
    try:
        index = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return

    await show_service(
        callback.message,
        callback.from_user.id,
        index,
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "subscription")
async def subscription_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    lang = await get_language(callback.from_user.id)

    title = await get_setting(
        "subscription_title_en"
        if lang == "en"
        else "subscription_title_ru"
    )

    description = await get_setting(
        "subscription_en"
        if lang == "en"
        else "subscription_ru"
    )

    price = await get_setting(
        "subscription_price_en"
        if lang == "en"
        else "subscription_price_ru"
    )

    button_text = await get_setting(
        "subscription_button_en"
        if lang == "en"
        else "subscription_button_ru"
    )

    url = await get_setting("subscription_url")
    photo_url = await get_localized_photo("subscription", lang)

    caption_parts = [
        f"💎 <b>{esc(title or ('SUBSCRIPTION' if lang == 'en' else 'ПОДПИСКА'))}</b>"
    ]

    if description:
        caption_parts.append(esc(description))

    if price:
        price_label = "Price" if lang == "en" else "Цена"
        caption_parts.append(
            f"💰 <b>{price_label}: {esc(price)}</b>"
        )

    caption = "\n\n".join(caption_parts)

    rows = []

    if url and valid_url(url):
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        button_text
                        or ("Subscribe" if lang == "en" else "Оформить подписку")
                    ),
                    url=url,
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "🏠 Main menu"
                    if lang == "en"
                    else "🏠 Главное меню"
                ),
                callback_data="home",
            )
        ]
    )

    await send_section_card(
        callback.message,
        photo_url,
        caption,
        InlineKeyboardMarkup(inline_keyboard=rows),
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "reviews")
async def reviews_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    lang = await get_language(callback.from_user.id)
    photo_url = await get_localized_photo("reviews", lang)

    caption = (
        "⭐ <b>REVIEWS</b>\n\nRead customer reviews or leave your own."
        if lang == "en"
        else "⭐ <b>ОТЗЫВЫ</b>\n\nПосмотрите отзывы клиентов или оставьте свой."
    )

    await send_section_card(
        callback.message,
        photo_url,
        caption,
        reviews_section_kb(lang),
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "reviews:list")
async def reviews_list_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    lang = await get_language(callback.from_user.id)
    reviews = await get_approved_reviews(5)

    if lang == "en":
        text = "⭐ <b>REVIEWS</b>\n\n"

        if not reviews:
            text += "No published reviews yet."
    else:
        text = "⭐ <b>ОТЗЫВЫ</b>\n\n"

        if not reviews:
            text += "Пока нет опубликованных отзывов."

    if reviews:
        blocks = []

        for _, username, review_text, _ in reviews:
            who = (
                f"@{username}"
                if username
                else (
                    "User"
                    if lang == "en"
                    else "Пользователь"
                )
            )

            blocks.append(
                f"👤 <b>{esc(who)}</b>\n"
                f"{esc(review_text)}"
            )

        text += "\n\n".join(blocks)

    await callback.message.answer(
        text,
        reply_markup=reviews_kb(lang),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(F.data == "review:add")
async def review_add_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    lang = await get_language(callback.from_user.id)

    await state.set_state(ReviewForm.text)

    text = (
        "Напишите ваш отзыв одним сообщением."
        if lang == "ru"
        else "Send your review in one message."
    )

    cancel_text = (
        "❌ Отмена"
        if lang == "ru"
        else "❌ Cancel"
    )

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=cancel_text,
                        callback_data="reviews",
                    )
                ]
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.message(ReviewForm.text)
async def review_receive_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if not message.text:
        return

    lang = await get_language(message.from_user.id)
    review_text = message.text.strip()

    if len(review_text) < 3:
        await message.answer(
            "Отзыв слишком короткий."
            if lang == "ru"
            else "Review is too short."
        )
        return

    await add_review(
        message.from_user.id,
        message.from_user.username,
        review_text,
    )

    await state.clear()

    await message.answer(
        (
            "✅ Спасибо! Отзыв отправлен на модерацию."
            if lang == "ru"
            else "✅ Thank you! Your review was sent for moderation."
        ),
        reply_markup=back_home_kb(lang),
    )

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "без username"
    )

    admin_text = (
        "⭐ <b>Новый отзыв на модерацию</b>\n\n"
        f"От: {esc(message.from_user.full_name)}\n"
        f"Username: {esc(username)}\n"
        f"ID: <code>{message.from_user.id}</code>\n\n"
        f"{esc(review_text)}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
            )
        except Exception:
            pass


@router.callback_query(F.data == "support")
async def support_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    lang = await get_language(callback.from_user.id)

    if lang == "en":
        text = (
            "💬 <b>SUPPORT</b>\n\n"
            "Describe your question and the operator "
            "will reply here."
        )
    else:
        text = (
            "💬 <b>ПОДДЕРЖКА</b>\n\n"
            "Опишите свой вопрос, и оператор ответит "
            "вам прямо в этом боте."
        )

    photo_url = await get_localized_photo("support", lang)

    await send_section_card(
        callback.message,
        photo_url,
        text,
        support_kb(lang),
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "support:write")
async def support_write_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    lang = await get_language(callback.from_user.id)

    await state.set_state(SupportForm.text)

    text = (
        "Напишите обращение одним сообщением."
        if lang == "ru"
        else "Send your request in one message."
    )

    cancel_text = (
        "❌ Отмена"
        if lang == "ru"
        else "❌ Cancel"
    )

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=cancel_text,
                        callback_data="support",
                    )
                ]
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.message(SupportForm.text)
async def support_receive_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if not message.text:
        return

    lang = await get_language(message.from_user.id)
    support_text = message.text.strip()

    if len(support_text) < 2:
        await message.answer(
            "Напишите вопрос подробнее."
            if lang == "ru"
            else "Please add more details."
        )
        return

    ticket_id = await add_ticket(
        message.from_user.id,
        message.from_user.username,
        support_text,
    )

    await state.clear()

    await message.answer(
        (
            f"✅ Обращение #{ticket_id} отправлено оператору."
            if lang == "ru"
            else f"✅ Request #{ticket_id} was sent to the operator."
        ),
        reply_markup=back_home_kb(lang),
    )

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "без username"
    )

    admin_text = (
        f"🎫 <b>Новое обращение #{ticket_id}</b>\n\n"
        f"👤 {esc(message.from_user.full_name)}\n"
        f"Username: {esc(username)}\n"
        f"ID: <code>{message.from_user.id}</code>\n\n"
        f"{esc(support_text)}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Ответить",
                    callback_data=(
                        f"adm_reply:{ticket_id}:"
                        f"{message.from_user.id}"
                    ),
                )
            ]
        ]
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=kb,
            )
        except Exception:
            pass


def admin_back_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Админ-панель",
                    callback_data="adm:home",
                )
            ]
        ]
    )


# ==================== ADMIN ====================

def admin_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Услуги",
                    callback_data="adm:services",
                ),
                InlineKeyboardButton(
                    text="➕ Добавить",
                    callback_data="adm:add",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Отзывы",
                    callback_data="adm:reviews",
                ),
                InlineKeyboardButton(
                    text="🎫 Поддержка",
                    callback_data="adm:tickets",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🖼 Фото разделов RU / EN",
                    callback_data="adm:photos",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 Полная настройка ПОДПИСКИ",
                    callback_data="adm:subscription",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Общая ссылка Купить",
                    callback_data="adm:set:global_buy_url",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Открыть меню бота",
                    callback_data="home",
                )
            ],
        ]
    )


def admin_photos_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌐 Язык RU",
                    callback_data="adm:set:language_photo_ru",
                ),
                InlineKeyboardButton(
                    text="🌐 Language EN",
                    callback_data="adm:set:language_photo_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Меню RU",
                    callback_data="adm:set:menu_photo_ru",
                ),
                InlineKeyboardButton(
                    text="🏠 Menu EN",
                    callback_data="adm:set:menu_photo_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📦 Услуги RU",
                    callback_data="adm:set:services_photo_ru",
                ),
                InlineKeyboardButton(
                    text="📦 Services EN",
                    callback_data="adm:set:services_photo_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💎 Подписка RU",
                    callback_data="adm:set:subscription_photo_ru",
                ),
                InlineKeyboardButton(
                    text="💎 Subscription EN",
                    callback_data="adm:set:subscription_photo_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Отзывы RU",
                    callback_data="adm:set:reviews_photo_ru",
                ),
                InlineKeyboardButton(
                    text="⭐ Reviews EN",
                    callback_data="adm:set:reviews_photo_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Поддержка RU",
                    callback_data="adm:set:support_photo_ru",
                ),
                InlineKeyboardButton(
                    text="💬 Support EN",
                    callback_data="adm:set:support_photo_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Админ-панель",
                    callback_data="adm:home",
                )
            ],
        ]
    )


def admin_subscription_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🖼 Фото RU",
                    callback_data="adm:set:subscription_photo_ru",
                ),
                InlineKeyboardButton(
                    text="🖼 Photo EN",
                    callback_data="adm:set:subscription_photo_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📝 Заголовок RU",
                    callback_data="adm:set:subscription_title_ru",
                ),
                InlineKeyboardButton(
                    text="📝 Title EN",
                    callback_data="adm:set:subscription_title_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 Описание RU",
                    callback_data="adm:set:subscription_ru",
                ),
                InlineKeyboardButton(
                    text="📄 Description EN",
                    callback_data="adm:set:subscription_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💰 Цена RU",
                    callback_data="adm:set:subscription_price_ru",
                ),
                InlineKeyboardButton(
                    text="💰 Price EN",
                    callback_data="adm:set:subscription_price_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔘 Кнопка RU",
                    callback_data="adm:set:subscription_button_ru",
                ),
                InlineKeyboardButton(
                    text="🔘 Button EN",
                    callback_data="adm:set:subscription_button_en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Ссылка подписки",
                    callback_data="adm:set:subscription_url",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Админ-панель",
                    callback_data="adm:home",
                )
            ],
        ]
    )


async def show_admin_home(
    message: Message,
    delete_old=False,
):
    await message.answer(
        "⚙️ <b>ANTONY WORLD — АДМИН-ПАНЕЛЬ</b>\n\n"
        "Все основные настройки меняются здесь "
        "без редактирования кода.",
        reply_markup=admin_main_kb(),
    )

    if delete_old:
        await safe_delete(message)


@router.message(Command("admin"))
async def admin_command_handler(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    await state.clear()
    await show_admin_home(message)


@router.callback_query(F.data == "adm:photos")
async def admin_photos_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()

    await callback.message.answer(
        "🖼 <b>ФОТО РАЗДЕЛОВ RU / EN</b>\n\n"
        "Для каждого раздела можно установить отдельное "
        "русское и английское изображение.\n\n"
        "Отправляйте прямую HTTPS-ссылку на картинку.",
        reply_markup=admin_photos_kb(),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(F.data == "adm:subscription")
async def admin_subscription_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()

    await callback.message.answer(
        "💎 <b>ПОЛНАЯ НАСТРОЙКА ПОДПИСКИ</b>\n\n"
        "Здесь настраиваются фото RU/EN, заголовок, описание, "
        "цена, текст кнопки и ссылка оформления.",
        reply_markup=admin_subscription_kb(),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(F.data == "adm:home")
async def admin_home_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    await state.clear()

    await show_admin_home(
        callback.message,
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "adm:services")
async def admin_services_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()

    services = await get_services(active_only=False)
    rows = []

    for service in services:
        (
            service_id,
            title_ru,
            _,
            _,
            _,
            _,
            _,
            _,
            active,
        ) = service

        icon = "🟢" if active else "⚫"

        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"{icon} #{service_id} "
                        f"{title_ru[:28]}"
                    ),
                    callback_data=f"adm_svc:{service_id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить услугу",
                callback_data="adm:add",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Админ-панель",
                callback_data="adm:home",
            )
        ]
    )

    text = (
        "📦 <b>УСЛУГИ</b>\n\nВыберите услугу:"
        if services
        else "📦 Услуг пока нет."
    )

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


def admin_service_kb(
    service_id: int,
    active: int,
):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Название RU",
                    callback_data=(
                        f"adm_edit:{service_id}:title_ru"
                    ),
                ),
                InlineKeyboardButton(
                    text="🌐 Название EN",
                    callback_data=(
                        f"adm_edit:{service_id}:title_en"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📝 Описание RU",
                    callback_data=(
                        f"adm_edit:{service_id}:desc_ru"
                    ),
                ),
                InlineKeyboardButton(
                    text="🌐 Описание EN",
                    callback_data=(
                        f"adm_edit:{service_id}:desc_en"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💰 Цена",
                    callback_data=(
                        f"adm_edit:{service_id}:price"
                    ),
                ),
                InlineKeyboardButton(
                    text="🖼 Фото",
                    callback_data=(
                        f"adm_edit:{service_id}:photo_url"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Купить",
                    callback_data=(
                        f"adm_edit:{service_id}:buy_url"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "🙈 Скрыть"
                        if active
                        else "👁 Показать"
                    ),
                    callback_data=f"adm_toggle:{service_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=(
                        f"adm_delete_ask:{service_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К услугам",
                    callback_data="adm:services",
                )
            ],
        ]
    )


async def send_admin_service(
    message: Message,
    service_id: int,
    delete_old=False,
):
    service = await get_service(service_id)

    if not service:
        await message.answer(
            "Услуга не найдена.",
            reply_markup=admin_back_kb(),
        )

        if delete_old:
            await safe_delete(message)

        return

    (
        sid,
        title_ru,
        title_en,
        desc_ru,
        desc_en,
        price,
        photo_url,
        buy_url,
        active,
    ) = service

    text = (
        f"📦 <b>Услуга #{sid}</b>\n\n"
        f"<b>RU:</b> {esc(title_ru)}\n"
        f"<b>EN:</b> {esc(title_en)}\n\n"
        f"<b>Описание RU:</b>\n"
        f"{esc(desc_ru)}\n\n"
        f"<b>Описание EN:</b>\n"
        f"{esc(desc_en)}\n\n"
        f"<b>Цена:</b> {esc(price)}\n"
        f"<b>Фото:</b> "
        f"{esc(photo_url) if photo_url else '—'}\n"
        f"<b>Купить:</b> "
        f"{esc(buy_url) if buy_url else 'общая ссылка'}\n"
        f"<b>Статус:</b> "
        f"{'активна' if active else 'скрыта'}"
    )

    await message.answer(
        text,
        reply_markup=admin_service_kb(
            sid,
            active,
        ),
    )

    if delete_old:
        await safe_delete(message)


@router.callback_query(F.data.startswith("adm_svc:"))
async def admin_service_card_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    service_id = int(
        callback.data.split(":")[1]
    )

    await send_admin_service(
        callback.message,
        service_id,
        delete_old=True,
    )

    await callback.answer()


@router.callback_query(F.data == "adm:add")
async def admin_add_start_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()
    await state.set_state(AddService.title_ru)

    await callback.message.answer(
        "➕ <b>Добавление услуги</b>\n\n"
        "1/7. Отправьте название на русском:",
        reply_markup=admin_back_kb(),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.message(AddService.title_ru)
async def admin_add_title_ru_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    await state.update_data(
        title_ru=message.text.strip()
    )

    await state.set_state(AddService.title_en)

    await message.answer(
        "2/7. Отправьте название на английском:"
    )


@router.message(AddService.title_en)
async def admin_add_title_en_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    await state.update_data(
        title_en=message.text.strip()
    )

    await state.set_state(AddService.desc_ru)

    await message.answer(
        "3/7. Отправьте описание на русском:"
    )


@router.message(AddService.desc_ru)
async def admin_add_desc_ru_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    await state.update_data(
        desc_ru=message.text.strip()
    )

    await state.set_state(AddService.desc_en)

    await message.answer(
        "4/7. Отправьте описание на английском:"
    )


@router.message(AddService.desc_en)
async def admin_add_desc_en_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    await state.update_data(
        desc_en=message.text.strip()
    )

    await state.set_state(AddService.price)

    await message.answer(
        "5/7. Отправьте цену, например: "
        "<code>1500 ₽</code>"
    )


@router.message(AddService.price)
async def admin_add_price_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    await state.update_data(
        price=message.text.strip()
    )

    await state.set_state(AddService.photo_url)

    await message.answer(
        "6/7. Отправьте <b>прямую HTTPS-ссылку "
        "на фото</b>.\n\n"
        "Если фото не нужно — отправьте "
        "<code>-</code>."
    )


@router.message(AddService.photo_url)
async def admin_add_photo_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    value = normalize_url(message.text)

    if value and not valid_url(value):
        await message.answer(
            "Нужна ссылка, начинающаяся с "
            "https:// или отправьте -"
        )
        return

    await state.update_data(photo_url=value)
    await state.set_state(AddService.buy_url)

    await message.answer(
        "7/7. Отправьте ссылку для кнопки "
        "<b>Купить</b>.\n\n"
        "Можно отправить @username_бота, "
        "https://t.me/... или <code>-</code>, "
        "чтобы использовать общую ссылку."
    )


@router.message(AddService.buy_url)
async def admin_add_buy_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    value = normalize_url(message.text)

    if value and not valid_url(value):
        await message.answer(
            "Неверная ссылка. Отправьте "
            "https://..., @username или -"
        )
        return

    await state.update_data(buy_url=value)

    data = await state.get_data()

    await add_service(data)
    await state.clear()

    await message.answer(
        "✅ Услуга добавлена.",
        reply_markup=admin_back_kb(),
    )


EDIT_FIELD_NAMES = {
    "title_ru": "название RU",
    "title_en": "название EN",
    "desc_ru": "описание RU",
    "desc_en": "описание EN",
    "price": "цену",
    "photo_url": "ссылку на фото",
    "buy_url": "ссылку Купить",
}


@router.callback_query(
    F.data.startswith("adm_edit:")
)
async def admin_edit_start_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    _, service_id, field = callback.data.split(
        ":",
        2,
    )

    if field not in EDIT_FIELD_NAMES:
        await callback.answer(
            "Неизвестное поле",
            show_alert=True,
        )
        return

    await state.clear()
    await state.set_state(EditService.value)

    await state.update_data(
        service_id=int(service_id),
        field=field,
    )

    extra = ""

    if field in {"photo_url", "buy_url"}:
        extra = (
            "\nОтправьте <code>-</code>, "
            "чтобы очистить значение."
        )

    await callback.message.answer(
        "✏️ Отправьте новое значение: "
        f"<b>{EDIT_FIELD_NAMES[field]}</b>."
        f"{extra}",
        reply_markup=admin_back_kb(),
    )

    await callback.answer()


@router.message(EditService.value)
async def admin_edit_value_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    data = await state.get_data()

    service_id = data["service_id"]
    field = data["field"]
    value = message.text.strip()

    if field in {"photo_url", "buy_url"}:
        value = normalize_url(value)

        if value and not valid_url(value):
            await message.answer(
                "Неверная ссылка. Нужна "
                "https://..., @username или -"
            )
            return

    if (
        not value
        and field not in {"photo_url", "buy_url"}
    ):
        await message.answer(
            "Значение не может быть пустым."
        )
        return

    await update_service(
        service_id,
        field,
        value,
    )

    await state.clear()

    await message.answer("✅ Изменено.")

    await send_admin_service(
        message,
        service_id,
    )


@router.callback_query(
    F.data.startswith("adm_toggle:")
)
async def admin_toggle_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    service_id = int(
        callback.data.split(":")[1]
    )

    service = await get_service(service_id)

    if not service:
        await callback.answer(
            "Услуга не найдена",
            show_alert=True,
        )
        return

    new_value = 0 if service[8] else 1

    await update_service(
        service_id,
        "active",
        new_value,
    )

    await send_admin_service(
        callback.message,
        service_id,
        delete_old=True,
    )

    await callback.answer("Статус изменён")


@router.callback_query(
    F.data.startswith("adm_delete_ask:")
)
async def admin_delete_ask_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    service_id = int(
        callback.data.split(":")[1]
    )

    await callback.message.answer(
        f"⚠️ Удалить услугу #{service_id} "
        "без возможности восстановления?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑 Да, удалить",
                        callback_data=(
                            f"adm_delete:{service_id}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Нет",
                        callback_data=(
                            f"adm_svc:{service_id}"
                        ),
                    )
                ],
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(
    F.data.startswith("adm_delete:")
)
async def admin_delete_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    service_id = int(
        callback.data.split(":")[1]
    )

    await delete_service(service_id)

    await callback.message.answer(
        "✅ Услуга удалена.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К услугам",
                        callback_data="adm:services",
                    )
                ]
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


# ==================== ADMIN SETTINGS ====================

SETTING_TITLES = {
    "global_buy_url": "общую ссылку кнопки Купить",

    "language_photo_ru": "фото ВЫБОРА ЯЗЫКА — RU",
    "language_photo_en": "фото ВЫБОРА ЯЗЫКА — EN",
    "menu_photo_ru": "фото ГЛАВНОГО МЕНЮ — RU",
    "menu_photo_en": "фото ГЛАВНОГО МЕНЮ — EN",
    "services_photo_ru": "фото УСЛУГИ — RU",
    "services_photo_en": "фото SERVICES — EN",
    "subscription_photo_ru": "фото ПОДПИСКА — RU",
    "subscription_photo_en": "фото SUBSCRIPTION — EN",
    "reviews_photo_ru": "фото ОТЗЫВЫ — RU",
    "reviews_photo_en": "фото REVIEWS — EN",
    "support_photo_ru": "фото ПОДДЕРЖКА — RU",
    "support_photo_en": "фото SUPPORT — EN",

    "subscription_title_ru": "заголовок подписки RU",
    "subscription_title_en": "заголовок подписки EN",
    "subscription_ru": "описание подписки RU",
    "subscription_en": "описание подписки EN",
    "subscription_price_ru": "цену подписки RU",
    "subscription_price_en": "цену подписки EN",
    "subscription_button_ru": "текст кнопки подписки RU",
    "subscription_button_en": "текст кнопки подписки EN",
    "subscription_url": "ссылку оформления подписки",
}



@router.callback_query(
    F.data.startswith("adm:set:")
)
async def admin_setting_start_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    key = callback.data.split(":", 2)[2]

    if key not in SETTING_TITLES:
        await callback.answer()
        return

    current = await get_setting(key)

    await state.clear()
    await state.set_state(EditSetting.value)

    await state.update_data(
        setting_key=key
    )

    extra = ""

    if key in {
        "global_buy_url",
        "language_photo_ru",
        "language_photo_en",
        "menu_photo_ru",
        "menu_photo_en",
        "services_photo_ru",
        "services_photo_en",
        "subscription_photo_ru",
        "subscription_photo_en",
        "reviews_photo_ru",
        "reviews_photo_en",
        "support_photo_ru",
        "support_photo_en",
        "subscription_url",
    }:
        extra = (
            "\nЧтобы очистить — "
            "отправьте <code>-</code>."
        )

    await callback.message.answer(
        f"⚙️ Изменяем "
        f"<b>{SETTING_TITLES[key]}</b>.\n\n"
        f"Текущее значение:\n"
        f"<code>"
        f"{esc(current) if current else '—'}"
        f"</code>\n\n"
        f"Отправьте новое значение."
        f"{extra}",
        reply_markup=admin_back_kb(),
    )

    await callback.answer()


@router.message(EditSetting.value)
async def admin_setting_value_handler(
    message: Message,
    state: FSMContext,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    data = await state.get_data()
    key = data["setting_key"]
    value = message.text.strip()

    if key in {
        "global_buy_url",
        "language_photo_ru",
        "language_photo_en",
        "menu_photo_ru",
        "menu_photo_en",
        "services_photo_ru",
        "services_photo_en",
        "subscription_photo_ru",
        "subscription_photo_en",
        "reviews_photo_ru",
        "reviews_photo_en",
        "support_photo_ru",
        "support_photo_en",
        "subscription_url",
    }:
        value = normalize_url(value)

        if value and not valid_url(value):
            await message.answer(
                "Неверная ссылка. Используйте "
                "https://..., @username или -"
            )
            return

    await set_setting(
        key,
        value,
    )

    await state.clear()

    await message.answer(
        "✅ Настройка сохранена.",
        reply_markup=admin_back_kb(),
    )


# ==================== ADMIN REVIEWS ====================

@router.callback_query(F.data == "adm:reviews")
async def admin_reviews_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()

    reviews = await get_pending_reviews()
    rows = []

    for (
        review_id,
        user_id,
        username,
        text,
        created_at,
    ) in reviews:
        label = (
            f"#{review_id} @{username}"
            if username
            else f"#{review_id} ID {user_id}"
        )

        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:45],
                    callback_data=(
                        f"adm_review:{review_id}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Админ-панель",
                callback_data="adm:home",
            )
        ]
    )

    text = (
        "⭐ <b>ОТЗЫВЫ НА МОДЕРАЦИИ</b>\n\n"
        "Выберите отзыв:"
        if reviews
        else "⭐ Новых отзывов на модерации нет."
    )

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(
    F.data.startswith("adm_review:")
)
async def admin_review_card_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    review_id = int(
        callback.data.split(":")[1]
    )

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, user_id, username, text,
                   approved, created_at
            FROM reviews
            WHERE id=?
        """, (review_id,))

        row = await cur.fetchone()

    if not row:
        await callback.answer(
            "Отзыв не найден",
            show_alert=True,
        )
        return

    (
        rid,
        uid,
        username,
        review_text,
        approved,
        created_at,
    ) = row

    await callback.message.answer(
        f"⭐ <b>Отзыв #{rid}</b>\n\n"
        f"User ID: <code>{uid}</code>\n"
        f"Username: "
        f"{esc('@' + username) if username else '—'}"
        f"\n\n{esc(review_text)}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Опубликовать",
                        callback_data=(
                            f"adm_review_ok:{rid}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="🗑 Удалить",
                        callback_data=(
                            f"adm_review_del:{rid}"
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="adm:reviews",
                    )
                ],
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(
    F.data.startswith("adm_review_ok:")
)
async def admin_review_ok_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    review_id = int(
        callback.data.split(":")[1]
    )

    await set_review_approved(review_id)

    await callback.message.answer(
        "✅ Отзыв опубликован.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К отзывам",
                        callback_data="adm:reviews",
                    )
                ]
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(
    F.data.startswith("adm_review_del:")
)
async def admin_review_delete_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    review_id = int(
        callback.data.split(":")[1]
    )

    await delete_review(review_id)

    await callback.message.answer(
        "🗑 Отзыв удалён.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К отзывам",
                        callback_data="adm:reviews",
                    )
                ]
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


# ==================== ADMIN SUPPORT ====================

@router.callback_query(F.data == "adm:tickets")
async def admin_tickets_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()

    tickets = await get_open_tickets()
    rows = []

    for (
        ticket_id,
        user_id,
        username,
        text,
        created_at,
    ) in tickets:
        label = (
            f"#{ticket_id} @{username}"
            if username
            else f"#{ticket_id} ID {user_id}"
        )

        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:45],
                    callback_data=(
                        f"adm_ticket:{ticket_id}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Админ-панель",
                callback_data="adm:home",
            )
        ]
    )

    text = (
        "🎫 <b>ОТКРЫТЫЕ ОБРАЩЕНИЯ</b>\n\n"
        "Выберите обращение:"
        if tickets
        else "🎫 Открытых обращений нет."
    )

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(
    F.data.startswith("adm_ticket:")
)
async def admin_ticket_card_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    row = await get_ticket(ticket_id)

    if not row:
        await callback.answer(
            "Обращение не найдено",
            show_alert=True,
        )
        return

    (
        tid,
        uid,
        username,
        ticket_text,
        status,
        created_at,
    ) = row

    await callback.message.answer(
        f"🎫 <b>Обращение #{tid}</b>\n\n"
        f"User ID: <code>{uid}</code>\n"
        f"Username: "
        f"{esc('@' + username) if username else '—'}\n"
        f"Статус: {esc(status)}\n\n"
        f"{esc(ticket_text)}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="↩️ Ответить",
                        callback_data=(
                            f"adm_reply:{tid}:{uid}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✅ Закрыть",
                        callback_data=(
                            f"adm_ticket_close:{tid}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="adm:tickets",
                    )
                ],
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


@router.callback_query(
    F.data.startswith("adm_reply:")
)
async def admin_reply_start_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        return

    _, ticket_id, user_id = callback.data.split(
        ":"
    )

    await state.clear()
    await state.set_state(AdminReply.text)

    await state.update_data(
        ticket_id=int(ticket_id),
        user_id=int(user_id),
    )

    await callback.message.answer(
        "↩️ Отправьте ответ пользователю "
        f"<code>{user_id}</code> одним сообщением.",
        reply_markup=admin_back_kb(),
    )

    await callback.answer()


@router.message(AdminReply.text)
async def admin_reply_send_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if (
        not is_admin(message.from_user.id)
        or not message.text
    ):
        return

    data = await state.get_data()

    ticket_id = data["ticket_id"]
    user_id = data["user_id"]

    try:
        lang = await get_language(user_id)

        title = (
            "💬 <b>Support reply</b>"
            if lang == "en"
            else "💬 <b>Ответ поддержки</b>"
        )

        await bot.send_message(
            user_id,
            f"{title}\n\n{esc(message.text)}",
            reply_markup=back_home_kb(lang),
        )

    except Exception as ex:
        await message.answer(
            f"❌ Не удалось отправить ответ: "
            f"{esc(ex)}"
        )
        return

    await close_ticket(ticket_id)
    await state.clear()

    await message.answer(
        f"✅ Ответ отправлен. "
        f"Обращение #{ticket_id} закрыто.",
        reply_markup=admin_back_kb(),
    )


@router.callback_query(
    F.data.startswith("adm_ticket_close:")
)
async def admin_ticket_close_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    await close_ticket(ticket_id)

    await callback.message.answer(
        f"✅ Обращение #{ticket_id} закрыто.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К обращениям",
                        callback_data="adm:tickets",
                    )
                ]
            ]
        ),
    )

    await safe_delete(callback.message)
    await callback.answer()


# ==================== FALLBACK ====================

@router.message()
async def fallback_handler(message: Message):
    await ensure_user(message.from_user)

    lang = await get_language(
        message.from_user.id
    )

    await message.answer(
        (
            "Используйте кнопки меню 👇"
            if lang == "ru"
            else "Use the menu buttons 👇"
        ),
        reply_markup=back_home_kb(lang),
    )


# ==================== START ====================

async def main():
    if BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Вставьте токен бота в BOT_TOKEN "
            "в начале файла bot.py"
        )

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(
                command="start",
                description="Запустить бота",
            ),
            BotCommand(
                command="admin",
                description="Админ-панель",
            ),
        ]
    )

    print("ANTONY WORLD BOT запущен ✅")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
