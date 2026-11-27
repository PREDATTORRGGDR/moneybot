import asyncio
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from decimal import Decimal

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
# Админы, которым не слать алерт о новом пользователе (покупки им приходят как всем)
NEW_USER_ALERTS_OFF = {int(x) for x in os.getenv("NEW_USER_ALERTS_OFF", "").replace(" ", "").split(",") if x}
GRAM_AMOUNT = os.getenv("GRAM_AMOUNT", "0.1")
PRICE_STARS = int(os.getenv("PRICE_STARS", "100"))
PRICE_TON = os.getenv("PRICE_TON", "")          # пусто = оплата GRAM отключена
TON_WALLET = os.getenv("TON_WALLET", "")
TONCENTER_KEY = os.getenv("TONCENTER_API_KEY", "")
# Можно несколько через запятую: под каждым будет своя кнопка
SUPPORT = [u.strip().lstrip("@") for u in os.getenv("SUPPORT_USERNAME", "").split(",") if u.strip()]
REVIEWS_URL = os.getenv("REVIEWS_URL", "")
REVIEWS_CHAT_ID = os.getenv("REVIEWS_CHAT_ID", "")

BANNER = os.path.join(os.path.dirname(__file__), "assets", "banner.png")
TON_ENABLED = bool(PRICE_TON and TON_WALLET)

# Оплата переводом на карту: покупатель переводит рубли и присылает PDF-чек, админ подтверждает вручную.
# Пусто PRICE_RUB или CARD_NUMBER = способ выключен.
PRICE_RUB = os.getenv("PRICE_RUB", "")
CARD_NUMBER = os.getenv("CARD_NUMBER", "").replace(" ", "")
CARD_BANK = os.getenv("CARD_BANK", "")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
CARD_ENABLED = bool(PRICE_RUB and CARD_NUMBER)
RECEIPT_MAX_BYTES = 10 * 1024 * 1024

# ---------- база ----------

