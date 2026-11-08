import os
import json
import time
import asyncio
import httpx
import logging
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / "bot.env"
DATA_FILE = BASE / "danati_orders.json"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@")

# Реквизитҳои ҳозираро нигоҳ доштем.
DC_CARD = "9762 0002 6172 0113"
DC_OWNER = "Jumaboi Khojazoda"
DC_PHONE = "989047040"

DIAMONDS = [
    ("💎 110 алмаз — 9 сомонӣ", "110 алмаз", 9),
    ("💎 341 алмаз — 28 сомонӣ", "341 алмаз", 28),
    ("💎 572 алмаз — 48 сомонӣ", "572 алмаз", 48),
    ("💎 1166 алмаз — 90 сомонӣ", "1166 алмаз", 90),
    ("💎 2398 алмаз — 180 сомонӣ", "2398 алмаз", 180),
    ("💎 6160 алмаз — 455 сомонӣ", "6160 алмаз", 455),
]
VOUCHERS = [
    ("🎁 Lite 90 — 6 сомонӣ", "Lite 90", 6),
    ("🎁 Ҳафта 450 — 17 сомонӣ", "Ҳафта 450", 17),
    ("🎁 Моҳона 2600 — 70 сомонӣ", "Моҳона 2600", 70),
]
LEVELS = [
    ("🏆 Level 6 — 6 сомонӣ", "Level 6", 6),
    ("🏆 Level 10 — 9 сомонӣ", "Level 10", 9),
    ("🏆 Level 15 — 9 сомонӣ", "Level 15", 9),
    ("🏆 Level 20 — 9 сомонӣ", "Level 20", 9),
    ("🏆 Level 25 — 9 сомонӣ", "Level 25", 9),
    ("🏆 Level 30 — 10 сомонӣ", "Level 30", 10),
]

def load_data():
    if not DATA_FILE.exists():
        return {"orders": [], "next_id": 1001, "wallets": {}, "topups": []}
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        data.setdefault("orders", [])
        data.setdefault("wallets", {})
        data.setdefault("topups", [])
        data.setdefault("next_id", 1001)
        return data
    except (ValueError, OSError) as exc:
        logging.exception("Cannot read order database; refusing to start with empty data")
        raise RuntimeError(
            f"Базаи маълумот хонда нашуд: {DATA_FILE}. "
            "Файлро барқарор кунед; базаи холӣ сабт карда намешавад."
        ) from exc

DB = load_data()

def save_data():
    """Сабти бехатари база тавассути файли муваққатӣ."""
    temp = DATA_FILE.with_suffix(".tmp")
    content = json.dumps(DB, ensure_ascii=False, indent=2)
    with temp.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(DATA_FILE)

def new_order_id():
    number = DB["next_id"]
    DB["next_id"] += 1
    save_data()
    return number

def money(amount):
    return f"{amount} сомонӣ"

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Донат кардан", callback_data="catalog")],
        [InlineKeyboardButton("💰 Ҳамёни ман", callback_data="wallet")],
        [InlineKeyboardButton("👥 Даъвати дӯстон", callback_data="referral")],
        [
            InlineKeyboardButton("📦 Фармоишҳои ман", callback_data="orders"),
            InlineKeyboardButton("👤 Профил", callback_data="profile")
        ],
        [
            InlineKeyboardButton("👨‍💻 Админ", callback_data="admin"),
            InlineKeyboardButton("ℹ️ Маълумот", callback_data="info")
        ],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Менюи асосӣ", callback_data="home")]
    ])

def product_keyboard(items, prefix):
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:{i}")]
        for i, (label, _, _) in enumerate(items)
    ]
    rows += [
        [InlineKeyboardButton("⬅️ Категорияҳо", callback_data="catalog")],
        [InlineKeyboardButton("🏠 Менюи асосӣ", callback_data="home")]
    ]
    return InlineKeyboardMarkup(rows)

