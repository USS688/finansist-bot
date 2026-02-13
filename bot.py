import os
import json
import logging
import asyncio
from datetime import datetime
import pytz
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

# --- НАСТРОЙКИ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
MY_CHAT_ID = int(os.environ.get("MY_CHAT_ID", "0"))
TIMEZONE = "Asia/Tashkent"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- GOOGLE SHEETS ---
def get_sheet(sheet_name):
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)

def add_transaction(rows: list):
    sheet = get_sheet("Транзакции")
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz).strftime("%d.%m.%Y %H:%M")
    for row in rows:
        sheet.append_row([
            now,
            row.get("тип", "расход"),
            row.get("сумма", 0),
            row.get("категория", "другое"),
            row.get("описание", "")
        ])

def get_month_stats():
    sheet = get_sheet("Транзакции")
    records = sheet.get_all_records()
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    current_month = now.strftime("%m.%Y")

    total_expense = 0
    total_income = 0
    categories = {}
    debts_given = 0
    debts_received = 0

    for r in records:
        try:
            date_str = str(r.get("Дата", ""))
            if len(date_str) >= 7 and date_str[3:10] == current_month:
                amount = float(str(r.get("Сумма", 0)).replace(" ", "").replace(",", ".") or 0)
                t = str(r.get("Тип", "")).lower()
                cat = str(r.get("Категория", "другое"))
                if t == "расход":
                    total_expense += amount
                    categories[cat] = categories.get(cat, 0) + amount
                elif t == "доход":
                    total_income += amount
                elif t == "долг":
                    if cat == "долг_выдал":
                        debts_given += amount
                    elif cat == "долг_получил":
                        debts_received += amount
        except:
            continue

    return {
        "expense": total_expense,
        "income": total_income,
        "categories": categories,
        "debts_given": debts_given,
        "debts_received": debts_received,
        "month": now.strftime("%m.%Y")
    }