db = sqlite3.connect(os.path.join(os.path.dirname(__file__), "bot.db"))
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, username TEXT, recipient TEXT,
    method TEXT, code TEXT, status TEXT DEFAULT 'new',
    charge_id TEXT UNIQUE, reviewed INTEGER DEFAULT 0, created INTEGER)""")
db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, first_seen INTEGER)")
_cols = {r[1] for r in db.execute("PRAGMA table_info(orders)")}
for _col in ("amount REAL", "paid_at INTEGER", "done_at INTEGER"):
    if _col.split()[0] not in _cols:
        db.execute(f"ALTER TABLE orders ADD COLUMN {_col}")
db.commit()


def order(order_id):
    return db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def set_status(order_id, status, charge_id=None, amount=None):
    now = int(time.time())
    db.execute("""UPDATE orders SET status=?, charge_id=COALESCE(?, charge_id), amount=COALESCE(?, amount),
                  paid_at=CASE WHEN ?='paid' THEN ? ELSE paid_at END,
                  done_at=CASE WHEN ?='done' THEN ? ELSE done_at END WHERE id=?""",
               (status, charge_id, amount, status, now, status, now, order_id))
    db.commit()


# ---------- клавиатуры ----------

def btn(text, data=None, url=None, style=None):
    return InlineKeyboardButton(text=text, callback_data=data, url=url, style=style)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


HOME = btn("🏠 В главное меню", "home")

def main_kb():
    # «Отзывы» сразу открывают канал с отзывами; без REVIEWS_URL остаётся экран внутри бота
    reviews = btn("⭐ Отзывы", url=REVIEWS_URL, style="primary") if REVIEWS_URL \
        else btn("⭐ Отзывы", "rev", style="primary")
    return kb(
        [btn("💎 Активация", "act", style="success")],
        [reviews],
        [btn("🆘 Поддержка", "sup", style="danger")],
    )


def main_text(user):
    name = f"@{user.username}" if user.username else html.escape(user.first_name)
    ton_line = f"\n🔷 <b>GRAM (TON)</b> — {PRICE_TON} GRAM" if TON_ENABLED else ""
    ton_line += f"\n💳 <b>Картой</b> — {PRICE_RUB} ₽" if CARD_ENABLED else ""
    return (
        "💎 <b>Gram Wallet («Деньги») | Активация кошелька</b>\n\n"
        f"Привет, {name} 🥹\n\n"
        "💰 <b>Бот поможет активировать кошелёк «Деньги» в Telegram</b>\n\n"
        f"🎁 <b>Активация</b> — получи первый перевод <b>{GRAM_AMOUNT} GRAM</b>, "
        "и кошелёк откроется. Открытие раздела зависит от условий Telegram.\n\n"
        "🪙 <b>Как это работает:</b>\n"
        "1. Нажми «Активация»\n"
        "2. Выбери способ оплаты и укажи @username\n"
        "3. Оплати заказ\n"
        f"4. После подтверждения бот отправит <b>{GRAM_AMOUNT} GRAM</b> 💎\n\n"
        f"Стоимость {GRAM_AMOUNT.replace('.', ',')} GRAM: <b>{PRICE_STARS}</b> ⭐️ <b>Stars</b>{ton_line}"
    )


# ---------- навигация ----------

router = Router()
# file_id баннера после первой загрузки. Лежит в файле, чтобы после рестарта не грузить картинку заново.
BANNER_ID_FILE = os.path.join(os.path.dirname(__file__), "banner_id.txt")
banner_id = open(BANNER_ID_FILE).read().strip() if os.path.exists(BANNER_ID_FILE) else None


class Order(StatesGroup):
    recipient = State()
    receipt = State()  # оплата картой: ждём PDF-чек


class Review(StatesGroup):
    text = State()


class AdminMsg(StatesGroup):
    text = State()


async def send_menu(bot: Bot, chat_id, user):
    global banner_id
    if os.path.exists(BANNER) or banner_id:
        msg = await bot.send_photo(chat_id, banner_id or FSInputFile(BANNER),
                                   caption=main_text(user), reply_markup=main_kb())
        if msg.photo[-1].file_id != banner_id:
            banner_id = msg.photo[-1].file_id
            with open(BANNER_ID_FILE, "w") as f:
                f.write(banner_id)
    else:
        await bot.send_message(chat_id, main_text(user), reply_markup=main_kb())


async def drop(msg: Message):
    try:
        await msg.delete()
    except Exception:
        pass


async def edit_screen(m: Message, text, markup):
    """Меняет экран в том же сообщении: у фото подпись, у текста текст. False, если править нельзя."""
    if not isinstance(m, Message):  # InaccessibleMessage: кнопку нажали в сообщении старше 48 ч
        return False
    try:
        if m.photo:
            await m.edit_caption(caption=text, reply_markup=markup)
        else:
            await m.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        return True
    except TelegramBadRequest as e:
        if "not modified" in str(e):  # тот же экран нажали второй раз
            return True
        logging.warning("edit failed, sending new: %s", e)
        return False


async def replace(cb: CallbackQuery, text, markup):
    """Показывает новый экран на месте старого (одно сообщение, баннер остаётся).
    Часики на кнопке снимаем сразу, чтобы клик ощущался мгновенным."""
    await cb.answer()
    if not await edit_screen(cb.message, text, markup):
        await drop(cb.message)
        await cb.bot.send_message(cb.message.chat.id, text, reply_markup=markup, disable_web_page_preview=True)


async def notify_admins(bot: Bot, text, markup=None, skip=()):
    """Шлёт сообщение всем админам, кроме skip. Упавший админ (не писал боту, заблокировал) не мешает остальным."""
    for admin in ADMIN_IDS:
        if admin in skip:
            continue
        try:
            await bot.send_message(admin, text, reply_markup=markup)
        except Exception:
            logging.warning("admin notify %s failed", admin, exc_info=True)


def user_line(u):
    name = html.escape(u.full_name or "")
    nick = f"@{u.username}" if u.username else "без ника"
    return f"{name} ({nick}, <code>{u.id}</code>)"


@router.message(CommandStart())
async def start(m: Message, state: FSMContext, bot: Bot, command: CommandObject):
    await state.clear()
    cur = db.execute("INSERT OR IGNORE INTO users (id, username, first_seen) VALUES (?,?,?)",
                     (m.from_user.id, m.from_user.username, int(time.time())))
    db.commit()
    await send_menu(bot, m.chat.id, m.from_user)
    if cur.rowcount and m.from_user.id not in ADMIN_IDS:
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        source = f"\nИсточник: <code>{html.escape(command.args)}</code>" if command.args else ""
        await notify_admins(bot, f"👤 <b>Новый пользователь</b>\n{user_line(m.from_user)}{source}\n"
                                 f"Всего пользователей: <b>{total}</b>", skip=NEW_USER_ALERTS_OFF)


# ---------- админка ----------

STATUS_NAMES = {"new": "🕓 не оплачен", "review": "🧾 чек на проверке", "paid": "⏳ ждёт отправки",
                "done": "✅ выполнен", "refunded": "↩️ возврат", "cancelled": "❌ отменён"}


def stats_text():
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    paid_sql = "status IN ('paid','done') AND COALESCE(paid_at, created) >= ?"

    lines = ["📊 <b>Статистика</b>\n"]
    for name, since in (("Сегодня", midnight), ("7 дней", now - 7 * 86400),
                        ("30 дней", now - 30 * 86400), ("Всё время", 0)):
        cnt = q(f"SELECT COUNT(*) FROM orders WHERE {paid_sql}", since)
        stars = q(f"SELECT COALESCE(SUM(COALESCE(amount, ?)), 0) FROM orders WHERE method='stars' AND {paid_sql}",
                  PRICE_STARS, since)
        gram = q(f"SELECT COALESCE(SUM(COALESCE(amount, 0)), 0) FROM orders WHERE method='ton' AND {paid_sql}",
                 since)
        rub = q(f"SELECT COALESCE(SUM(COALESCE(amount, 0)), 0) FROM orders WHERE method='card' AND {paid_sql}",
                since)
        users = q("SELECT COUNT(*) FROM users WHERE first_seen >= ?", since)
        money = f"{stars:g} ⭐" + (f" + {gram:g} GRAM" if gram else "") + (f" + {rub:g} ₽" if rub else "")
        lines.append(f"<b>{name}:</b> продаж {cnt} · {money} · новых {users}")

    users_total = q("SELECT COUNT(*) FROM users")
    buyers = q("SELECT COUNT(DISTINCT user_id) FROM orders WHERE status IN ('paid','done')")
    done = q("SELECT COUNT(*) FROM orders WHERE status='done'")
    conv = f"{buyers / users_total * 100:.1f}%" if users_total else "—"
    lines += [
        "",
        f"👥 Пользователей: <b>{users_total}</b>",
        f"🛒 Покупателей: <b>{buyers}</b> (конверсия {conv})",
        f"💎 Отправлено GRAM: <b>{done * float(GRAM_AMOUNT):g}</b>",
        "",
        "<b>Заказы по статусам:</b>",
    ]
    for status, label in STATUS_NAMES.items():
        lines.append(f"{label}: {q('SELECT COUNT(*) FROM orders WHERE status=?', status)}")
    lines.append(f"\n<i>Обновлено {time.strftime('%d.%m %H:%M')}</i>")
    return "\n".join(lines)


def admin_kb():
    pending = db.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0]
    return kb(
        [btn("🔄 Обновить", "adm:stats", style="primary")],
        [btn(f"⏳ Ждут отправки ({pending})", "adm:pending", style="success" if pending else None)],
        [btn("📋 Последние заказы", "adm:last")],
    )


@router.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(stats_text(), reply_markup=admin_kb())


@router.callback_query(F.data == "adm:stats", F.from_user.id.in_(ADMIN_IDS))
async def admin_refresh(cb: CallbackQuery):
    try:
        await cb.message.edit_text(stats_text(), reply_markup=admin_kb())
    except TelegramBadRequest:
        pass  # ничего не изменилось
    await cb.answer("Обновлено")


@router.callback_query(F.data == "adm:pending", F.from_user.id.in_(ADMIN_IDS))
async def admin_pending(cb: CallbackQuery):
    rows = db.execute("SELECT * FROM orders WHERE status='paid' ORDER BY id LIMIT 20").fetchall()
    if not rows:
        return await cb.answer("Все заказы отправлены 👌", show_alert=True)
    await cb.answer()
    for o in rows:
        text, markup = admin_card(o)
        await cb.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:last", F.from_user.id.in_(ADMIN_IDS))
async def admin_last(cb: CallbackQuery):
    rows = db.execute("SELECT * FROM orders WHERE status != 'new' ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        return await cb.answer("Заказов пока нет", show_alert=True)
    lines = ["📋 <b>Последние заказы</b>\n"]
    for o in rows:
        who = f"@{o['username']}" if o["username"] else str(o["user_id"])
        when = time.strftime("%d.%m %H:%M", time.localtime(o["paid_at"] or o["created"]))
        lines.append(f"#{o['id']} · {when} · {html.escape(who)} → {html.escape(o['recipient'])} · "
                     f"{STATUS_NAMES.get(o['status'], o['status'])}")
    await cb.message.answer("\n".join(lines))
    await cb.answer()


# ---------- премиум-иконки ----------
# icons.json: {"💎": "<custom_emoji_id>", "Назад": "<id>", ...}
# Ключ-эмодзи заменяется на премиум-эмодзи в текстах и на иконку в кнопках,
# ключ-слово (например «Назад») добавляет иконку кнопке, начинающейся с этого слова.

ICONS_FILE = os.path.join(os.path.dirname(__file__), "icons.json")
ICONS = json.load(open(ICONS_FILE, encoding="utf-8")) if os.path.exists(ICONS_FILE) else {}
BUTTON_EMOJI = ["💎", "🔷", "⭐", "🆘", "🏠", "✉️", "👤", "✅", "✏️", "👀", "✍️", "↩️", "❌", "Назад", "Отмена"]


def norm(s):
    return s.replace("️", "")


def save_icons():
    with open(ICONS_FILE, "w", encoding="utf-8") as f:
        json.dump(ICONS, f, ensure_ascii=False, indent=1)


def with_tg_emoji(text):
    emojis = [k for k in ICONS if not k[0].isalnum()]
    if not text or not emojis:
        return text
    pattern = "|".join(re.escape(norm(k)) + "️?" for k in sorted(emojis, key=len, reverse=True))
    return re.sub(pattern, lambda m: f'<tg-emoji emoji-id="{ICONS[norm(m.group())]}">{m.group()}</tg-emoji>', text)


class IconsMiddleware(BaseRequestMiddleware):
    """Подставляет премиум-эмодзи в исходящие сообщения. Если Telegram их не принял — шлёт как было."""

    async def __call__(self, make_request, bot, method):
        if not ICONS:
            return await make_request(bot, method)
        # Не deep: в методе, вызванном через m.answer(), лежит ссылка на Bot (сессия, loop),
        # deepcopy её не переживает. Глубоко копируем только клавиатуру, которую меняем.
        new = method.model_copy()
        changed = False
        for field in ("text", "caption"):
            old = getattr(new, field, None)
            if isinstance(old, str) and (val := with_tg_emoji(old)) != old:
                setattr(new, field, val)
                changed = True
        markup = getattr(new, "reply_markup", None)
        if isinstance(markup, InlineKeyboardMarkup):
            markup = markup.model_copy(deep=True)
            new.reply_markup = markup
            for row in markup.inline_keyboard:
                for b in row:
                    first, _, rest = b.text.partition(" ")
                    icon = ICONS.get(norm(first))
                    if icon and not b.icon_custom_emoji_id:
                        b.icon_custom_emoji_id = icon
                        if not first[0].isalnum() and rest:
                            b.text = rest
                        changed = True
        if not changed:
            return await make_request(bot, method)
        try:
            return await make_request(bot, new)
        except TelegramBadRequest as e:
            logging.warning("премиум-эмодзи не приняты: %s", e)
            return await make_request(bot, method)


@router.message(Command("seticon"), F.from_user.id.in_(ADMIN_IDS))
async def set_icon(m: Message):
    rest = m.text.split(maxsplit=1)[1].strip() if " " in m.text.strip() else ""
    custom = [e for e in (m.entities or []) if e.type == "custom_emoji"]
    if not rest:
        return await icons_list(m)
    key = rest
    if custom:  # всё до премиум-эмодзи — ключ; если ключа нет, берём обычный вариант эмодзи
        alt = custom[-1].extract_from(m.text)
        key = rest[:rest.rfind(alt)].strip() or alt
    key = norm(key)
    if custom:
        ICONS[key] = custom[-1].custom_emoji_id
        save_icons()
        await m.answer(f"✅ Иконка для «{html.escape(key)}» сохранена")
    else:
        ICONS.pop(key, None)
        save_icons()
        await m.answer(f"🗑 Иконка для «{html.escape(key)}» убрана")


@router.message(Command("icons"), F.from_user.id.in_(ADMIN_IDS))
async def icons_list(m: Message):
    lines = [f"{'✅' if norm(k) in ICONS else '▫️'} {html.escape(k)}" for k in BUTTON_EMOJI]
    extra = [k for k in ICONS if k not in map(norm, BUTTON_EMOJI)]
    lines += [f"✅ {html.escape(k)}" for k in extra]
    await m.answer(
        "🎨 <b>Премиум-иконки</b>\n\n"
        "Отправь: /seticon 💎 и через пробел свой премиум-эмодзи.\n"
        "Обычный эмодзи 💎 везде заменится на премиум, а в кнопках станет иконкой.\n"
        "Для кнопок без эмодзи: /seticon Назад + премиум-эмодзи.\n"
        "Убрать: /seticon 💎 без премиум-эмодзи.\n\n" + "\n".join(lines))


@router.callback_query(F.data == "home")
async def home(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await cb.answer()
    # Сообщение с баннером просто возвращаем к меню. Текстовое (экран после ввода ника) в фото
    # не превратить, его меняем на новое меню с баннером.
    if getattr(cb.message, "photo", None) and await edit_screen(cb.message, main_text(cb.from_user), main_kb()):
        return
    await asyncio.gather(drop(cb.message), send_menu(bot, cb.message.chat.id, cb.from_user))


# ---------- поддержка ----------

SUPPORT_TEXT = ("🆘 <b>Поддержка</b>\n\nЕсли возникла проблема, напиши одному из владельцев: "
                + ", ".join(f"@{u}" for u in SUPPORT)
                + ".\n\nУкажи номер заказа, так ответим быстрее.")

TERMS_TEXT = (
    "📄 <b>Условия перевода</b>\n\n"
    f"1. После оплаты мы отправляем <b>{GRAM_AMOUNT} GRAM</b> на указанный @username или адрес кошелька.\n"
    "2. Перевод выполняется вручную, обычно в течение нескольких минут, максимум — 24 часа.\n"
    "3. Переводы в блокчейне необратимы. Проверь получателя перед оплатой: "
    "за ошибку в @username или адресе мы не отвечаем.\n"
    "4. Если мы не смогли выполнить заказ — вернём оплату полностью.\n"
    "5. После отправки GRAM возврат невозможен.\n"
    "6. Сервис не связан с Telegram. Открытие кошелька зависит от условий Telegram.\n\n"
    "Оплачивая заказ, ты соглашаешься с этими условиями. Вопросы по оплате — /paysupport"
)


def support_kb():
    rows = [[btn(f"✉️ Написать @{u}", url=f"https://t.me/{u}", style="primary")] for u in SUPPORT]
    return kb(*rows, [HOME])


@router.callback_query(F.data == "sup")
async def support(cb: CallbackQuery):
    await replace(cb, SUPPORT_TEXT, support_kb())


@router.message(Command("paysupport"))
async def paysupport_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(SUPPORT_TEXT, reply_markup=support_kb())


@router.message(Command("terms"))
async def terms_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(TERMS_TEXT, reply_markup=kb([HOME]))


# ---------- активация ----------

@router.callback_query(F.data == "act")
async def activation(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[btn(f"⭐ Оплата Stars — {PRICE_STARS} ⭐", "pay:stars", style="success")]]
    if TON_ENABLED:
        rows.append([btn(f"🔷 Оплата GRAM — {PRICE_TON}", "pay:ton", style="primary")])
    if CARD_ENABLED:
        rows.append([btn(f"💳 Оплата картой — {PRICE_RUB} ₽", "pay:card", style="primary")])
    rows.append([btn("Назад", "home", style="danger")])
    await replace(cb, f"🪙 <b>Выбери способ оплаты</b>\n\nТы получишь <b>{GRAM_AMOUNT} GRAM</b> "
                      "на свой кошелёк «Деньги».", kb(*rows))


@router.callback_query(F.data.startswith("pay:"))
async def ask_recipient(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Order.recipient)
    await state.update_data(method=cb.data.split(":")[1])
    rows = []
    if cb.from_user.username:
        rows.append([btn(f"👤 Себе (@{cb.from_user.username})", "me", style="success")])
    rows.append([btn("Назад", "act", style="danger")])
    await replace(cb, "✍️ <b>Кому отправить GRAM?</b>\n\nПришли <b>@username</b> получателя "
                      "или <b>адрес кошелька</b>.", kb(*rows))


USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")
ADDRESS_RE = re.compile(r"^(?:[EUk0][Qf][A-Za-z0-9_-]{46}|-?\d:[0-9a-fA-F]{64})$")


async def confirm_screen(state: FSMContext, recipient):
    await state.update_data(recipient=recipient)
    data = await state.get_data()
    price = {"stars": f"{PRICE_STARS} ⭐", "card": f"{PRICE_RUB} ₽"}.get(data["method"], f"{PRICE_TON} GRAM")
    text = ("📋 <b>Проверь заказ</b>\n\n"
            f"Получатель: <code>{html.escape(recipient)}</code>\n"
            f"Получит: <b>{GRAM_AMOUNT} GRAM</b>\n"
            f"К оплате: <b>{price}</b>\n\n"
            "⚠️ Перевод в блокчейне необратим — проверь получателя.")
    return text, kb(
        [btn("✅ Всё верно, оплатить", "confirm", style="success")],
        [btn("✏️ Изменить получателя", f"pay:{data['method']}", style="primary")],
        [btn("Отмена", "home", style="danger")],
    )


@router.callback_query(F.data == "me", StateFilter(Order.recipient))
async def recipient_me(cb: CallbackQuery, state: FSMContext):
    await replace(cb, *await confirm_screen(state, f"@{cb.from_user.username}"))


@router.message(StateFilter(Order.recipient), F.text)
async def recipient_text(m: Message, state: FSMContext):
    t = m.text.strip()
    if u := USERNAME_RE.match(t):
        text, markup = await confirm_screen(state, "@" + u.group(1))
        await m.answer(text, reply_markup=markup)
    elif ADDRESS_RE.match(t):
        text, markup = await confirm_screen(state, t)
        await m.answer(text, reply_markup=markup)
    else:
        await m.answer("Не похоже на @username или адрес кошелька. Попробуй ещё раз 🙂")


@router.callback_query(F.data == "confirm", StateFilter(Order.recipient))
async def create_order(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if "recipient" not in data:
        return await cb.answer("Сначала укажи получателя", show_alert=True)
    await state.clear()
    code = "MA" + secrets.token_hex(3).upper()
    cur = db.execute(
        "INSERT INTO orders (user_id, username, recipient, method, code, created) VALUES (?,?,?,?,?,?)",
        (cb.from_user.id, cb.from_user.username, data["recipient"], data["method"], code, int(time.time())))
    db.commit()
    oid = cur.lastrowid

    if data["method"] == "stars":
        # Счёт это отдельный тип сообщения, в него экран не превратить: экран становится шапкой заказа
        await replace(cb, f"🧾 <b>Заказ #{oid}</b>\n\nПолучатель: <code>{html.escape(data['recipient'])}</code>\n"
                          f"Получит: <b>{GRAM_AMOUNT} GRAM</b>\n\nОплати счёт ниже 👇", kb([HOME]))
        await bot.send_invoice(
            cb.message.chat.id,
            title=f"Активация кошелька — {GRAM_AMOUNT} GRAM",
            description=f"Заказ #{oid}. Получатель: {data['recipient']}",
            payload=f"order:{oid}",
            currency="XTR",
            prices=[LabeledPrice(label=f"{GRAM_AMOUNT} GRAM", amount=PRICE_STARS)],
        )
    elif data["method"] == "card":
        await state.set_state(Order.receipt)
        await state.update_data(order_id=oid)
        holder = " · ".join(x for x in (CARD_BANK, CARD_HOLDER) if x)
        await replace(
            cb,
            f"💳 <b>Заказ #{oid}, оплата картой</b>\n\n"
            f"1. Переведи <b>{PRICE_RUB} ₽</b> на карту:\n<code>{CARD_NUMBER}</code>\n{html.escape(holder)}\n\n"
            "2. Сохрани чек в приложении банка в <b>PDF</b> (операция → «Чек» → «Поделиться» → PDF) "
            "и пришли его сюда файлом.\n\n"
            "⚠️ Скриншот не подойдёт, нужен именно PDF-чек. После проверки бот пришлёт уведомление.",
            kb([HOME]))
    else:
        await replace(
            cb,
            f"💎 <b>Заказ #{oid}</b>\n\n"
            f"Отправь <b>{PRICE_TON} GRAM</b> на адрес:\n<code>{TON_WALLET}</code>\n\n"
            f"С комментарием (обязательно!):\n<code>{code}</code>\n\n"
            "После перевода нажми «Я оплатил».",
            kb([btn("✅ Я оплатил", f"check:{oid}", style="success")], [HOME]))


# ---------- оплата картой: PDF-чек ----------

def pending_card_order(uid):
    return db.execute("SELECT * FROM orders WHERE user_id=? AND method='card' AND status='new' "
                      "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()


RECEIPT_HELP = ("Нужен <b>PDF-чек</b> из приложения банка: открой перевод → «Чек» → «Поделиться» → PDF "
                "и пришли файл сюда. Скриншот не подойдёт.")


@router.message(F.document)
async def receipt_doc(m: Message, state: FSMContext, bot: Bot):
    """PDF от человека с неоплаченным заказом картой = чек. Работает и если он уходил в меню."""
    o = pending_card_order(m.from_user.id)
    if not o:
        raise SkipHandler()  # обычный файл: дальше по хендлерам (уйдёт админам как сообщение)
    doc = m.document
    if doc.mime_type != "application/pdf" and not (doc.file_name or "").lower().endswith(".pdf"):
        return await m.answer("❌ Это не PDF. " + RECEIPT_HELP)
    if doc.file_size and doc.file_size > RECEIPT_MAX_BYTES:
        return await m.answer("❌ Файл слишком большой. " + RECEIPT_HELP)
    set_status(o["id"], "review")
    await state.clear()
    await m.answer(f"🧾 Чек по заказу #{o['id']} получен, проверяем. "
                   "Обычно это до 15 минут, бот пришлёт уведомление.", reply_markup=kb([HOME]))
    caption = (f"🧾 <b>Чек на проверке, заказ #{o['id']}</b>\n"
               f"Покупатель: {user_line(m.from_user)}\n"
               f"Получатель: <code>{html.escape(o['recipient'])}</code>\n"
               f"Сумма: <b>{PRICE_RUB} ₽</b> на карту *{CARD_NUMBER[-4:]}\n\n"
               "Сверь поступление в банке и нажми кнопку.")
    markup = kb([btn("✅ Оплата пришла", f"rcpt_ok:{o['id']}", style="success")],
                [btn("❌ Чек не подходит", f"rcpt_no:{o['id']}", style="danger")],
                [btn("🔍 Найти покупателя", f"who:{m.from_user.id}", style="primary")])
    for admin in ADMIN_IDS:
        try:
            await bot.send_document(admin, doc.file_id, caption=caption, reply_markup=markup)
        except Exception:
            logging.warning("receipt to admin %s failed", admin, exc_info=True)


@router.message(StateFilter(Order.receipt))
async def receipt_wrong(m: Message):
    await m.answer(("❌ Скриншот не подойдёт. " if m.photo else "") + RECEIPT_HELP)


@router.callback_query(F.data.regexp(r"^rcpt_(ok|no):\d+$"), F.from_user.id.in_(ADMIN_IDS))
async def receipt_decision(cb: CallbackQuery, bot: Bot):
    action, oid = cb.data.removeprefix("rcpt_").split(":")
    o = order(int(oid))
    if not o or o["status"] != "review":  # второй админ уже нажал
        return await cb.answer(f"Уже обработан: {STATUS_NAMES.get(o['status'], o['status']) if o else '?'}",
                               show_alert=True)
    await cb.answer()
    if action == "ok":
        set_status(o["id"], "paid", amount=float(PRICE_RUB))
        mark = "✅ Оплата подтверждена"
    else:
        set_status(o["id"], "cancelled")
        mark = "❌ Чек отклонён"
    try:
        await cb.message.edit_caption(caption=(cb.message.html_text or "") +
                                      f"\n\n<b>{mark}</b> ({html.escape(cb.from_user.first_name)})")
    except TelegramBadRequest:
        pass
    if action == "ok":
        await on_paid(bot, o["id"])  # покупателю «оплата получена», админам карточка с «Отправил»
    else:
        try:
            await bot.send_message(o["user_id"], f"❌ Чек по заказу #{o['id']} не подтвердился. "
                                                 "Если деньги списались, напиши в поддержку и приложи чек.",
                                   reply_markup=support_kb())
        except Exception:
            logging.warning("buyer notify %s failed", o["user_id"], exc_info=True)


# ---------- оплата Stars ----------

@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    o = order(int(q.invoice_payload.split(":")[1]))
    if (o and o["status"] == "new" and o["user_id"] == q.from_user.id
            and q.total_amount == PRICE_STARS):
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Заказ устарел. Создай новый через /start")


@router.message(F.successful_payment)
async def paid_stars(m: Message, bot: Bot):
    oid = int(m.successful_payment.invoice_payload.split(":")[1])
    set_status(oid, "paid", m.successful_payment.telegram_payment_charge_id, m.successful_payment.total_amount)
    await on_paid(bot, oid)


# ---------- оплата GRAM (TON) ----------

async def find_ton_payment(code):
    """Ищет входящий перевод с нужным комментарием и суммой. Возвращает хеш транзакции."""
    need = int(Decimal(PRICE_TON) * 10**9)
    headers = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else {}
    async with aiohttp.ClientSession(trust_env=True) as s:
        async with s.get("https://toncenter.com/api/v2/getTransactions",
                         params={"address": TON_WALLET, "limit": 100}, headers=headers) as r:
            res = await r.json()
    for tx in res.get("result", []):
        msg = tx.get("in_msg") or {}
        if (msg.get("message") or "").strip() == code and int(msg.get("value", 0)) >= need:
            return tx["transaction_id"]["hash"]
    return None


@router.callback_query(F.data.startswith("check:"))
async def check_ton(cb: CallbackQuery, bot: Bot):
    o = order(int(cb.data.split(":")[1]))
    if not o or o["user_id"] != cb.from_user.id:
        return await cb.answer("Заказ не найден", show_alert=True)
    if o["status"] != "new":
        return await cb.answer("Оплата уже получена ✅", show_alert=True)
    try:
        tx = await find_ton_payment(o["code"])
    except Exception:
        logging.exception("toncenter")
        return await cb.answer("Не удалось проверить, попробуй через минуту", show_alert=True)
    if not tx:
        return await cb.answer("Платёж пока не найден. Если уже отправил — подожди 1–2 минуты "
                               "и нажми ещё раз.", show_alert=True)
    try:
        set_status(o["id"], "paid", "ton:" + tx, float(PRICE_TON))
    except sqlite3.IntegrityError:
        return await cb.answer("Эта транзакция уже использована", show_alert=True)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await on_paid(bot, o["id"])


# ---------- после оплаты / админ ----------

async def on_paid(bot: Bot, oid):
    o = order(oid)
    try:  # даже если покупатель заблокировал бота, админы должны узнать об оплате
        await bot.send_message(o["user_id"],
                               f"✅ <b>Оплата получена!</b>\n\nЗаказ #{oid}: {GRAM_AMOUNT} GRAM → "
                               f"<code>{html.escape(o['recipient'])}</code>\n"
                               "Отправим в ближайшее время, бот пришлёт уведомление.")
    except Exception:
        logging.warning("buyer notify %s failed", o["user_id"], exc_info=True)
    text, markup = admin_card(o)
    paid_today = db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('paid','done') AND paid_at >= ?",
                            (int(time.time()) - 86400,)).fetchone()[0]
    await notify_admins(bot, f"💰 <b>Новая покупка!</b> (за 24 ч: {paid_today})\n\n{text}", markup)


def admin_card(o):
    """Карточка оплаченного заказа для админа с кнопками."""
    oid = o["id"]
    who = f"@{o['username']}" if o["username"] else f"id {o['user_id']}"
    unit = {"stars": "⭐", "card": "₽ картой"}.get(o["method"], "GRAM")
    fallback = {"stars": PRICE_STARS, "card": PRICE_RUB or 0}.get(o["method"], PRICE_TON or 0)
    amount = float(o["amount"] if o["amount"] is not None else fallback)
    second = btn("↩️ Вернуть Stars", f"refund:{oid}", style="danger") if o["method"] == "stars" \
        else btn("❌ Отменить", f"cancel:{oid}", style="danger")
    text = (f"🆕 <b>Заказ #{oid}</b>\n"
            f"Покупатель: {html.escape(who)} (<code>{o['user_id']}</code>)\n"
            f"Получатель: <code>{html.escape(o['recipient'])}</code>\n"
            f"Отправить: <b>{GRAM_AMOUNT} GRAM</b>\nОплачено: {amount:g} {unit}")
    return text, kb([btn("✅ Отправил", f"done:{oid}", style="success")], [second],
                    [btn("🔍 Найти покупателя", f"who:{o['user_id']}", style="primary")])


@router.callback_query(F.data.regexp(r"^(done|refund|cancel):\d+$"), F.from_user.id.in_(ADMIN_IDS))
async def admin_action(cb: CallbackQuery, bot: Bot):
    action, oid = cb.data.split(":")
    o = order(int(oid))
    if o["status"] != "paid":
        return await cb.answer(f"Заказ уже обработан: {o['status']}", show_alert=True)

    if action == "done":
        set_status(o["id"], "done")
        await bot.send_message(
            o["user_id"],
            f"🎉 <b>Заказ #{oid} выполнен!</b>\n\n{GRAM_AMOUNT} GRAM отправлен на "
            f"<code>{html.escape(o['recipient'])}</code>. Проверь кошелёк «Деньги».\n\n"
            "Будем рады отзыву 🙏",
            reply_markup=kb([btn("✍️ Оставить отзыв", "rev:new", style="success")], [HOME]))
        mark = "✅ Выполнено"
    elif action == "refund":
        await bot.refund_star_payment(o["user_id"], o["charge_id"])
        set_status(o["id"], "refunded")
        await bot.send_message(o["user_id"], f"↩️ Заказ #{oid} отменён, Stars возвращены.")
        mark = "↩️ Возврат"
    else:
        set_status(o["id"], "cancelled")
        await bot.send_message(o["user_id"], f"❌ Заказ #{oid} отменён. Вопросы — в поддержку.")
        mark = "❌ Отменён"

    await cb.message.edit_text(cb.message.html_text + f"\n\n<b>{mark}</b> ({cb.from_user.first_name})")
    await cb.answer()


# ---------- отзывы ----------

@router.callback_query(F.data == "rev")
async def reviews(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    if REVIEWS_URL:
        rows.append([btn("👀 Посмотреть отзывы", url=REVIEWS_URL, style="primary")])
    rows += [[btn("✍️ Оставить отзыв", "rev:new", style="success")], [HOME]]
    await replace(cb, "⭐ <b>Отзывы</b>\n\nПосмотри отзывы или поделись впечатлением "
                      "о завершённом заказе.", kb(*rows))


@router.callback_query(F.data == "rev:new")
async def review_new(cb: CallbackQuery, state: FSMContext):
    o = db.execute("SELECT id FROM orders WHERE user_id=? AND status='done' AND reviewed=0 "
                   "ORDER BY id DESC", (cb.from_user.id,)).fetchone()
    if not o:
        return await cb.answer("Отзыв можно оставить после выполненного заказа 🙂", show_alert=True)
    await state.set_state(Review.text)
    await state.update_data(order_id=o["id"])
    await replace(cb, f"✍️ <b>Отзыв о заказе #{o['id']}</b>\n\nНапиши пару слов одним сообщением.",
                  kb([btn("Отмена", "rev", style="danger")]))


@router.message(StateFilter(Review.text), F.text)
async def review_text(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    db.execute("UPDATE orders SET reviewed=1 WHERE id=?", (data["order_id"],))
    db.commit()
    who = f"@{m.from_user.username}" if m.from_user.username else html.escape(m.from_user.first_name)
    text = f"⭐ <b>Отзыв</b> от {who}\n\n{html.escape(m.text[:1000])}"
    targets = [REVIEWS_CHAT_ID] if REVIEWS_CHAT_ID else ADMIN_IDS
    for chat in targets:
        try:
            await bot.send_message(chat, text)
        except Exception:
            logging.exception("review post %s", chat)
    await m.answer("Спасибо за отзыв! 💙", reply_markup=kb([HOME]))


# ---------- поиск человека по ID ----------
# Ник покупатель может сменить после заказа, ID не меняется никогда.
# get_chat отдаёт актуальный профиль любого, кто хоть раз писал боту.

def known_usernames(uid):
    rows = db.execute("SELECT username FROM users WHERE id=? UNION SELECT username FROM orders WHERE user_id=?",
                      (uid, uid)).fetchall()
    return {r[0] for r in rows if r[0]}


async def lookup_user(bot: Bot, uid: int):
    old_names = known_usernames(uid)
    lines = [f"🔍 <b>Пользователь</b> <code>{uid}</code>\n"]
    username = None
    try:
        chat = await bot.get_chat(uid)
        username = chat.username
        lines.append(f'Имя: <a href="tg://user?id={uid}">{html.escape(chat.full_name or "без имени")}</a>')
        lines.append(f"Ник сейчас: <b>@{username}</b>" if username else "Ник сейчас: <b>нет ника</b>")
        db.execute("UPDATE users SET username=? WHERE id=?", (username, uid))
        db.commit()
    except Exception as e:
        lines.append(f"Профиль не получен: {html.escape(str(e))[:150]}")
    old_names.discard(username)
    if old_names:
        lines.append("Прежний ник: " + ", ".join(f"@{html.escape(n)}" for n in sorted(old_names)))

    old_lower = {n.lower() for n in old_names}
    orders = db.execute("SELECT id, recipient, status FROM orders WHERE user_id=? AND status != 'new' "
                        "ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    if orders:
        lines.append("\n<b>Заказы:</b>")
        for o in orders:
            warn = " ⚠️ старый ник, уточни получателя" if o["recipient"].lstrip("@").lower() in old_lower else ""
            lines.append(f"#{o['id']} → <code>{html.escape(o['recipient'])}</code> · "
                         f"{STATUS_NAMES.get(o['status'], o['status'])}{warn}")

    rows = []
    if username:
        rows.append([btn(f"👤 Открыть @{username}", url=f"https://t.me/{username}", style="primary")])
    rows.append([btn("✉️ Написать через бота", f"msg:{uid}", style="success")])
    return "\n".join(lines), kb(*rows)


async def send_lookup(bot: Bot, chat_id, uid):
    text, markup = await lookup_user(bot, uid)
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramBadRequest:  # ссылку на профиль по ID запрещают настройки приватности
        await bot.send_message(chat_id, re.sub(r'<a href="tg://user[^"]*">(.*?)</a>', r"\1", text),
                               reply_markup=markup)


@router.message(Command("user"), F.from_user.id.in_(ADMIN_IDS))
async def user_cmd(m: Message, state: FSMContext, bot: Bot, command: CommandObject):
    await state.clear()
    arg = (command.args or "").strip().lstrip("@")
    if arg.isdigit():
        return await send_lookup(bot, m.chat.id, int(arg))
    if arg:  # по нику, в том числе старому: ищем в своей базе
        row = db.execute("SELECT id FROM users WHERE lower(username)=lower(?) "
                         "UNION SELECT user_id FROM orders WHERE lower(username)=lower(?) LIMIT 1",
                         (arg, arg)).fetchone()
        if row:
            return await send_lookup(bot, m.chat.id, row[0])
        return await m.answer(f"В базе нет @{html.escape(arg)}. Попробуй по ID.")
    await m.answer("🔍 Поиск человека: <code>/user 8409289830</code> или <code>/user @ник</code> "
                   "(подходит и старый ник). Ещё можно нажать «Найти покупателя» под заказом.")


@router.callback_query(F.data.regexp(r"^who:\d+$"), F.from_user.id.in_(ADMIN_IDS))
async def who_cb(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await send_lookup(bot, cb.message.chat.id, int(cb.data.split(":")[1]))


@router.callback_query(F.data.regexp(r"^msg:\d+$"), F.from_user.id.in_(ADMIN_IDS))
async def admin_msg_start(cb: CallbackQuery, state: FSMContext):
    uid = int(cb.data.split(":")[1])
    await state.set_state(AdminMsg.text)
    await state.update_data(to=uid)
    await cb.message.answer(f"✉️ Пришли сообщение для <code>{uid}</code>: текст, фото или голос. "
                            "Бот отправит его от своего имени.", reply_markup=kb([btn("Отмена", "home", style="danger")]))
    await cb.answer()


@router.message(StateFilter(AdminMsg.text), F.from_user.id.in_(ADMIN_IDS))
async def admin_msg_send(m: Message, state: FSMContext, bot: Bot):
    uid = (await state.get_data())["to"]
    await state.clear()
    try:
        await bot.copy_message(uid, m.chat.id, m.message_id)
        await m.answer(f"✅ Доставлено <code>{uid}</code>. Ответ человека придёт сюда.")
    except Exception as e:
        await m.answer(f"❌ Не доставлено: {html.escape(str(e))[:200]}")


# ---------- админ прислал набор премиум-эмодзи ----------

@router.message(F.from_user.id.in_(ADMIN_IDS), F.entities)
async def emoji_inbox(m: Message):
    custom = [e for e in m.entities if e.type == "custom_emoji"]
    if not custom:
        return
    items = [{"n": i + 1, "alt": e.extract_from(m.text), "id": e.custom_emoji_id} for i, e in enumerate(custom)]
    with open(os.path.join(os.path.dirname(__file__), "emoji_inbox.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    lines = [f'{x["n"]}. <tg-emoji emoji-id="{x["id"]}">{x["alt"]}</tg-emoji>' for x in items]
    await m.answer(f"📥 Получил {len(items)} премиум-эмодзи:\n\n" + "\n".join(lines))


# ---------- сообщение покупателя боту -> админам ----------
# Последний хендлер: сюда попадает только то, что не поймали меню, заказ и отзыв.

@router.message(StateFilter(None), F.chat.type == "private", ~F.from_user.id.in_(ADMIN_IDS))
async def user_to_admins(m: Message, bot: Bot):
    if m.text and m.text.startswith("/"):
        return
    head = f"💬 <b>Сообщение от</b> {user_line(m.from_user)}"
    markup = kb([btn("✉️ Ответить", f"msg:{m.from_user.id}", style="success")],
                [btn("🔍 Найти покупателя", f"who:{m.from_user.id}", style="primary")])
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, head, reply_markup=markup)
            await bot.copy_message(admin, m.chat.id, m.message_id)
        except Exception:
            logging.warning("forward to admin %s failed", admin, exc_info=True)


# ---------- запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)
    proxy = os.getenv("BOT_PROXY") or os.getenv("HTTPS_PROXY")
    bot = Bot(BOT_TOKEN, session=AiohttpSession(proxy=proxy) if proxy else None,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(IconsMiddleware())
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
    ])
    for admin in ADMIN_IDS:  # админам в меню ещё и /admin
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="admin", description="Админка и статистика"),
            ], scope=BotCommandScopeChat(chat_id=admin))
        except Exception:
            logging.warning("не удалось задать меню админу %s (он ещё не писал боту?)", admin)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