def get_order(order_id):
    return next(
        (o for o in DB["orders"] if o["id"] == order_id),
        None
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # REFERRAL_SYSTEM_V1
    user_id = str(update.effective_user.id)
    refs = DB.setdefault("referrals", {})
    refs.setdefault("seen_users", [])
    refs.setdefault("invited_by", {})

    is_new_user = user_id not in refs["seen_users"]

    if is_new_user:
        refs["seen_users"].append(user_id)
        args = context.args or []

        if args and args[0].startswith("ref_"):
            inviter_id = args[0][4:]

            if (
                inviter_id.isdigit()
                and inviter_id != user_id
                and user_id not in refs["invited_by"]
            ):
                refs["invited_by"][user_id] = inviter_id

                # 50 дирам = 0.50 сомонӣ
                # Балансро бо дирам нигоҳ медорем, то хатои float нашавад.
                refs.setdefault("earned_diram", {})
                refs["earned_diram"][inviter_id] = (
                    int(refs["earned_diram"].get(inviter_id, 0)) + 50
                )

                # Барои ҳамёни мавҷуда 0.50 сомонӣ илова мешавад.
                wallets = DB.setdefault("wallets", {})
                old_balance = float(wallets.get(inviter_id, 0))
                wallets[inviter_id] = old_balance + 0.50

                try:
                    await context.bot.send_message(
                        chat_id=int(inviter_id),
                        text=(
                            "🎉 Дӯсти нав тавассути пайванди шумо ворид шуд!\n\n"
                            "🎁 Мукофот: 50 дирам (0,50 сомонӣ)\n"
                            "💰 Мукофот ба ҳамёни шумо илова шуд."
                        )
                    )
                except Exception:
                    logging.exception("Referral notification failed")

        save_data()

    context.user_data.clear()
    await update.message.reply_text(
        "🔥 DANATI_PATSANI\n\n"
        "🎮 Хуш омадед ба мағозаи донати Free Fire!\n\n"
        "💎 Алмазҳо\n"
        "🎁 Ваучерҳо\n"
        "🏆 Level Up\n\n"
        "📌 Маҳсулотро интихоб кунед. UID-ро дуруст нависед.\n"
        ""
        "⚠️ Фармоиш пас аз санҷиши пардохт аз ҷониби админ иҷро мешавад.",
        reply_markup=main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Барои оғоз /start-ро пахш кунед.\n"
        "Барои фармоиш аввал маҳсулотро интихоб намоед.",
        reply_markup=main_keyboard()
    )

async def _buttons_original(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except (TimedOut, NetworkError) as exc:
        logging.warning("Ҷавоби тугма аз сабаби шабака дер шуд: %s", type(exc).__name__)
    data = q.data or ""
    user = q.from_user

    if data == "home":
        context.user_data.clear()
        await q.edit_message_text(
            "🏠 Менюи асосӣ\n\nХизматрасониро интихоб кунед:",
            reply_markup=main_keyboard()
        )
        return

    if data == "catalog":
        await q.edit_message_text(
            "💎 ДОНАТ КАРДАН\n\nКатегорияро интихоб кунед:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Алмазҳо", callback_data="diamonds")],
                [InlineKeyboardButton("🎁 Ваучерҳо", callback_data="vouchers")],
                [InlineKeyboardButton("🏆 Level Up", callback_data="levels")],
                [InlineKeyboardButton("🏠 Менюи асосӣ", callback_data="home")]
            ])
        )
        return

    if data in ("diamonds", "vouchers", "levels"):
        mapping = {
            "diamonds": (DIAMONDS, "💎 НАРХНОМАИ АЛМАЗҲО", "diamond"),
            "vouchers": (VOUCHERS, "🎁 ВАУЧЕРҲО", "voucher"),
            "levels": (LEVELS, "🏆 LEVEL UP", "level"),
        }
        items, title, prefix = mapping[data]
        await q.edit_message_text(
            f"{title}\n\nМаҳсулоти дилхоҳро интихоб кунед:",
            reply_markup=product_keyboard(items, prefix)
        )
        return

    if ":" in data and data.split(":", 1)[0] in ("diamond", "voucher", "level"):
        prefix, idx = data.split(":", 1)
        items = {
            "diamond": DIAMONDS,
            "voucher": VOUCHERS,
            "level": LEVELS
        }[prefix]
        try:
            label, product_name, price = items[int(idx)]
        except (ValueError, IndexError):
            await q.edit_message_text(
                "❌ Ин маҳсулот ёфт нашуд.",
                reply_markup=back_keyboard()
            )
            return

        context.user_data["product"] = {
            "name": product_name,
            "price": price,
            "label": label
        }
        context.user_data["waiting_for"] = "uid"
        if q.message:
            context.user_data["uid_prompt_message_id"] = q.message.message_id
        await q.edit_message_text(
            f"✅ Интихоби шумо:\n{label}\n\n"
            "🆔 UID-и Free Fire-и худро фиристед.\n\n"
            "UID бояд 8–12 рақам бошад.\n"
            "Лутфан UID-и худро дуруст санҷед.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Бекор кардан", callback_data="cancel")],
                [InlineKeyboardButton("🏠 Меню", callback_data="home")]
            ])
        )
        return

    if data == "cancel":
        context.user_data.clear()
        await q.edit_message_text(
            "🏠 Менюи асосӣ\n\nФармоиш бекор шуд. Хизматрасониро интихоб кунед:",
            reply_markup=main_keyboard()
        )
        return

    if data == "order_confirm":
        product = context.user_data.get("product")
        uid = context.user_data.get("uid")
        if not product or not uid:
            await q.edit_message_text(
                "❌ Маълумоти фармоиш гум шудааст. Аз нав оғоз кунед.",
                reply_markup=main_keyboard()
            )
            return

        order_id = new_order_id()
        order = {
            "id": order_id,
            "user_id": user.id,
            "username": user.username or "",
            "full_name": user.full_name,
            "product": product["name"],
            "price": product["price"],
            "uid": uid,
            "payment": "",
            "status": "Интизорӣ барои пардохт",
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "receipt": False
        }
        DB["orders"].append(order)
        save_data()
        context.user_data["order_id"] = order_id

        await q.edit_message_text(
            f"🛒 ФАРМОИШИ ШУМО\n\n"
            f"🔢 Рақами фармоиш: DP-{order_id}\n"
            f"🎮 Маҳсулот: {product['name']}\n"
            f"💰 Маблағ: {money(product['price'])}\n"
            f"🆔 UID: {uid}\n\n"
            "🏦 БА КАДОМ КОРТ МЕХОҲЕД ПАРДОХТ КУНЕД?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏦 Душанбе Сити", callback_data="pay:dcity")],
                [InlineKeyboardButton("💳 Алиф", callback_data="pay:alif")],
                [InlineKeyboardButton("💰 Аз ҳамён пардохт кардан", callback_data="pay:wallet")],
                [InlineKeyboardButton("❌ Бекор кардан", callback_data="cancel")]
            ])
        )
        return

    if data.startswith("pay:"):
        method_key = data.split(":", 1)[1]
        method = "Душанбе Сити" if method_key == "dcity" else "Алиф"
        order_id = context.user_data.get("order_id")
        order = get_order(order_id) if order_id else None
        if not order or order["user_id"] != user.id:
            await q.edit_message_text(
                "❌ Фармоиш ёфт нашуд. Аз /start аз нав оғоз кунед.",
                reply_markup=main_keyboard()
            )
            return

        order["payment"] = method
        order["status"] = "Интизори расиди пардохт"
        save_data()
        context.user_data["waiting_for"] = "receipt"
        context.user_data["order_id"] = order_id

        if method_key == "dcity":
            payment_text = (
                "🏦 ПАРДОХТ — ДУШАНБЕ СИТИ\n\n"
                f"🔢 Фармоиш: DP-{order_id}\n"
                f"💰 Маблағи дақиқ: {money(order['price'])}\n\n"
                f"💳 Рақами корт: {DC_CARD}\n"
                f"👤 Соҳиби корт: {DC_OWNER}\n"
                f"📱 Телефон: {DC_PHONE}\n\n"
                "1️⃣ Маблағи нишон додашударо пардохт кунед.\n"
                "2️⃣ Акси расидро дар ҳамин чат фиристед.\n\n"
                "⏳ Кӯшиш кунед, ки пардохтро дар давоми 10 дақиқа анҷом диҳед.\n"
                "⚠️ Расид танҳо баъди санҷиши админ тасдиқ мешавад."
            )
        else:
            payment_text = (
                "💳 ПАРДОХТ — АЛИФ\n\n"
                f"🔢 Фармоиш: DP-{order_id}\n"
                f"💰 Маблағи дақиқ: {money(order['price'])}\n\n"
                "Реквизитҳои Алифро аз админ гиред.\n"
                "Баъд аз пардохт акси расидро дар ҳамин чат фиристед.\n\n"
                "⚠️ Расид танҳо баъди санҷиши админ тасдиқ мешавад."
            )

        await q.edit_message_text(
            payment_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Бекор кардани фармоиш", callback_data="cancel")],
                [InlineKeyboardButton("🏠 Менюи асосӣ", callback_data="home")]
            ])
        )
        return

    if data.startswith("admin:"):
        if user.id != ADMIN_ID:
            await q.answer("Ин тугма танҳо барои админ аст.", show_alert=True)
            return

        parts = data.split(":", 2)
        if len(parts) != 3:
            await q.answer("Дархости нодуруст аст.", show_alert=True)
            return

        _, action, order_id_text = parts
        try:
            order_id = int(order_id_text)
        except ValueError:
            await q.answer("Рақами фармоиш нодуруст аст.", show_alert=True)
            return

        order = get_order(order_id)
        if not order:
            await q.answer("Фармоиш ёфт нашуд.", show_alert=True)
            return

        if action == "complete":
            if order.get("completed"):
                await q.answer("Ин фармоиш аллакай анҷом ёфтааст.", show_alert=True)
                return

            if not (
                order.get("wallet_charged")
                or order.get("status") == "Пардохт тасдиқ шуд — интизори иҷро"
                or order.get("status") == "Пардохт аз ҳамён тасдиқ шуд — интизори иҷро"
            ):
                await q.answer(
                    "Аввал пардохтро тасдиқ кунед.",
                    show_alert=True
                )
                return

            order["completed"] = True
            order["completed_at"] = datetime.now().strftime("%d.%m.%Y %H:%M")
            order["status"] = "Анҷом ёфт"
            save_data()

            try:
                card = create_completion_card(order)
                await context.bot.send_photo(
                    chat_id=order["user_id"],
                    photo=card,
                    caption=(
                        "✅ Фармоиши шумо анҷом ёфт!\n"
                        f"🔢 Фармоиш: DP-{order_id}\n"
                        f"🎮 Маҳсулот: {order.get('product', '—')}\n"
                        f"🆔 UID: {order.get('uid', '—')}\n"
                        f"💰 Маблағ: {money(order.get('price', 0))}"
                    )
                )
            except Exception:
                # Агар фиристодан ноком шавад, админ метавонад аз нав кӯшиш кунад.
                order["completed"] = False
                order.pop("completed_at", None)
                order["status"] = (
                    "Пардохт аз ҳамён тасдиқ шуд — интизори иҷро"
                    if order.get("wallet_charged")
                    else "Пардохт тасдиқ шуд — интизори иҷро"
                )
                save_data()
                logging.exception("Could not send completion card")
                await q.answer(
                    "Корти расмӣ фиристода нашуд. Хатогиро санҷед ва дубора кӯшиш кунед.",
                    show_alert=True
                )
                return

            await q.answer("Фармоиш анҷом ёфт.")
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        if action == "approve":
            order["status"] = "Пардохт тасдиқ шуд — интизори иҷро"
            msg = (
                f"✅ Пардохти фармоиши DP-{order_id} тасдиқ шуд.\n"
                "Пас аз фиристодани алмос тугмаи анҷомро пахш кунед."
            )
        elif action == "reject":
            order["status"] = "Пардохт рад шуд"
            msg = (
                f"❌ Расиди фармоиши DP-{order_id} рад шуд.\n"
                "Лутфан бо админ тамос гиред."
            )
        else:
            await q.answer("Амали номаълум.", show_alert=True)
            return

        save_data()
        # Паёми такрории тасдиқро ба муштарӣ намефиристем.
        # Ҳолати фармоиш дар база нигоҳ дошта мешавад.

        try:
            caption = q.message.caption or ""
            if action == "approve":
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "💎 Алмос фиристода шуд",
                        callback_data=f"admin:complete:{order_id}"
                    )
                ]])
                suffix = "\n\n✅ Пардохт тасдиқ шуд. Баъди фиристодани алмос тугмаи анҷомро пахш кунед."
            else:
                keyboard = None
                suffix = "\n\n❌ Пардохт рад шуд."

            await q.edit_message_caption(
                caption=caption + suffix,
                reply_markup=keyboard
            )
        except Exception:
            try:
                if action == "approve":
                    await q.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "💎 Алмос фиристода шуд",
                                callback_data=f"admin:complete:{order_id}"
                            )
                        ]])
                    )
                else:
                    await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

        await q.answer("Ҳолати фармоиш нав шуд.")
        return

    if data == "orders":
        orders = [
            o for o in DB["orders"]
            if o["user_id"] == user.id
        ][-10:]
        if not orders:
            await q.edit_message_text(
                "📦 Шумо ҳоло фармоиш надоред.",
                reply_markup=back_keyboard()
            )
            return

        lines = ["📦 ФАРМОИШҲОИ ОХИРИНИ ШУМО\n"]
        for o in reversed(orders):
            lines.append(
                f"🔢 DP-{o['id']}\n"
                f"🎮 {o['product']} — {money(o['price'])}\n"
                f"🆔 UID: {o['uid']}\n"
                f"📌 Ҳолат: {o['status']}\n"
            )
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=back_keyboard()
        )
        return

    if data == "profile":
        username = f"@{user.username}" if user.username else "надорад"
        count = sum(1 for o in DB["orders"] if o["user_id"] == user.id)
        await q.edit_message_text(
            f"👤 ПРОФИЛ\n\n"
            f"Ном: {user.full_name}\n"
            f"Username: {username}\n"
            f"Telegram ID: {user.id}\n"
            f"📦 Шумораи фармоишҳо: {count}",
            reply_markup=back_keyboard()
        )
        return

    if data == "admin":
        text = (
            f"👨‍💻 Админ: @{ADMIN_USERNAME}"
            if ADMIN_USERNAME
            else "👨‍💻 Username-и админ дар bot.env насб нашудааст."
        )
        await q.edit_message_text(text, reply_markup=back_keyboard())
        return

    if data == "info":
        await q.edit_message_text(
            "ℹ️ МАЪЛУМОТ\n\n"
            "• Маҳсулотро интихоб кунед.\n"
            "• UID-и худро бодиққат ворид кунед.\n"
            "• Пардохт кунед ва расидро фиристед.\n"
            "• Админ пардохтро месанҷад.\n\n"
            "⚠️ Донат худкор нест. Пас аз тасдиқи пардохт фармоиш аз ҷониби админ баррасӣ мешавад.\n"
            "Лутфан маълумоти фармоишро дуруст ворид кунед.",
            reply_markup=back_keyboard()
        )
        return

    await q.edit_message_text(
        "Ин интихоб шинохта нашуд. Аз меню аз нав оғоз кунед.",
        reply_markup=main_keyboard()
    )