# --- OPENAI ---
def parse_message(text: str) -> list:
    client = OpenAI(api_key=OPENAI_API_KEY)
    system_prompt = """You are a financial message parser. The user writes in Russian or Uzbek.
Extract financial data and return ONLY a JSON array. No explanation, no markdown, no code blocks.

If ONE transaction:
[{"тип":"расход","сумма":15000,"категория":"транспорт","описание":"такси"}]

If MULTIPLE transactions:
[{"тип":"расход","сумма":8000,"категория":"еда","описание":"молоко"},{"тип":"расход","сумма":45000,"категория":"еда","описание":"мясо"}]

Types: расход, доход, долг
Categories: еда, транспорт, коммунальные, одежда, здоровье, развлечения, кафе, долг_выдал, долг_получил, зарплата, другое

Rules:
- spending/purchase = тип "расход"
- salary/income/received = тип "доход"
- lent to someone = тип "долг", категория "долг_выдал"
- borrowed from someone = тип "долг", категория "долг_получил"
- returned debt = тип "долг"

IMPORTANT: Always return a JSON array [...]. Only JSON, nothing else."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ],
        max_tokens=500
    )
    content = response.choices[0].message.content.strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)

# --- ОБРАБОТЧИКИ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MY_CHAT_ID:
        return

    text = update.message.text.strip()

    if text.lower() in ["итоги", "итог", "статистика", "отчёт", "отчет"]:
        await send_stats(update)
        return

    if text.lower() in ["долги", "долг"]:
        await send_debts(update)
        return

    if text.lower() in ["помощь", "help", "/help", "/start"]:
        await send_help(update)
        return

    try:
        await update.message.reply_text("⏳ Записываю...")
        rows = parse_message(text)
        add_transaction(rows)

        if len(rows) == 1:
            r = rows[0]
            emoji = "💸" if r["тип"] == "расход" else "💰" if r["тип"] == "доход" else "🤝"
            msg = f"{emoji} Записано!\n\n{r['описание'].capitalize()} — {int(r['сумма']):,} сум\nКатегория: {r['категория']}"
        else:
            total = sum(int(r["сумма"]) for r in rows)
            lines = "\n".join([f"• {r['описание'].capitalize()} — {int(r['сумма']):,} сум" for r in rows])
            msg = f"✅ Записано {len(rows)} позиций!\n\n{lines}\n\n💰 Итого: {total:,} сум"

        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Не смог разобрать. Попробуй:\nтакси 15000\nили список:\nмолоко 8000\nмясо 45000")

async def send_stats(update: Update):
    try:
        stats = get_month_stats()
        cat_lines = ""
        if stats["categories"]:
            sorted_cats = sorted(stats["categories"].items(), key=lambda x: x[1], reverse=True)
            cat_lines = "\n".join([f"  • {cat}: {int(amount):,} сум" for cat, amount in sorted_cats[:7]])

        balance = stats["income"] - stats["expense"]
        balance_emoji = "📈" if balance >= 0 else "📉"

        msg = f"📊 Итоги за {stats['month']}\n\n"
        msg += f"💰 Доходы: {int(stats['income']):,} сум\n"
        msg += f"💸 Расходы: {int(stats['expense']):,} сум\n"
        msg += f"{balance_emoji} Баланс: {int(balance):,} сум\n\n"
        msg += f"📂 По категориям:\n{cat_lines if cat_lines else 'Нет данных'}"

        if stats["debts_given"] > 0 or stats["debts_received"] > 0:
            msg += f"\n\n🤝 Долги:\nВыдал: {int(stats['debts_given']):,} сум\nПолучил: {int(stats['debts_received']):,} сум"

        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await update.message.reply_text("❌ Не удалось получить статистику.")

async def send_debts(update: Update):
    try:
        sheet = get_sheet("Транзакции")
        records = sheet.get_all_records()
        debts = {}

        for r in records:
            t = str(r.get("Тип", "")).lower()
            cat = str(r.get("Категория", ""))
            desc = str(r.get("Описание", ""))
            try:
                amount = float(str(r.get("Сумма", 0)).replace(" ", "").replace(",", ".") or 0)
            except:
                amount = 0

            if t == "долг" and desc:
                name = desc.strip()
                if name not in debts:
                    debts[name] = 0
                if cat == "долг_выдал":
                    debts[name] += amount
                elif cat == "долг_получил":
                    debts[name] -= amount

        if not debts:
            await update.message.reply_text("🤝 Долгов нет!")
            return

        lines = []
        for name, amount in debts.items():
            if amount > 0:
                lines.append(f"• {name} должен тебе: {int(amount):,} сум")
            elif amount < 0:
                lines.append(f"• Ты должен {name}: {int(abs(amount)):,} сум")

        msg = "🤝 Долги:\n\n" + "\n".join(lines) if lines else "✅ Все долги погашены!"
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Debts error: {e}")
        await update.message.reply_text("❌ Не удалось получить список долгов.")

async def send_help(update: Update):
    msg = """👋 Привет! Я твой финансовый бот.

Как записывать:
• такси 15000 — расход
• зарплата 5000000 — доход
• одолжил Алишеру 100000 — долг
• вернул Темур 50000 — возврат

Список с базара:
молоко 8000
мясо 45000
хлеб 3000

Команды:
• итоги — статистика за месяц
• долги — кто кому должен
• помощь — эта подсказка"""
    await update.message.reply_text(msg)

# --- НАПОМИНАНИЕ ---
async def send_reminder():
    bot = Bot(token=TELEGRAM_TOKEN)
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    msg = f"👋 Привет Стас!\n\nУже {now.strftime('%H:%M')}. Не забудь записать расходы за сегодня 📝"
    await bot.send_message(chat_id=MY_CHAT_ID, text=msg)

# --- ЗАПУСК ---
async def main():
    # Напоминание в 22:00 Ташкент = 17:00 UTC
    scheduler = AsyncIOScheduler(timezone=pytz.utc)
    scheduler.add_job(send_reminder, "cron", hour=17, minute=0)
    scheduler.start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_message))
    app.add_handler(CommandHandler("help", handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен!")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Держим бота живым
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
