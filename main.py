import asyncio
import logging
import os
import json
from decimal import Decimal
from typing import Optional
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
import aiosqlite
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

# ---------- Настройки ----------
API_TOKEN = os.getenv("8143338617:AAGVHijh2I_GCglNF6jqTURW_Gce4aWEOh8")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x)
DATABASE = os.getenv("DATABASE","escrow.db")
WEB3_RPC = os.getenv("WEB3_RPC","https://rpc.ankr.com/eth_goerli")  # пример тест.сети
ESCROW_WALLET_PRIVATE_KEY = os.getenv("ESCROW_PRIV_KEY")  # ОБЯЗАТЕЛЬНО хранить безопасно
ESCROW_WALLET_ADDRESS = os.getenv("ESCROW_ADDRESS")
NFT_CONTRACT_ADDRESS = os.getenv("NFT_CONTRACT_ADDRESS")
NFT_CONTRACT_ABI_PATH = os.getenv("NFT_CONTRACT_ABI","nft_abi.json")

if API_TOKEN is None:
    raise SystemExit("Set TG_BOT_TOKEN in .env")

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Web3 ----------
w3 = Web3(Web3.HTTPProvider(WEB3_RPC))
if not w3.isConnected():
    logger.warning("Web3 not connected. Проверьте WEB3_RPC")

# Загружаем ABI контракта ERC-721/1155 (поместите abi в файл)
with open(NFT_CONTRACT_ABI_PATH, "r", encoding="utf-8") as f:
    NFT_ABI = json.load(f)
nft_contract = w3.eth.contract(address=Web3.toChecksumAddress(NFT_CONTRACT_ADDRESS), abi=NFT_ABI)

# ---------- Бот и FSM ----------
bot = Bot(token=API_TOKEN, parse_mode="Markdown")
dp = Dispatcher(bot, storage=MemoryStorage())

# ---------- Состояния для FSM ----------
class Escrow States(StatesGroup):
    waiting_for_gift_type = State()
    waiting_for_token_id = State()
    waiting_for_recipient = State()
    waiting_for_agree = State()