async def _messages_original(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    if context.user_data.get("waiting_for") != "uid":
        await update.message.reply_text(
            "Барои кушодани меню /start-ро пахш кунед.",
            reply_markup=main_keyboard()
        )
        return

    uid = update.message.text.strip()

    if not uid.isdigit() or not 8 <= len(uid) <= 12:
        await update.message.reply_text(
            "❌ UID бояд аз 8 то 12 рақам бошад. Лутфан аз нав фиристед."
        )
        return

    product = context.user_data.get("product")
    if not product:
        context.user_data.clear()
        await update.message.reply_text(
            "❌ Маҳсулот ёфт нашуд. Аз /start аз нав оғоз кунед.",
            reply_markup=main_keyboard()
        )
        return

    context.user_data["uid"] = uid
    context.user_data["waiting_for"] = None

    text = (
        "🛒 ТАСДИҚИ ФАРМОИШ\n\n"
        f"🎮 Маҳсулот: {product['name']}\n"
        f"🆔 UID: {uid}\n"
        f"💰 Нарх: {money(product['price'])}\n\n"
        "⚠️ Никнейм ба таври автоматӣ санҷида намешавад. "
        "Лутфан UID-и худро дар бозӣ санҷед.\n\n"
        "Агар UID ва маҳсулот дуруст бошанд, фармоишро тасдиқ кунед."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Тасдиқ", callback_data="order_confirm")],
        [InlineKeyboardButton("❌ Бекор кардан", callback_data="cancel")]
    ])

    # Саҳифаи пешинаи ботро таҳрир мекунем.
    chat_id = update.effective_chat.id
    prompt_id = context.user_data.get("uid_prompt_message_id")

    if prompt_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=prompt_id,
                text=text,
                reply_markup=keyboard
            )
        except Exception as exc:
            # Агар таҳрир имконнопазир бошад, паёми нав нишон медиҳем.
            logging.warning(
                "Таҳрири саҳифаи UID ноком шуд: %s",
                type(exc).__name__
            )
            await update.message.reply_text(
                text,
                reply_markup=keyboard
            )
    else:
        await update.message.reply_text(
            text,
            reply_markup=keyboard
        )

    # Барои тоза нигоҳ доштани чат, паёми UID-ро нест карданӣ мешавем.
    try:
        await update.message.delete()
    except Exception as exc:
        logging.debug(
            "Паёми UID нест карда нашуд: %s",
            type(exc).__name__
        )


async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_for") != "receipt":
        await update.message.reply_text(
            "Барои фиристодани расид аввал фармоиш созед ва усули пардохтро интихоб кунед.",
            reply_markup=main_keyboard()
        )
        return

    if not ADMIN_ID:
        await update.message.reply_text(
            "❌ ADMIN_ID дар bot.env дуруст насб нашудааст. Ба админ хабар диҳед."
        )
        return

    order_id = context.user_data.get("order_id")
    order = get_order(order_id) if order_id else None
    user = update.effective_user

    if not order or order["user_id"] != user.id:
        context.user_data.pop("waiting_for", None)
        await update.message.reply_text(
            "❌ Фармоиш ёфт нашуд. Аз /start аз нав оғоз кунед.",
            reply_markup=main_keyboard()
        )
        return

    photo_id = update.message.photo[-1].file_id
    caption = (
        f"🧾 РАСИД — БАРОИ САНҶИШ\n\n"
        f"🔢 Фармоиш: DP-{order['id']}\n"
        f"🎮 Маҳсулот: {order['product']}\n"
        f"💰 Маблағ: {money(order['price'])}\n"
        f"🆔 UID: {order['uid']}\n"
        f"💳 Усул: {order['payment']}\n"
        f"👤 Корбар: {user.full_name}\n"
        f"Username: @{user.username if user.username else 'надорад'}\n"
        f"Telegram ID: {user.id}\n\n"
        "⚠️ Пеш аз тасдиқ пардохтро воқеан санҷед."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Тасдиқ", callback_data=f"admin:approve:{order['id']}"
            ),
            InlineKeyboardButton(
                "❌ Рад кардан", callback_data=f"admin:reject:{order['id']}"
            )
        ]
    ])

    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=photo_id,
            caption=caption,
            reply_markup=keyboard
        )
    except Exception:
        logging.exception("Could not send receipt to admin")
        await update.message.reply_text(
            "❌ Расид фиристода нашуд. Баъдтар кӯшиш кунед."
        )
        return

    order["receipt"] = True
    order["status"] = "Расид фиристода шуд — интизори санҷиш"
    save_data()
    context.user_data.pop("waiting_for", None)

    await update.message.reply_text(
        f"✅ Расиди фармоиши DP-{order['id']} ба админ фиристода шуд.\n\n"
        "📌 Пас аз санҷиши пардохт админ ба шумо хабар медиҳад.",
        reply_markup=main_keyboard()
    )