# ---------- Инициализация БД ----------
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS escrows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                creator_username TEXT,
                recipient TEXT,
                token_id TEXT,
                token_contract TEXT,
                status TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance TEXT DEFAULT '0'
            )
        """)
        await db.commit()

# ---------- Утилитарные клавиатуры ----------
def main_menu(username: Optional[str]=None):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("🎁 Создать подарок"))
    kb.add(KeyboardButton("📦 Мои подарки"))
    kb.add(KeyboardButton("ℹ️ Информация"))
    if username:
        kb.add(KeyboardButton("👤 Профиль"))
    return kb

def escrows_inline(escrow_id: int, status: str):
    kb = InlineKeyboardMarkup()
    if status == "pending":
        kb.add(InlineKeyboardButton("✅ Подтвердить получение (получатель)", callback_data=f"confirm:{escrow_id}"))
        kb.add(InlineKeyboardButton("❌ Отменить (создатель)", callback_data=f"cancel:{escrow_id}"))
        kb.add(InlineKeyboardButton("📜 Данные сделки", callback_data=f"info:{escrow_id}"))
    else:
        kb.add(InlineKeyboardButton("📜 Данные сделки", callback_data=f"info:{escrow_id}"))
    return kb

# ---------- Помощники работы с блокчейном ----------
def to_checksum(addr):
    try:
        return Web3.toChecksumAddress(addr)
    except Exception:
        return None

def transfer_nft(from_address, to_address, token_id, private_key):
    # Простой пример передачи ERC-721 методом safeTransferFrom (упрощённо)
    try:
        nonce = w3.eth.get_transaction_count(to_checksum(from_address))
        tx = nft_contract.functions.safeTransferFrom(
            to_checksum(from_address),
            to_checksum(to_address),
            int(token_id)
        ).build_transaction({
            "nonce": nonce,
            "from": to_checksum(from_address),
            "gas": 200000,
            "gasPrice": w3.toWei('20', 'gwei')
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        return w3.toHex(tx_hash)
    except Exception as e:
        logger.exception("transfer_nft error")
        return None

# ---------- Хэндлеры команд ----------
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    await init_db()
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("INSERT OR REPLACE INTO users (id, username, first_name) VALUES (?, ?, ?)",
                         (message.from_user.id, message.from_user.username or "", message.from_user.first_name or ""))
        await db.commit()
    text = (
        "Привет! Я NFT Гарант бот. Я помогаю безопасно дарить NFT: держу токен в эскроу и переводю получателю\n"
        "— Создавайте сделку\n— Отправляйте NFT на эскроу-адрес\n— Получатель подтверждает получение\n\n"
        "Начните с кнопки Создать подарок"
    )
    await message.answer(text, reply_markup=main_menu(message.from_user.username))

@dp.message_handler(lambda m: m.text == "🎁 Создать подарок")
async def create_gift_start(message: types.Message):
    await message.answer("Выберите тип токена: 1) ERC-721 (единственный NFT) 2) ERC-1155 (токен-кольцо)\nОтправьте 721 или 1155")
    await EscrowStates.waiting_for_gift_type.set()

@dp.message_handler(state=EscrowStates.waiting_for_gift_type)
async def process_gift_type(message: types.Message, state: FSMContext):
    txt = message.text.strip()
    if txt not in ("721","1155"):
        await message.answer("Введите 721 или 1155")
        return
    await state.update_data(gift_type=txt)
    await message.answer("Укажите Token ID (число) NFT, который вы хотите подарить")
    await EscrowStates.waiting_for_token_id.set()

@dp.message_handler(state=EscrowStates.waiting_for_token_id)
async def process_token_id(message: types.Message, state: FSMContext):
    token_id = message.text.strip()
    if not token_id.isdigit():
        await message.answer("Token ID должен быть числом. Попробуйте снова")
        return
    await state.update_data(token_id=token_id)
    await message.answer("Укажите ник получателя или адрес кошелька получателя (Telegram @username или ETH-адрес)")
    await EscrowStates.waiting_for_recipient.set()

@dp.message_handler(state=EscrowStates.waiting_for_recipient)
async def process_recipient(message: types.Message, state: FSMContext):
    data = await state.get_data()
    token_id = data["token_id"]
    gift_type = data["gift_type"]
    recipient = message.text.strip()
    await state.update_data(recipient=recipient)
    summary = f"Подтвердите создание сделки\n\nТип {gift_type}\nToken ID {token_id}\nПолучатель {recipient}\n\nДалее вы должны перевести NFT на эскроу-адрес бота: {ESCROW_WALLET_ADDRESS}\nПосле поступления NFT бот переведёт токен получателю по подтверждению."
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("Создать сделку", callback_data="create_escrow"),
        InlineKeyboardButton("Отменить", callback_data="abort_create")
    )
    await message.answer(summary, reply_markup=kb)
    await EscrowStates.waiting_for_agree.set()

@dp.callback_query_handler(lambda c: c.data == "abort_create", state=EscrowStates.waiting_for_agree)
async def abort_create(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Создание сделки отменено", show_alert=True)
    await state.finish()
    await call.message.edit_reply_markup(None)
    await call.message.answer("Возвращаюсь в меню", reply_markup=main_menu(call.from_user.username))

@dp.callback_query_handler(lambda c: c.data == "create_escrow", state=EscrowStates.waiting_for_agree)
async def create_escrow(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("INSERT INTO escrows (creator_id, creator_username, recipient, token_id, token_contract, status) VALUES (?, ?, ?, ?, ?, ?)",
                         (call.from_user.id, call.from_user.username or "", data["recipient"], data["token_id"], NFT_CONTRACT_ADDRESS, "pending"))
        await db.commit()
        cursor = await db.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        escrow_id = row[0]
    await call.answer("Сделка создана. Ожидаем, когда вы переведёте NFT на эскроу-адрес.", show_alert=True)
    await call.message.edit_reply_markup(None)
    await call.message.answer(f"Сделка #{escrow_id} создана.\nПожалуйста, переведите NFT (Token ID {data['token_id']}) на адрес эскроу:\n`{ESCROW_WALLET_ADDRESS}`\nПосле прихода токена нажмите кнопку Подтвердить получение (получатель) или отмените", parse_mode="Markdown", reply_markup=main_menu(call.from_user.username))
    await state.finish()

@dp.message_handler(lambda m: m.text == "📦 Мои подарки")
async def my_escrows(message: types.Message):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id, token_id, recipient, status, created_at FROM escrows WHERE creator_id = ? ORDER BY id DESC", (message.from_user.id,))
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("У вас нет созданных подарков.", reply_markup=main_menu(message.from_user.username))
        return
    for r in rows:
        eid, token_id, recipient, status, created_at = r
        text = f"ID {eid}\nToken {token_id}\nПолучатель {recipient}\nСтатус {status}\nСоздано {created_at}"
        await message.answer(text, reply_markup=escrows_inline(eid, status))

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("info:"))
async def escrow_info(call: types.CallbackQuery):
    _, sid = call.data.split(":")
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id, creator_id, creator_username, recipient, token_id, status, created_at FROM escrows WHERE id = ?", (sid,))
        row = await cursor.fetchone()
    if not row:
        await call.answer("Сделка не найдена", show_alert=True)
        return
    eid, creator_id, creator_username, recipient, token_id, status, created_at = row
    text = (
        f"Сделка #{eid}\nСоздатель @{creator_username} ({creator_id})\nПолучатель {recipient}\n"
        f"Token ID {token_id}\nСтатус {status}\nСоздана {created_at}\n\n"
        "Важно: пока NFT не поступил на эскроу-адрес, статус остаётся pending."
    )
    await call.answer()
    await call.message.answer(text, reply_markup=escrows_inline(eid, status))

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("cancel:"))
async def cancel_escrow(call: types.CallbackQuery):
    _, sid = call.data.split(":")
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT creator_id, status FROM escrows WHERE id = ?", (sid,))
        row = await cursor.fetchone()
    if not row:
        await call.answer("Сделка не найдена", show_alert=True)
        return
    creator_id, status = row
    if call.from_user.id != creator_id:
        await call.answer("Только создатель может отменить сделку", show_alert=True)
        return
    if status != "pending":
        await call.answer("Эту сделку нельзя отменить", show_alert=True)
        return
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE escrows SET status = ? WHERE id = ?", ("cancelled", sid))
        await db.commit()
    await call.answer("Сделка отменена", show_alert=True)
    await call.message.edit_reply_markup(None)
    await call.message.answer("Сделка отменена. NFT остаётся у вас, если вы ещё не переводили.", reply_markup=main_menu(call.from_user.username))

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("confirm:"))
async def confirm_receipt(call: types.CallbackQuery):
    _, sid = call.data.split(":")
    # получатель нажал подтвердить. Бот проверяет наличие NFT на эскроу и переводит на указанный адрес
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT creator_id, recipient, token_id, status FROM escrows WHERE id = ?", (sid,))
        row = await cursor.fetchone()
    if not row:
        await call.answer("Сделка не найдена", show_alert=True)
        return
    creator_id, recipient, token_id, status = row
    # проверка прав — в этом примере любой может подтвердить, но лучше требовать быть указанным получателем
    if status != "pending":
        await call.answer("Сделка не активна", show_alert=True)
        return
    # проверим, находится ли указанный токен на эскроу-адресе
    try:
        owner = nft_contract.functions.ownerOf(int(token_id)).call()
    except Exception as e:
        owner = None
    if owner is None:
        await call.answer("Не удалось получить владельца токена. Повторите позже", show_alert=True)
        return
    if owner.lower() != ESCROW_WALLET_ADDRESS.lower():
        await call.answer("Токен ещё не на эскроу-адресе. Подтвердить нельзя.", show_alert=True)
        return
    # совершим перевод: от ESCROW_WALLET_ADDRESS к recipient (если recipient — адрес), иначе требуем адрес
    tx_hash = None
    to_addr = None
    if recipient.startswith("@"):
        await call.answer("Получатель указан Telegram ником. Попросите получателя прислать свой ETH-адрес или укажите адрес вручную.", show_alert=True)
        return
    else:
        # ожидаем, что recipient — адрес
        checksum = to_checksum(recipient)
        if not checksum:
            await call.answer("Некорректный адрес получателя. Нужен ETH-адрес.", show_alert=True)
            return
        to_addr = checksum
    # переводим NFT
    await call.answer("Переводим NFT получателю. Подождите...", show_alert=True)
    tx = transfer_nft(ESCROW_WALLET_ADDRESS, to_addr, token_id, ESCROW_WALLET_PRIVATE_KEY)
    if not tx:
        await call.message.answer("Ошибка перевода. Проверьте логи и баланс эскроу-кошелька.", reply_markup=main_menu(call.from_user.username))
        return
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE escrows SET status = ? WHERE id = ?", ("completed", sid))
        await db.commit()
    await call.message.answer(f"Готово! NFT передан. TxHash: `{tx}`", parse_mode="Markdown", reply_markup=main_menu(call.from_user.username))

@dp.message_handler(lambda m: m.text == "ℹ️ Информация")
async def info_cmd(message: types.Message):
    text = (
        "Как работает бот гаранта\n"
        "1) Создатель создаёт сделку и переводит NFT на адрес эскроу\n"
        "2) Как только NFT окажется на адресе эскроу, получатель подтверждает получение\n"
        "3) Бот переводит NFT получателю\n\n"
        "Безопасность\n"
        "- Эскроу-кошелёк контролирует бот. В production используйте мульти-сиг или смарт-контракт эскроу.\n"
        "- Все транзакции прозрачны и имеют TxHash."
    )
    await message.answer(text, reply_markup=main_menu(message.from_user.username))

@dp.message_handler(lambda m: m.text == "👤 Профиль")
async def profile_cmd(message: types.Message):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id, username, first_name FROM users WHERE id = ?", (message.from_user.id,))
        row = await cursor.fetchone()
    if not row:
        await message.answer("Профиль не найден.", reply_markup=main_menu(message.from_user.username))
        return
    uid, username, fname = row
    text = f"Профиль\nID {uid}\n@{username}\n{name_or_empty(fname)}"
    await message.answer(text, reply_markup=main_menu(message.from_user.username))

def name_or_empty(s):
    return s if s else ""

# ---------- Админские хэндлеры ----------
@dp.message_handler(commands=["admin"], lambda message: message.from_user.id in ADMIN_IDS)
async def admin_panel(message: types.Message):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("📊 Статистика"))
    kb.add(KeyboardButton("🔙 Назад"))
    await message.answer("Панель администратора", reply_markup=kb)

@dp.message_handler(lambda m: m.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещён")
        return
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM escrows")
        total = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM escrows WHERE status='pending'")
        pending = (await cursor.fetchone())[0]
    await message.answer(f"Всего сделок {total}\nВ ожидании {pending}", reply_markup=main_menu(message.from_user.username))

# ---------- Вспомогательные хэндлеры ----------
@dp.message_handler()
async def fallback(message: types.Message):
    await message.answer("Выберите кнопку из меню или /start", reply_markup=main_menu(message.from_user.username))

# ---------- Запуск ----------
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(init_db())
    from aiogram import executor
    executor.start_polling(dp, skip_updates=True)