def create_completion_card(order):
    from io import BytesIO
    from datetime import datetime
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1080, 1500
    BLACK = "#111111"
    PANEL = "#1b1b1f"
    ORANGE = "#ff8a24"
    ORANGE_DARK = "#4a260e"
    WHITE = "#f7f7f7"
    GREY = "#a7a7b0"
    GREEN = "#29d878"

    img = Image.new("RGB", (W, H), BLACK)
    d = ImageDraw.Draw(img)

    # Заминаи торик бо нурҳои нозук
    d.ellipse((-300, -300, 500, 430), fill="#21160e")
    d.ellipse((700, 1150, 1370, 1770), fill="#25170d")

    d.rounded_rectangle(
        (24, 24, W-24, H-24), radius=42,
        outline=ORANGE, width=4
    )
    d.rounded_rectangle(
        (38, 38, W-38, H-38), radius=34,
        outline="#593016", width=2
    )

    regular_paths = [
        "/system/fonts/NotoSans-Regular.ttf",
        "/system/fonts/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]
    bold_paths = [
        "/system/fonts/NotoSans-Bold.ttf",
        "/system/fonts/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]

    def font(paths, size):
        for p in paths:
            try:
                return ImageFont.truetype(p, size)
            except (OSError, IOError):
                pass
        return ImageFont.load_default()

    f_logo = font(bold_paths, 43)
    f_title = font(bold_paths, 36)
    f_product = font(bold_paths, 55)
    f_label = font(regular_paths, 23)
    f_value = font(bold_paths, 25)
    f_small = font(regular_paths, 19)
    f_status = font(bold_paths, 30)

    def val(*keys, default="—"):
        for key in keys:
            v = order.get(key)
            if v is not None and str(v).strip():
                return v
        return default

    def short(text, f, max_width):
        text = str(text)
        if d.textbbox((0, 0), text, font=f)[2] <= max_width:
            return text
        while len(text) > 1:
            text = text[:-1]
            candidate = text + "…"
            if d.textbbox((0, 0), candidate, font=f)[2] <= max_width:
                return candidate
        return "…"

    def centered(text, y, f, fill):
        box = d.textbbox((0, 0), text, font=f)
        x = (W - (box[2] - box[0])) // 2
        d.text((x, y), text, font=f, fill=fill)

    # Номи бренд ва логотип
    d.rounded_rectangle(
        (W//2-75, 65, W//2+75, 215),
        radius=34, fill="#090909", outline=ORANGE, width=4
    )
    d.rounded_rectangle(
        (W//2-52, 88, W//2+52, 192),
        radius=24, outline="#8a4b1b", width=2
    )

    # Алмоси марказӣ
    cx, cy = W//2, 141
    d.polygon([
        (cx-38, cy-9), (cx-22, cy-31),
        (cx+22, cy-31), (cx+38, cy-9),
        (cx, cy+35)
    ], fill=ORANGE, outline=WHITE)
    d.line([(cx-22, cy-31), (cx, cy+35),
            (cx+22, cy-31)], fill=WHITE, width=2)
    d.line([(cx-38, cy-9), (cx+38, cy-9)],
           fill=WHITE, width=2)

    centered("DANATI_PATSANI", 230, f_logo, ORANGE)
    centered("FREE FIRE • DIAMOND SERVICE", 283, f_small, GREY)

    # Аломати сабзи анҷом
    d.ellipse((W//2-47, 325, W//2+47, 419),
              fill=GREEN)
    d.line([
        (W//2-23, 370), (W//2-6, 388),
        (W//2+27, 351)
    ], fill=WHITE, width=9)

    centered("Фармоиш бомуваффақият иҷро шуд",
             440, f_title, WHITE)

    # Номи маҳсулот: дастгирии маҳсулоти матнӣ ё dict
    product = val("product", "product_name", "name", default="Free Fire")
    if isinstance(product, dict):
        product = product.get("name", product.get("title", "Free Fire"))

    product_name = str(product)
    diamonds = val("diamonds", "diamond_amount", default="")
    if diamonds and str(diamonds) not in product_name:
        # Агар ном танҳо рақам ё номи кӯтоҳ бошад, онро вайрон накун
        if str(diamonds).isdigit():
            product_name = product_name + " • " + str(diamonds) + " алмаз"

    # Қуттии маҳсулот
    d.rounded_rectangle(
        (58, 505, W-58, 685), radius=26,
        fill="#17130f", outline=ORANGE, width=3
    )
    centered(
        short(product_name, f_product, 900),
        548, f_product, ORANGE
    )
    centered("Маҳсулот ба аккаунти шумо илова шуд",
             625, f_label, GREY)

    # Сарлавҳаи маълумот
    d.text((78, 718), "МАЪЛУМОТИ ФАРМОИШ",
           font=f_status, fill=ORANGE)

    order_id = val("order_id", "id", default="—")
    if str(order_id).isdigit():
        order_number = "SH-DONAT TJ #" + str(order_id)
    else:
        order_number = str(order_id)

    uid = val("uid", "player_uid", "game_uid", "selected_uid")
    nickname = val("nickname", "player_name", "ff_nickname")
    payment = val("payment_method", "payment", "method")
    price = val("price", "total", "amount", default="—")

    if isinstance(price, (int, float)):
        price = f"{price:g} сомонӣ"
    elif str(price) != "—" and not any(
        x in str(price).lower() for x in ("сом", "смн", "tjs")
    ):
        price = str(price) + " сомонӣ"

    # Сана ва вақт
    date_value = val("completed_at", "created_at", "date", default="")
    date_text = "—"
    if date_value:
        try:
            if isinstance(date_value, (int, float)):
                dt = datetime.fromtimestamp(date_value)
            else:
                dt = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
            date_text = dt.strftime("%d.%m.%Y • %H:%M")
        except (ValueError, TypeError, OSError):
            date_text = str(date_value)
    else:
        date_text = datetime.now().strftime("%d.%m.%Y • %H:%M")

    rows = [
        ("Фармоиш №", order_number),
        ("ID аккаунт", uid),
        ("Ном", nickname),
        ("Усули пардохт", payment),
        ("Сана", date_text),
        ("Маблағ", price),
    ]

    # Як панели ягона, мисли намуна
    top, bottom = 760, 1265
    d.rounded_rectangle(
        (58, top, W-58, bottom), radius=28,
        fill=PANEL, outline="#37373d", width=2
    )

    row_h = (bottom - top) // len(rows)

    for i, (label, value) in enumerate(rows):
        y = top + i * row_h

        if i:
            d.line(
                (85, y, W-85, y),
                fill="#35353a", width=1
            )

        # Нишонаи норанҷӣ
        d.ellipse((82, y+row_h//2-5, 92, y+row_h//2+5),
                  fill=ORANGE)

        d.text((112, y+row_h//2-17),
               label, font=f_label, fill=GREY)

        value = short(value, f_value, 520)
        bbox = d.textbbox((0, 0), value, font=f_value)
        value_width = bbox[2] - bbox[0]
        d.text((W-88-value_width, y+row_h//2-18),
               value, font=f_value,
               fill=ORANGE if i in (0, 5) else WHITE)

    # Навори тасдиқ
    d.rounded_rectangle(
        (58, 1290, W-58, 1370), radius=23,
        fill="#211509", outline=ORANGE, width=3
    )
    d.ellipse((84, 1308, 134, 1358),
              outline=ORANGE, width=3)
    d.line([(96, 1333), (107, 1344), (123, 1322)],
           fill=ORANGE, width=4)
    centered("ПАРДОХТ ТАСДИҚ ШУД",
             1311, f_status, WHITE)

    d.line((80, 1390, W-80, 1390),
           fill="#393939", width=2)
    centered("@DANATI_PATSANI", 1400, f_value, ORANGE)
    centered("Ташаккур барои интихоби шумо!",
             1435, f_small, GREY)

    output = BytesIO()
    img.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = "danati_completion.png"
    return output


def wallet_balance(user_id):
    try:
        value = DB.setdefault("wallets", {}).get(str(user_id), 0)
        return max(0.0, float(value))
    except (TypeError, ValueError):
        logging.exception("Invalid wallet balance for user %s", user_id)
        return 0.0

def wallet_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Пур кардани ҳамён", callback_data="wallet_add")],
        [InlineKeyboardButton("🏠 Менюи асосӣ", callback_data="home")]
    ])

async def wallet_buttons(update, context):
    q = update.callback_query
    if q is None:
        return await _buttons_original(update, context)

    data = q.data or ""

    if data == "referral":
        user_id = str(q.from_user.id)
        refs = DB.setdefault("referrals", {})
        refs.setdefault("invited_by", {})
        refs.setdefault("earned_diram", {})

        bot_info = await context.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

        invited = sum(
            1 for inviter in refs["invited_by"].values()
            if str(inviter) == user_id
        )

        earned_diram = int(refs["earned_diram"].get(user_id, 0))

        await q.message.reply_text(
            "👥 ДАЪВАТИ ДӮСТОН\n\n"
            "🎁 Барои ҳар дӯсти нави даъватшуда: 50 дирам\n"
            f"👤 Дӯстони даъватшуда: {invited}\n"
            f"💰 Мукофотҳои ҷамъшуда: {earned_diram} дирам "
            f"({earned_diram / 100:.2f} сомонӣ)\n\n"
            "🔗 Пайванди шахсии шумо:\n"
            f"{link}\n\n"
            "📨 Ин пайвандро ба дӯстони худ фиристед!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Менюи асосӣ", callback_data="home")]
            ]),
            disable_web_page_preview=True
        )
        return

    user = q.from_user

    if data == "wallet":
        await q.answer()
        await q.edit_message_text(
            f"💰 ҲАМЁНИ МАН\n\nБаланс: {money(wallet_balance(user.id))}\n\n"
            "Пул танҳо баъди тасдиқи пардохт аз ҷониби админ ба баланс илова мешавад.",
            reply_markup=wallet_keyboard()
        )
        return

    if data == "wallet_add":
        await q.answer()
        await q.edit_message_text(
            "➕ ПУР КАРДАНИ ҲАМЁН\\n\\n"
            "Усули пардохтро интихоб кунед:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏦 Душанбе Сити", callback_data="wallet_method:dc")],
                [InlineKeyboardButton("⚠️ Alif ҳоло кор намекунад", callback_data="wallet_alif_unavailable")],
                [InlineKeyboardButton("❌ Бекор кардан", callback_data="cancel")],
                [InlineKeyboardButton("🏠 Меню", callback_data="home")]
            ])
        )
        return

    if data == "wallet_alif_unavailable":
        await q.answer(
            "⚠️ Alif ҳоло муваққатан кор намекунад. Танҳо Душанбе Сити дастрас аст.",
            show_alert=True
        )
        return

    if data.startswith("wallet_method:"):
        method = data.split(":", 1)[1]
        if method != "dc":
            await q.answer("Усули пардохт нодуруст аст.", show_alert=True)
            return

        context.user_data["wallet_payment_method"] = method
        context.user_data["waiting_for"] = "wallet_amount"
        await q.answer()
        await q.edit_message_text(
            "💰 Маблағи пуркунии ҳамёнро бо сомонӣ фиристед.\\n"
            "Мисол: 50",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Бекор кардан", callback_data="cancel")],
                [InlineKeyboardButton("🏠 Меню", callback_data="home")]
            ])
        )
        return

    if data.startswith("admin_wallet:"):
        if user.id != ADMIN_ID:
            await q.answer("Ин тугма танҳо барои админ аст.", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("approve", "reject"):
            await q.answer("Дархости нодуруст.", show_alert=True)
            return
        action, topup_id = parts[1], parts[2]
        topup = next((x for x in DB.get("topups", [])
                      if str(x.get("id")) == topup_id), None)
        if not topup:
            await q.answer("Дархост ёфт нашуд.", show_alert=True)
            return
        if topup.get("status") != "pending":
            await q.answer("Ин дархост қаблан баррасӣ шудааст.", show_alert=True)
            return

        if action == "approve":
            wallets = DB.setdefault("wallets", {})
            key = str(topup["user_id"])
            wallets[key] = wallet_balance(topup["user_id"]) + int(topup["amount"])
            topup["status"] = "approved"
            notice = (
                f"✅ Пур кардани ҳамён тасдиқ шуд.\n"
                f"➕ Илова шуд: {money(topup['amount'])}\n"
                f"💰 Баланси нав: {money(wallet_balance(topup['user_id']))}"
            )
            result = "✅ Пур кардани ҳамён тасдиқ шуд."
        else:
            topup["status"] = "rejected"
            notice = "❌ Дархости пур кардани ҳамён рад шуд. Барои маълумот бо админ тамос гиред."
            result = "❌ Дархост рад шуд."

        save_data()
        await q.answer(result)
        try:
            await context.bot.send_message(chat_id=topup["user_id"], text=notice)
        except Exception:
            logging.exception("Wallet notification failed")
        try:
            await q.edit_message_caption(
                caption=(q.message.caption or "") + "\n\n" + result,
                reply_markup=None
            )
        except Exception:
            pass
        return

    if data == "pay:wallet":
        await q.answer()
        order_id = context.user_data.get("order_id")
        order = get_order(order_id) if order_id else None
        if not order or order.get("user_id") != user.id:
            await q.edit_message_text(
                "❌ Фармоиш ёфт нашуд. Аз нав оғоз кунед.",
                reply_markup=main_keyboard()
            )
            return
        if order.get("wallet_charged"):
            await q.edit_message_text(
                "✅ Ин фармоиш аллакай аз ҳамён пардохт шудааст.",
                reply_markup=main_keyboard()
            )
            return
        price = int(order.get("price", 0))
        balance = wallet_balance(user.id)
        if price <= 0 or balance < price:
            await q.edit_message_text(
                f"❌ Баланс кофӣ нест.\n\n"
                f"💰 Баланс: {money(balance)}\n"
                f"🛒 Нарх: {money(price)}\n\n"
                "Аввал ҳамёнро пур кунед ё усули дигари пардохтро интихоб намоед.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Пур кардани ҳамён", callback_data="wallet_add")],
                    [InlineKeyboardButton("🏠 Меню", callback_data="home")]
                ])
            )
            return

        # Тарҳи баланс ва сабти фармоиш дар як амалиёт захира мешаванд.
        DB["wallets"][str(user.id)] = balance - price
        order["wallet_charged"] = True
        order["payment"] = "Ҳамён"
        order["status"] = "Пардохт аз ҳамён тасдиқ шуд — интизори иҷро"
        save_data()

        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"🛒 ФАРМОИШИ НАВ DP-{order['id']}\n\n"
                        f"🎮 Маҳсулот: {order['product']}\n"
                        f"💰 Пардохт аз ҳамён: {money(price)}\n"
                        f"🆔 UID: {order['uid']}\n"
                        f"👤 Корбар: {order.get('full_name', '')}\n"
                        f"Telegram ID: {user.id}\n\n"
                        "Пардохт аз ҳамён сабт шуд. Фармоишро барои иҷро баррасӣ кунед."
                    )
                )
            except Exception:
                logging.exception("Could not notify admin about wallet order")

        await q.edit_message_text(
            f"✅ Пардохт аз ҳамён сабт шуд!\n\n"
            f"🔢 Фармоиш: DP-{order['id']}\n"
            f"🎮 Маҳсулот: {order['product']}\n"
            f"💰 Пардохт: {money(price)}\n"
            f"💵 Баланси боқимонда: {money(wallet_balance(user.id))}\n\n"
            "Фармоиш ҳоло интизори баррасии админ аст.",
            reply_markup=main_keyboard()
        )
        return

    return await _buttons_original(update, context)


async def wallet_messages(update, context):
    message = update.effective_message
    if not message or not message.text:
        return await _messages_original(update, context)

    if context.user_data.get("waiting_for") != "wallet_amount":
        return await _messages_original(update, context)

    raw = message.text.strip()
    if not raw.isdigit():
        await message.reply_text("❌ Маблағро танҳо бо рақам фиристед, масалан 50.")
        return
    amount = int(raw)
    if amount < 1 or amount > 10000:
        await message.reply_text("❌ Маблағ бояд аз 1 то 10000 сомонӣ бошад.")
        return
    if not ADMIN_ID:
        await message.reply_text("❌ ADMIN_ID насб нашудааст. Ҳоло дархост фиристода намешавад.")
        context.user_data.pop("waiting_for", None)
        return

    topup_id = str(int(time.time() * 1000))
    topup = {
        "id": topup_id,
        "user_id": message.from_user.id,
        "username": message.from_user.username or "",
        "amount": amount,
        "status": "pending",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "receipt": False
    }
    DB.setdefault("topups", []).append(topup)
    save_data()
    context.user_data["wallet_topup_id"] = topup_id
    context.user_data["waiting_for"] = "wallet_receipt"

    method = context.user_data.get("wallet_payment_method", "dc")
    if method == "alif":
        card = os.getenv("ALIF_CARD", "")
        owner = os.getenv("ALIF_OWNER", "")
        phone = os.getenv("ALIF_PHONE", "")
        method_name = "Alif"
    else:
        card = DC_CARD
        owner = DC_OWNER
        phone = DC_PHONE
        method_name = "Душанбе Сити"

    details = []
    if card:
        details.append(f"💳 Корт: {card}")
    if owner:
        details.append(f"👤 Соҳиби корт: {owner}")
    if phone:
        details.append(f"📱 Телефон: {phone}")
    if not details:
        details.append("⚠️ Реквизитҳои ин усул ҳоло танзим нашудаанд.")

    await message.reply_text(
        f"➕ ПУР КАРДАНИ ҲАМЁН\\n\\n"
        f"💰 Маблағ: {money(amount)}\\n"
        f"🏦 Усул: {method_name}\\n"
        + "\\n".join(details)
        + "\\n\\nПас аз пардохти воқеӣ акси расидро дар ҳамин чат фиристед.\\n"
        "Баланс танҳо баъди санҷиши админ зиёд мешавад.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Меню", callback_data="home")]
        ])
    )


async def wallet_receipt_photo(update, context):
    if context.user_data.get("waiting_for") != "wallet_receipt":
        return await receipt_photo(update, context)

    message = update.effective_message
    user = update.effective_user
    topup_id = context.user_data.get("wallet_topup_id")
    topup = next((x for x in DB.get("topups", [])
                  if str(x.get("id")) == str(topup_id)), None)

    if not topup or topup.get("user_id") != user.id or topup.get("status") != "pending":
        context.user_data.pop("waiting_for", None)
        await message.reply_text("❌ Дархост ёфт нашуд. Аз меню аз нав оғоз кунед.")
        return
    if topup.get("receipt"):
        await message.reply_text("⏳ Расид аллакай фиристода шудааст ва интизори санҷиши админ аст.")
        return
    if not message.photo:
        await message.reply_text("❌ Лутфан акси расиди пардохтро фиристед.")
        return

    caption = (
        "🧾 РАСИДИ ПУР КАРДАНИ ҲАМЁН\n\n"
        f"🔢 Дархост: {topup_id}\n"
        f"💰 Маблағ: {money(topup['amount'])}\n"
        f"👤 Корбар: {user.full_name}\n"
        f"Username: @{user.username if user.username else 'надорад'}\n"
        f"Telegram ID: {user.id}\n\n"
        "⚠️ Пеш аз тасдиқ пардохтро воқеан санҷед."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Қабул кардан", callback_data=f"admin_wallet:approve:{topup_id}"),
        InlineKeyboardButton("❌ Рад кардан", callback_data=f"admin_wallet:reject:{topup_id}")
    ]])
    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID, photo=message.photo[-1].file_id,
            caption=caption, reply_markup=keyboard
        )
    except Exception:
        logging.exception("Wallet receipt could not be sent")
        await message.reply_text("❌ Расид ба админ фиристода нашуд. Баъдтар кӯшиш кунед.")
        return

    topup["receipt"] = True
    save_data()
    context.user_data.pop("waiting_for", None)
    await message.reply_text(
        "✅ Расид ба админ фиристода шуд. Баланс баъди тасдиқи пардохт зиёд мешавад.",
        reply_markup=main_keyboard()
    )


async def error_handler(update, context):
    error = context.error
    if isinstance(error, (TimedOut, NetworkError)):
        logging.warning("Хатои муваққатии шабакаи Telegram: %s", type(error).__name__)
    else:
        logging.error(
            "Хатои коркарди навсозӣ",
            exc_info=(type(error), error, error.__traceback__)
        )



async def lookup_ff_nickname(uid):
    """Ҷустуҷӯи номи бозигари Free Fire аз рӯйи UID."""
    import httpx
    import logging

    uid = str(uid).strip()

    if not uid.isdigit() or not 5 <= len(uid) <= 20:
        return None

    regions = [
        "ME", "CIS", "RU", "IND", "SG", "BD",
        "PK", "ID", "BR", "US", "TH", "VN", "TW"
    ]

    urls = [
        "https://freefireapis.lat/info-player",
        "https://api2.nftoken.info/get",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": "DANATI-PATSANI/1.0"},
        ) as client:

            for url in urls:
                for region in regions:
                    try:
                        response = await client.get(
                            url,
                            params={"uid": uid, "region": region},
                        )

                        if response.status_code != 200:
                            continue

                        data = response.json()
                        if not isinstance(data, dict):
                            continue

                        # Ҷавобҳои API-ҳои гуногунро мехонем.
                        result = data.get("result", data)
                        if not isinstance(result, dict):
                            continue

                        info = (
                            result.get("basicInfo")
                            or result.get("basicinfo")
                            or result.get("accountInfo")
                            or result.get("accountinfo")
                            or result
                        )

                        if not isinstance(info, dict):
                            continue

                        name = (
                            info.get("nickname")
                            or info.get("accountName")
                            or info.get("accountname")
                            or info.get("Nickname")
                        )

                        found_uid = (
                            info.get("accountId")
                            or info.get("accountid")
                            or info.get("uid")
                            or info.get("UID")
                        )

                        if not isinstance(name, str) or not name.strip():
                            continue

                        # UID-и дигарро қабул намекунем.
                        if found_uid is not None and str(found_uid) != uid:
                            continue

                        logging.info(
                            "Free Fire UID lookup successful in %s",
                            region,
                        )
                        return name.strip()

                    except (
                        httpx.HTTPError,
                        ValueError,
                        TypeError,
                        AttributeError,
                    ):
                        continue

    except httpx.HTTPError as exc:
        logging.warning(
            "Free Fire lookup connection failed: %s",
            type(exc).__name__,
        )

    return None


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN дар bot.env ёфт нашуд.")
    if not ADMIN_ID:
        logging.warning("ADMIN_ID насб нашудааст; расидҳо ба админ фиристода намешаванд.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(30)
        .pool_timeout(30)
        .connection_pool_size(32)
        .get_updates_connection_pool_size(4)
        .get_updates_pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(wallet_buttons))
    app.add_handler(MessageHandler(filters.PHOTO, wallet_receipt_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_messages))

    app.add_error_handler(error_handler)
    print("DANATI_PATSANI is running...")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(
        poll_interval=0,
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=False,
    )



# UID_LOOKUP_UPDATE_V1
if __name__ == "__main__":
    main()
