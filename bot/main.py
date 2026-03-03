import os
import io
import json
import logging
import asyncio
import time
import sqlite3
import aiohttp
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, LabeledPrice
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ConversationHandler,
                          PreCheckoutQueryHandler, ContextTypes, filters)

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None

try:
    from docx import Document
except ImportError:
    Document = None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
ADMIN_ID = int(os.environ.get('ADMIN_ID', '0'))

STEP_START, STEP_RESUME, STEP_PREFERENCES, STEP_SEARCH, STEP_VACANCY = range(5)

user_data_store = {}
USERS_DB_FILE = 'bot/users_db.json'
STATS_FILE = 'bot/stats.json'
SEARCH_CACHE = {}
CACHE_TTL = 300

conn = sqlite3.connect("bot/vacancies.db", check_same_thread=False)
db_cursor = conn.cursor()
db_cursor.execute("""
CREATE TABLE IF NOT EXISTS telegram_vacancies (
    id TEXT PRIMARY KEY,
    name TEXT,
    employer TEXT,
    salary TEXT,
    url TEXT,
    area TEXT,
    full_text TEXT,
    parsed_at TEXT
)
""")
conn.commit()


def load_users_db():
    try:
        with open(USERS_DB_FILE, 'r') as f:
            data = json.load(f)
            for uid, u in data.items():
                if u.get("turbo_until"):
                    u["turbo_until"] = datetime.fromisoformat(u["turbo_until"])
            return {int(k): v for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_users_db():
    data = {}
    for uid, u in users_db.items():
        entry = dict(u)
        if entry.get("turbo_until"):
            entry["turbo_until"] = entry["turbo_until"].isoformat()
        data[str(uid)] = entry
    try:
        with open(USERS_DB_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving users_db: {e}")


users_db = load_users_db()

HH_API_URL = "https://api.hh.ru"
TRUDVSEM_API_URL = "http://opendata.trudvsem.ru/api/v1"
HEADERS = {
    'User-Agent':
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}


def get_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {
            "credits": 3,
            "turbo_until": None,
            "purchased_start": False,
            "used_after_start": 0,
            "applied_vacancies": [],
            "stats": {
                "total_applies": 0
            }
        }
        save_users_db()
    else:
        user = users_db[user_id]
        if "applied_vacancies" not in user:
            user["applied_vacancies"] = []
        if "stats" not in user:
            user["stats"] = {"total_applies": 0}
    return users_db[user_id]


def clean_applied_history(user):
    now = int(time.time())
    THIRTY_DAYS = 30 * 24 * 60 * 60
    user["applied_vacancies"] = [
        v for v in user["applied_vacancies"] if now - v["ts"] <= THIRTY_DAYS
    ]
    if len(user["applied_vacancies"]) > 200:
        user["applied_vacancies"] = user["applied_vacancies"][-200:]


def get_cache(key):
    if key in SEARCH_CACHE:
        data, ts = SEARCH_CACHE[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def set_cache(key, data):
    SEARCH_CACHE[key] = (data, time.time())


def rank_vacancies(vacancies: list, query: str):
    query_tokens = query.lower().split()
    for vac in vacancies:
        combined = (vac.get("name", "") + " " + vac.get("full_text", "") +
                    " " + str(vac.get("area", {}).get("name", ""))).lower()
        score = 0
        for token in query_tokens:
            if token in combined:
                score += 5
        if vac.get("salary"):
            score += 2
        if "remote" in combined or "удал" in combined:
            score += 2
        if vac.get("source") == "hh":
            score += 1
        vac["score"] = score
    vacancies.sort(key=lambda x: x.get("score", 0), reverse=True)
    return vacancies


def deduplicate_vacancies(vacancies: list) -> list:
    seen = set()
    unique = []
    for vac in vacancies:
        key = (vac.get("name", "").lower().strip(),
               vac.get("employer", {}).get("name", "").lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(vac)
    return unique


async def search_hh(query: str, prefs: dict) -> list:
    try:
        all_vacancies = []
        page = 0
        per_page = 100

        async with aiohttp.ClientSession() as session:
            while True:
                params = {
                    "text": query,
                    "page": page,
                    "per_page": per_page,
                    "area": prefs.get("area", 113),
                    "period": 14
                }
                if prefs.get("schedule"):
                    params["schedule"] = prefs["schedule"]
                if prefs.get("salary"):
                    params["salary"] = prefs["salary"]
                if prefs.get("experience"):
                    params["experience"] = prefs["experience"]

                async with session.get(
                        f"{HH_API_URL}/vacancies",
                        params=params,
                        headers=HEADERS,
                        timeout=aiohttp.ClientTimeout(total=20)) as response:
                    if response.status != 200:
                        break
                    data = await response.json()

                items = data.get("items", [])
                if not items:
                    break

                for item in items:
                    all_vacancies.append({
                        "id":
                        f"hh_{item.get('id')}",
                        "name":
                        item.get("name"),
                        "employer": {
                            "name": item.get("employer", {}).get("name")
                        },
                        "salary":
                        item.get("salary"),
                        "alternate_url":
                        item.get("alternate_url"),
                        "area": {
                            "name": item.get("area", {}).get("name")
                        },
                        "source":
                        "hh"
                    })

                if page >= data.get("pages", 0) - 1:
                    break
                page += 1

        return all_vacancies
    except Exception as e:
        logger.error(f"HH error: {e}")
        return []


def has_access(user_id):
    user = get_user(user_id)
    if user["turbo_until"]:
        if datetime.now() < user["turbo_until"]:
            return True
    return user["credits"] > 0


def use_credit(user_id):
    user = get_user(user_id)
    if user["turbo_until"]:
        if datetime.now() < user["turbo_until"]:
            return False
    user["credits"] -= 1
    if user.get("purchased_start"):
        user["used_after_start"] = user.get("used_after_start", 0) + 1
    save_users_db()
    return user.get("purchased_start") and user.get("used_after_start") in (3,
                                                                            4)


def get_tariff_keyboard():
    keyboard = [[
        InlineKeyboardButton("Start — 70 звёзд (20 откликов)",
                             callback_data="buy_start")
    ],
                [
                    InlineKeyboardButton(
                        "🔥 Active — 180 звёзд (60 откликов, лучший выбор)",
                        callback_data="buy_active")
                ],
                [
                    InlineKeyboardButton(
                        "Turbo — 330 звёзд (белимит на 30 дней)",
                        callback_data="buy_turbo")
                ]]
    return InlineKeyboardMarkup(keyboard)


def load_stats():
    try:
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {'users': [], 'total_searches': 0}


def save_stats(stats):
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(stats, f)
    except Exception as e:
        logger.error(f"Error saving stats: {e}")


def track_user(user_id: int):
    stats = load_stats()
    if user_id not in stats['users']:
        stats['users'].append(user_id)
        save_stats(stats)


def track_search():
    stats = load_stats()
    stats['total_searches'] = stats.get('total_searches', 0) + 1
    save_stats(stats)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    track_user(user_id)

    user_data_store[user_id] = {
        'resume': None,
        'preferences': {},
        'vacancies': [],
        'current_vacancy': None,
        'current_vacancy_index': 0
    }

    message = update.effective_message
    if not message:
        return STEP_RESUME

    await message.reply_text(
        "<b>Найди работу быстрее и выделись среди сотен кандидатов</b> 🚀\n\n"
        "Я анализирую твоё резюме и под каждую подходящую вакансию создаю персональный отклик, "
        "который увеличивает шанс приглашения на собеседование.\n\n"
        "Что ты получаешь:\n"
        "🔎 <b>Не трать время на поиск</b> — все актуальные вакансии из 130+ источников уже собраны для тебя.\n"
        "✍️ <b>Твой отклик выделит тебя среди толпы</b> — я составляю письмо, идеально сочетая твои навыки с требованиями работодателя.\n"
        "📄 <b>Резюме, которое работает само</b> — получи рекомендации, как усилить его, чтобы попадать в топ даже без отклика.\n\n"
        "🎁 <b>3 персональных отклика — бесплатно</b>\n"
        "После этого можно выбрать тариф и продолжить.\n\n"
        "— — —\n\n"
        "<b>Шаг 1 из 3:</b>\n"
        "📎 Загрузи своё резюме (PDF, DOCX или текст)\n\n"
        "Готов начать? Отправь файл 👇",
        parse_mode="HTML")

    return STEP_RESUME


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_ID and user_id != ADMIN_ID:
        await update.message.reply_text(
            "Эта команда только для администратора.")
        return

    stats = load_stats()
    total_users = len(stats.get('users', []))
    total_searches = stats.get('total_searches', 0)

    await update.message.reply_text(
        f"📊 **Статистика бота**\n\n"
        f"👥 Уникальных пользователей: {total_users}\n"
        f"🔍 Всего поисков: {total_searches}\n"
        f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        parse_mode='Markdown')


async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    total = user["stats"].get("total_applies", 0)
    hidden = len(user["applied_vacancies"])
    credits = user.get("credits", 0)
    await update.message.reply_text(f"📊 Твоя статистика:\n\n"
                                    f"Всего откликов: {total}\n"
                                    f"Скрытых вакансий: {hidden}\n"
                                    f"Кредитов осталось: {credits}")


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(f"Твой Telegram ID: `{user_id}`",
                                    parse_mode='Markdown')


async def receive_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(
        f"receive_resume called for user {user_id}, document={update.message.document is not None if update.message else 'no message'}"
    )
    resume_text = None

    if user_id not in user_data_store:
        user_data_store[user_id] = {
            'resume': None,
            'preferences': {},
            'vacancies': [],
            'current_vacancy': None,
            'current_vacancy_index': 0
        }

    if update.message.document:
        file = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await file.download_as_bytearray()
        file_name = update.message.document.file_name.lower()

        if file_name.endswith('.pdf'):
            if PdfReader:
                try:
                    pdf = PdfReader(io.BytesIO(bytes(file_bytes)))
                    resume_text = ""
                    for page in pdf.pages:
                        resume_text += page.extract_text() or ""
                except Exception as e:
                    await update.message.reply_text(
                        f"Ошибка чтения PDF: {e}\nПопробуй отправить текстом.")
                    return STEP_RESUME
            else:
                await update.message.reply_text(
                    "PDF не поддерживается. Отправь Word или текст.")
                return STEP_RESUME

        elif file_name.endswith('.docx'):
            if Document:
                try:
                    doc = Document(io.BytesIO(bytes(file_bytes)))
                    resume_text = "\n".join([p.text for p in doc.paragraphs])
                except Exception as e:
                    await update.message.reply_text(
                        f"Ошибка чтения Word: {e}\nПопробуй отправить текстом."
                    )
                    return STEP_RESUME
            else:
                await update.message.reply_text(
                    "Word не поддерживается. Отправь PDF или текст.")
                return STEP_RESUME

        elif file_name.endswith('.txt'):
            resume_text = bytes(file_bytes).decode('utf-8')
        else:
            await update.message.reply_text(
                "Формат не поддерживается.\n"
                "Отправь PDF, Word (.docx) или текстовый файл (.txt)")
            return STEP_RESUME
    else:
        resume_text = update.message.text

    if not resume_text or len(resume_text.strip()) < 50:
        await update.message.reply_text(
            "Резюме слишком короткое (меньше 50 символов).\n"
            "Пожалуйста, отправь полное резюме.")
        return STEP_RESUME

    user_data_store[user_id]['resume'] = resume_text.strip()

    skip_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Пропустить ➡️", callback_data="skip_preferences")
    ]])
    await update.message.reply_text(
        f"Резюме загружено ({len(resume_text)} символов)\n\n"
        "**Шаг 2 из 3**: Опиши свои пожелания к вакансии\n\n"
        "Напиши своими словами, что важно:\n"
        "• Удалёнка или офис?\n"
        "• Желаемая зарплата?\n"
        "• Опыт работы?\n"
        "• Город?\n\n"
        "Например: «удалёнка, от 150000, без опыта ок, Москва»\n\n"
        "Или нажми кнопку, чтобы искать без фильтров.",
        parse_mode='Markdown',
        reply_markup=skip_kb)
    return STEP_PREFERENCES


async def receive_preferences(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.lower().strip()

    if user_id not in user_data_store:
        await update.message.reply_text("Начни сначала: /start")
        return ConversationHandler.END

    prefs = {'schedule': None, 'salary': None, 'experience': None, 'area': 113}

    if text != 'пропустить':
        if 'удалён' in text or 'удален' in text or 'remote' in text:
            prefs['schedule'] = 'remote'
        elif 'офис' in text:
            prefs['schedule'] = 'fullDay'

        import re
        salary_match = re.search(r'от\s*(\d+)\s*(тыс|к|k)?',
                                 text.replace(' ', ''), re.IGNORECASE)
        if salary_match:
            salary = int(salary_match.group(1))
            suffix = salary_match.group(2)
            if suffix and suffix.lower() in ['тыс', 'к', 'k']:
                salary = salary * 1000
            elif salary < 1000:
                salary = salary * 1000
            prefs['salary'] = salary

        if 'без опыт' in text or 'нет опыт' in text:
            prefs['experience'] = 'noExperience'
        elif '1-3' in text or '1 год' in text or '2 год' in text:
            prefs['experience'] = 'between1And3'
        elif '3-6' in text or '3 год' in text or '5 год' in text:
            prefs['experience'] = 'between3And6'

    user_data_store[user_id]['preferences'] = prefs

    pref_text = []
    if prefs.get('schedule') == 'remote':
        pref_text.append("удалёнка")
    if prefs.get('salary'):
        sal = prefs['salary']
        if sal >= 1000:
            pref_text.append(f"от {sal//1000}k руб")
        else:
            pref_text.append(f"от {sal} руб")
    if prefs.get('experience'):
        exp_map = {
            'noExperience': 'без опыта',
            'between1And3': '1-3 года',
            'between3And6': '3-6 лет'
        }
        pref_text.append(exp_map.get(prefs['experience'], ''))

    pref_summary = ", ".join(pref_text) if pref_text else "без фильтров"

    await update.message.reply_text(
        f"Фильтры: {pref_summary}\n\n"
        "**Шаг 3 из 3**: Поиск вакансий\n\n"
        "Напиши должность для поиска:\n"
        "Например: «менеджер проекта» или «Python разработчик»",
        parse_mode='Markdown')
    return STEP_SEARCH


async def skip_preferences_callback(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if user_id not in user_data_store:
        await query.edit_message_text("Начни сначала: /start")
        return ConversationHandler.END

    prefs = {'schedule': None, 'salary': None, 'experience': None, 'area': 113}
    user_data_store[user_id]['preferences'] = prefs

    await query.edit_message_text(
        "Фильтры: без фильтров\n\n"
        "**Шаг 3 из 3**: Поиск вакансий\n\n"
        "Напиши должность для поиска:\n"
        "Например: «менеджер проекта» или «Python разработчик»",
        parse_mode='Markdown')
    return STEP_SEARCH


ROLE_GROUPS = {
    'менеджер': [
        'менеджер', 'manager', 'руководитель', 'управляющий', 'тимлид',
        'team lead'
    ],
    'разработчик': [
        'разработчик', 'developer', 'программист', 'engineer',
        'инженер-программист', 'software'
    ],
    'аналитик': ['аналитик', 'analyst', 'data analyst', 'бизнес-аналитик'],
    'дизайнер': ['дизайнер', 'designer', 'ui', 'ux', 'верстальщик'],
    'маркетолог': ['маркетолог', 'marketing', 'seo', 'smm', 'контент'],
    'бухгалтер': ['бухгалтер', 'accountant', 'финансист', 'экономист'],
    'hr': ['hr', 'рекрутер', 'кадр', 'подбор персонала'],
    'devops':
    ['devops', 'sre', 'системный администратор', 'sysadmin', 'инфраструктур'],
    'тестировщик': ['тестировщик', 'qa', 'tester', 'quality assurance'],
    'продажи': ['продаж', 'sales', 'торговый', 'продавец'],
}


def normalize_query(query: str) -> list:
    text = query.lower().strip()
    text = ' '.join(text.split())
    tokens = text.split()
    filtered = [t for t in tokens if len(t) >= 2]
    return filtered if filtered else tokens


def _get_role(text: str) -> str:
    text_lower = text.lower()
    for role, keywords in ROLE_GROUPS.items():
        if any(kw in text_lower for kw in keywords):
            return role
    return ''


def calculate_score(vacancy: dict, query_tokens: list) -> int:
    title = vacancy.get('name', '').lower()
    description = vacancy.get('full_text', '') or vacancy.get(
        'snippet', {}).get('requirement', '') or ''
    description = description.lower()

    score = 0
    for token in query_tokens:
        if token in title:
            score += 5
        if token in description:
            score += 2

    query_text = ' '.join(query_tokens)
    query_role = _get_role(query_text)
    title_role = _get_role(title)

    if query_role and title_role:
        if query_role == title_role:
            score += 4
        else:
            score -= 3

    return score


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


def is_core_relevant(vacancy: dict, query_tokens: list) -> bool:
    title = vacancy.get('name', '').lower()
    description = vacancy.get('full_text', '') or vacancy.get(
        'snippet', {}).get('requirement', '') or ''
    description = description.lower()
    combined = title + ' ' + description

    return any(token in combined for token in query_tokens)


JOB_SYNONYMS = {
    'менеджер проекта': [
        'менеджер проекта', 'менеджер проектов', 'project manager',
        'руководитель проекта', 'руководитель проектов', 'проектный менеджер',
        'PM'
    ],
    'project manager': [
        'project manager', 'менеджер проекта', 'менеджер проектов',
        'руководитель проекта', 'PM'
    ],
    'продакт менеджер': [
        'продакт менеджер', 'product manager', 'продукт менеджер',
        'менеджер продукта', 'product owner', 'PO'
    ],
    'product manager': [
        'product manager', 'продакт менеджер', 'product owner',
        'менеджер продукта'
    ],
    'разработчик':
    ['разработчик', 'developer', 'программист', 'инженер-программист'],
    'аналитик': [
        'аналитик', 'analyst', 'бизнес-аналитик', 'системный аналитик',
        'data analyst'
    ],
    'дизайнер': [
        'дизайнер', 'designer', 'UI дизайнер', 'UX дизайнер', 'UI/UX',
        'веб-дизайнер'
    ],
    'маркетолог': [
        'маркетолог', 'marketing manager', 'интернет-маркетолог',
        'digital маркетолог'
    ],
    'hr': [
        'hr', 'HR менеджер', 'рекрутер', 'HR специалист',
        'специалист по подбору'
    ],
}


def expand_query(query: str) -> str:
    query_lower = query.lower().strip()
    for key, synonyms in JOB_SYNONYMS.items():
        if key in query_lower or query_lower in key:
            return ' OR '.join(synonyms)
    return query


async def search_trudvsem(query: str, prefs: dict) -> list:
    try:
        all_vacancies = []
        offset = 0
        limit = 100

        async with aiohttp.ClientSession() as session:
            while True:
                params = {"text": query, "offset": offset, "limit": limit}

                async with session.get(
                        f"{TRUDVSEM_API_URL}/vacancies",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=20)) as response:
                    if response.status != 200:
                        break
                    data = await response.json()

                results = data.get("results", {}).get("vacancies", [])
                if not results:
                    break

                for item in results:
                    vac = item.get("vacancy", {})
                    company = vac.get("company", {})
                    companycode = company.get("companycode", "")
                    vac_id = vac.get("id", "")
                    salary_min = vac.get("salary_min")
                    salary_max = vac.get("salary_max")
                    all_vacancies.append({
                        "id":
                        f"tv_{vac_id}",
                        "name":
                        vac.get("job-name", ""),
                        "employer": {
                            "name": company.get("name", "")
                        },
                        "salary": {
                            "from": salary_min,
                            "to": salary_max,
                            "currency": "RUR"
                        } if salary_min or salary_max else None,
                        "alternate_url":
                        f"https://trudvsem.ru/vacancy/card/{companycode}/{vac_id}"
                        if companycode and vac_id else "",
                        "area": {
                            "name": vac.get("region", {}).get("name", "")
                        },
                        "source":
                        "trudvsem"
                    })

                offset += limit

        return all_vacancies

    except Exception as e:
        logger.error(f"Trudvsem error: {e}")
        return []


def search_telegram_vacancies(query: str, prefs: dict) -> list:
    query_lower = query.lower()
    try:
        db_cursor.execute(
            """
        SELECT id, name, employer, salary, url, area, full_text
        FROM telegram_vacancies
        WHERE lower(name) LIKE ? OR lower(full_text) LIKE ?
        """, (f"%{query_lower}%", f"%{query_lower}%"))
        rows = db_cursor.fetchall()
    except Exception as e:
        logger.error(f"SQLite search error: {e}")
        return []

    results = []
    for row in rows:
        results.append({
            "id": row[0],
            "name": row[1],
            "employer": {
                "name": row[2]
            },
            "salary": row[3],
            "alternate_url": row[4],
            "area": {
                "name": row[5]
            },
            "full_text": row[6],
            "source": "telegram"
        })
    return results


def _score_label(score: int) -> str:
    if score >= 8:
        return "🔥"
    elif score >= 4:
        return "🟡"
    return "⚪"


def get_vacancy_action_keyboard():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✍️ Сопроводительное письмо",
                                 callback_data="gen_cover"),
            InlineKeyboardButton("📄 Рекомендации по резюме",
                                 callback_data="adapt_resume")
        ],
         [
             InlineKeyboardButton("🔙 К списку вакансий",
                                  callback_data="back_to_list"),
             InlineKeyboardButton("🔎 Новый поиск", callback_data="new_search")
         ]])


def build_vacancy_keyboard(vacancies: list,
                           page: int = 0,
                           page_size: int = 10) -> list:
    start = page * page_size
    end = start + page_size
    page_vacancies = vacancies[start:end]
    total_pages = (len(vacancies) + page_size - 1) // page_size

    def _get_group(sc):
        if sc >= 8:
            return "🔥 Самые релевантные"
        elif sc >= 4:
            return "🟡 Смежные"
        return "⚪ Дополнительно"

    prev_group_global = _get_group(vacancies[start - 1].get(
        '_score', 0)) if start > 0 else None

    keyboard = []
    for i, vac in enumerate(page_vacancies):
        idx = start + i
        group = _get_group(vac.get('_score', 0))

        if group != prev_group_global:
            keyboard.append([
                InlineKeyboardButton(f"— {group} —",
                                     callback_data=f"noop_{idx}")
            ])
            prev_group_global = group

        salary_text = ""
        if vac.get('salary'):
            sal = vac['salary']
            sal_from = sal.get('from') or 0
            sal_to = sal.get('to') or 0
            if sal_from and sal_to:
                salary_text = f" ({sal_from//1000}k-{sal_to//1000}k)"
            elif sal_from:
                salary_text = f" (от {sal_from//1000}k)"
            elif sal_to:
                salary_text = f" (до {sal_to//1000}k)"

        source = vac.get('source', 'hh')
        if source == 'hh':
            source_icon = "🔵"
        elif source == 'trudvsem':
            source_icon = "🟢"
        else:
            source_icon = "📱"
        company = vac.get('employer', {}).get('name', '')[:12]
        btn_text = f"{source_icon} {vac['name'][:32]}{salary_text} • {company}"
        keyboard.append(
            [InlineKeyboardButton(btn_text, callback_data=f"vac_{idx}")])

    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{page-1}"))
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton("➡️ Ещё", callback_data=f"page_{page+1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append(
        [InlineKeyboardButton("🔄 Новый поиск", callback_data="new_search")])
    return keyboard


async def search_vacancies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.message.text.strip()

    if user_id not in user_data_store:
        await update.message.reply_text("Начни сначала: /start")
        return ConversationHandler.END

    track_search()
    prefs = user_data_store[user_id].get('preferences', {})

    expanded_query = expand_query(query)

    await update.message.reply_text(f"Ищу вакансии: {query}...")

    try:
        cache_key = f"{query}_{str(prefs)}"
        cached = get_cache(cache_key)

        if cached:
            vacancies = cached
            hh_vacancies = [v for v in vacancies if v.get('source') == 'hh']
            tv_vacancies = [
                v for v in vacancies if v.get('source') == 'trudvsem'
            ]
            tg_vacancies = [
                v for v in vacancies if v.get('source') == 'telegram'
            ]
        else:
            hh_vacancies = await search_hh(expanded_query, prefs)
            tv_vacancies = await search_trudvsem(query, prefs)
            tg_vacancies = search_telegram_vacancies(query, prefs)

            vacancies = hh_vacancies + tv_vacancies + tg_vacancies
            vacancies = deduplicate_vacancies(vacancies)
            vacancies = rank_vacancies(vacancies, query)
            set_cache(cache_key, vacancies)

        user = get_user(user_id)
        clean_applied_history(user)
        applied_ids = {v["id"] for v in user["applied_vacancies"]}
        vacancies = [
            vac for vac in vacancies
            if str(vac.get("id", "")) not in applied_ids
        ]

        if not vacancies:
            await update.message.reply_text(
                "Вакансии не найдены.\n"
                "Попробуй изменить запрос или напиши новую должность:")
            return STEP_SEARCH

        exclude_keywords = [
            'менеджер по продажам', 'sales manager', 'менеджер продаж',
            'торговый представитель', 'продавец-консультант', 'продавец'
        ]
        vacancies = [
            vac for vac in vacancies
            if not any(excl in vac.get('name', '').lower()
                       for excl in exclude_keywords)
        ]

        query_tokens = normalize_query(query)

        if query_tokens:
            scored = []
            for vac in vacancies:
                if not is_core_relevant(vac, query_tokens):
                    continue
                sc = calculate_score(vac, query_tokens)
                if sc > 0:
                    vac['_score'] = sc
                    scored.append(vac)

            scored.sort(key=lambda v: v.get('_score', 0), reverse=True)
            if scored:
                vacancies = scored

        if not vacancies:
            await update.message.reply_text(
                "Вакансий не найдено.\n"
                "Попробуй изменить запрос или напиши новую должность:")
            return STEP_SEARCH

        for vac in vacancies:
            if '_score' not in vac:
                vac['_score'] = 5

        sources = []
        if hh_vacancies:
            sources.append(f"hh.ru: {len(hh_vacancies)}")
        if tv_vacancies:
            sources.append(f"Работа России: {len(tv_vacancies)}")
        if tg_vacancies:
            sources.append(f"Telegram: {len(tg_vacancies)}")
        source_text = " + ".join(sources) if sources else ""

        hot = [v for v in vacancies if v.get('_score', 0) >= 8]
        mid = [v for v in vacancies if 4 <= v.get('_score', 0) <= 7]
        low = [v for v in vacancies if 1 <= v.get('_score', 0) <= 3]

        group_labels = []
        if hot:
            group_labels.append(f"🔥 Самые релевантные: {len(hot)}")
        if mid:
            group_labels.append(f"🟡 Смежные: {len(mid)}")
        if low:
            group_labels.append(f"⚪ Дополнительно: {len(low)}")
        groups_text = "\n".join(group_labels)

        user_data_store[user_id]['vacancies'] = vacancies
        user_data_store[user_id]['vacancy_groups'] = {
            'hot': len(hot),
            'mid': len(mid),
            'low': len(low)
        }
        user_data_store[user_id]['current_page'] = 0
        user_data_store[user_id]['total_found'] = len(vacancies)
        user_data_store[user_id]['source_text'] = source_text

        keyboard = build_vacancy_keyboard(vacancies, 0)

        hidden_count = len(applied_ids)
        if hidden_count > 0:
            keyboard.append([
                InlineKeyboardButton(
                    f"🔄 Показать снова все ({hidden_count} скрыто)",
                    callback_data="reset_history")
            ])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Найдено {len(vacancies)} вакансий ({source_text})\n\n"
            f"{groups_text}\n\n"
            "⏳ Самые свежие вакансии разбирают в первые 48 часов.\n\n"
            "Нажми на вакансию для просмотра:",
            reply_markup=reply_markup)
        return STEP_VACANCY

    except Exception as e:
        logger.error(f"Error searching vacancies: {e}")
        await update.message.reply_text(f"Ошибка при поиске: {str(e)[:100]}\n"
                                        "Попробуй другой запрос:")
        return STEP_SEARCH


async def vacancy_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    logger.info(f"vacancy_selected: user={user_id}, data={query.data}")

    if query.data == "reset_history":
        user = get_user(user_id)
        user["applied_vacancies"] = []
        save_users_db()
        await query.edit_message_text(
            "История откликов очищена ✅\nВведи поисковый запрос, чтобы увидеть все вакансии:"
        )
        return STEP_SEARCH

    if query.data == "new_search":
        await query.edit_message_text(
            "Напиши должность для поиска:\n"
            "Например: «менеджер проекта» или «Python разработчик»")
        return STEP_SEARCH

    if query.data == "back_search":
        await query.edit_message_text(
            "Напиши должность для поиска:\n"
            "Например: «менеджер проекта» или «Python разработчик»")
        return STEP_SEARCH

    if query.data.startswith("page_"):
        page = int(query.data.split('_')[1])
        vacancies = user_data_store[user_id].get('vacancies', [])
        user_data_store[user_id]['current_page'] = page
        keyboard = build_vacancy_keyboard(vacancies, page)
        total = user_data_store[user_id].get('total_found', len(vacancies))
        await query.edit_message_text(
            f"Найдено {total} вакансий (стр. {page+1}).\n\nНажми на вакансию:",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return STEP_VACANCY

    vacancy_index = int(query.data.split('_')[1])

    if user_id not in user_data_store or not user_data_store[user_id].get(
            'vacancies'):
        await query.edit_message_text("Сессия истекла. Начни заново: /start")
        return ConversationHandler.END

    vacancies = user_data_store[user_id]['vacancies']
    if vacancy_index >= len(vacancies):
        await query.edit_message_text(
            "Вакансия не найдена. Начни заново: /start")
        return ConversationHandler.END

    vacancy = vacancies[vacancy_index]
    source = vacancy.get('source', 'hh')

    await query.edit_message_text("Загружаю детали вакансии...")

    try:
        if source == 'telegram':
            vacancy_details = vacancy
            description = vacancy.get('full_text', vacancy.get('name',
                                                               ''))[:800]

            salary_text = "Не указана"
            if vacancy.get('salary'):
                sal = vacancy['salary']
                if sal.get('from') and sal.get('to'):
                    salary_text = f"{sal['from']:,} - {sal['to']:,} руб."
                elif sal.get('from'):
                    salary_text = f"от {sal['from']:,} руб."
                elif sal.get('to'):
                    salary_text = f"до {sal['to']:,} руб."

            vacancy_info = (
                f"📱 **{vacancy.get('name', 'Вакансия')}**\n\n"
                f"Компания: {vacancy.get('employer', {}).get('name', 'Не указано')}\n"
                f"Зарплата: {salary_text}\n"
                f"Тип: {vacancy.get('work_type', 'Не указано')}\n"
                f"Канал: {vacancy.get('channel', '')}\n\n"
                f"Описание:\n{description}\n\n"
                f"Ссылка: {vacancy.get('alternate_url', vacancy.get('url', ''))}"
            )
        elif source == 'trudvsem':
            vacancy_details = vacancy
            salary_text = "Не указана"
            if vacancy.get('salary'):
                sal = vacancy['salary']
                if sal.get('from') and sal.get('to'):
                    salary_text = f"{sal['from']:,} - {sal['to']:,} руб."
                elif sal.get('from'):
                    salary_text = f"от {sal['from']:,} руб."
                elif sal.get('to'):
                    salary_text = f"до {sal['to']:,} руб."

            vacancy_info = (
                f"🟢 **{vacancy.get('name', 'Вакансия')}**\n\n"
                f"Компания: {vacancy.get('employer', {}).get('name', 'Не указано')}\n"
                f"Зарплата: {salary_text}\n"
                f"Регион: {vacancy.get('area', {}).get('name', 'Не указано')}\n\n"
                f"Ссылка: {vacancy.get('alternate_url', '')}")
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{HH_API_URL}/vacancies/{str(vacancy['id']).replace('hh_', '')}",
                        headers=HEADERS,
                        timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        raise Exception(f"HTTP {response.status}")
                    vacancy_details = await response.json()

            description = vacancy_details.get('description', '')
            from html import unescape
            import re
            description = re.sub(r'<[^>]+>', ' ', description)
            description = unescape(description)
            description = ' '.join(description.split())[:800]

            salary_text = "Не указана"
            if vacancy_details.get('salary'):
                sal = vacancy_details['salary']
                if sal.get('from') and sal.get('to'):
                    salary_text = f"{sal['from']:,} - {sal['to']:,} {sal.get('currency', '')}"
                elif sal.get('from'):
                    salary_text = f"от {sal['from']:,} {sal.get('currency', '')}"
                elif sal.get('to'):
                    salary_text = f"до {sal['to']:,} {sal.get('currency', '')}"

            vacancy_info = (
                f"🔵 **{vacancy_details['name']}**\n\n"
                f"Компания: {vacancy_details.get('employer', {}).get('name', 'Не указано')}\n"
                f"Зарплата: {salary_text}\n"
                f"Город: {vacancy_details.get('area', {}).get('name', 'Не указано')}\n"
                f"Опыт: {vacancy_details.get('experience', {}).get('name', 'Не указано')}\n"
                f"Занятость: {vacancy_details.get('schedule', {}).get('name', 'Не указано')}\n\n"
                f"Описание:\n{description}...\n\n"
                f"Ссылка: {vacancy_details.get('alternate_url', '')}")

        try:
            await context.bot.send_message(chat_id=user_id,
                                           text=vacancy_info,
                                           parse_mode='Markdown')
        except Exception:
            await context.bot.send_message(chat_id=user_id, text=vacancy_info)

        user_data_store[user_id]['current_vacancy'] = vacancy_details
        user_data_store[user_id]['current_vacancy_index'] = vacancy_index

        await context.bot.send_message(
            chat_id=user_id,
            text="Что сделать?",
            reply_markup=get_vacancy_action_keyboard())
        return STEP_VACANCY

    except Exception as e:
        logger.error(f"Error getting vacancy details: {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Ошибка: {str(e)}",
            reply_markup=get_vacancy_action_keyboard())
        return STEP_VACANCY


def detect_level(vacancy_text: str) -> str:
    text = vacancy_text.lower()
    if any(w in text for w in ["junior", "jun", "начина", "без опыта"]):
        return "junior"
    if any(w in text
           for w in ["senior", "lead", "руковод", "5+ лет", "7+ лет"]):
        return "senior"
    return "middle"


RESUME_STRUCTURING_PROMPT = """
Ты HR-аналитик.

Извлеки из резюме СТРОГО ФАКТЫ.

❗ Не додумывай.
❗ Если нет данных — пиши null.

Верни JSON строго в формате:

{
  "role": "",
  "total_experience_years": "",
  "hard_skills": [],
  "soft_skills": [],
  "achievements": [
    {
      "description": "",
      "metrics": ""
    }
  ],
  "tools": [],
  "industries": [],
  "management_experience": "",
  "notable_projects": []
}

Используй только информацию из текста.
"""

VACANCY_ANALYSIS_PROMPT = """
Ты HR-аналитик.

Проанализируй вакансию и верни JSON:

{
  "required_skills": [],
  "preferred_skills": [],
  "required_experience": "",
  "main_tasks": [],
  "business_goals": [],
  "keywords": []
}

Не интерпретируй. Только факты из вакансии.
"""


async def call_openrouter(system_prompt: str,
                          user_prompt: str,
                          max_tokens: int = 800) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://replit.com",
                    "X-Title": "HH Resume Helper"
                },
                json={
                    "model":
                    "openai/gpt-4.1",
                    "messages": [{
                        "role": "system",
                        "content": system_prompt
                    }, {
                        "role": "user",
                        "content": user_prompt
                    }],
                    "max_tokens":
                    max_tokens,
                    "temperature":
                    0.1,
                    "top_p":
                    0.8
                },
                timeout=aiohttp.ClientTimeout(total=60)) as response:
            result = await response.json()

    if 'error' in result:
        raise Exception(
            f"API: {result['error'].get('message', result['error'])}")
    if 'choices' not in result or not result['choices']:
        raise Exception("Неожиданный ответ API")
    return result['choices'][0]['message']['content']


def get_cover_letter_system_prompt(level: str) -> str:
    base_rules = (
        "Ты пишешь сопроводительное письмо как практик, а не как HR-копирайтер.\n\n"
        "КРИТИЧЕСКИЕ ПРАВИЛА:\n"
        "1. Письмо ВСЕГДА начинается с 'Добрый день,' или 'Здравствуйте,'\n"
        "2. Запрещены фразы:\n"
        "- 'что позволило'\n"
        "- 'что обеспечило'\n"
        "- 'что привело к'\n"
        "- 'что расширило'\n"
        "- 'такой опыт поможет'\n"
        "- 'этот навык будет полезен'\n"
        "- любые прогнозы о будущем\n"
        "3. Нельзя объяснять очевидное и интерпретировать результаты.\n"
        "4. Не повторять формулировки из вакансии дословно.\n"
        "5. Не писать воду и абстрактные формулировки.\n\n"
        "ФОРМУЛА КАЖДОГО ДОСТИЖЕНИЯ:\n"
        "Я сделал X.\n"
        "Получили Y.\n"
        "Цифры Z.\n\n"
        "Если нет точных цифр — не придумывать.\n"
        "Короткие предложения.\n"
        "Простой деловой язык.\n\n"
        "СТРУКТУРА:\n"
        "1. Приветствие\n"
        "2. Краткая профессиональная идентификация\n"
        "3. 2–3 достижения по формуле выше\n"
        "4. Короткое завершение без просьб\n\n"
        "Перед завершением проверь: нет ли запрещённых связок. Если есть — перепиши.\n"
    )

    if level == "junior":
        return base_rules + (
            "\nДополнительно для junior:\n"
            "- Делай акцент на выполненных задачах.\n"
            "- Покажи конкретные результаты, даже если они небольшие.\n"
            "- 4–6 коротких абзацев.\n\n"
            'В конце: "Подробности — в резюме."')

    if level == "senior":
        return base_rules + (
            "\nДополнительно для senior:\n"
            "- Делай акцент на масштабе проектов и ответственности.\n"
            "- Указывай влияние на показатели: выручка, сроки, издержки, процессы.\n"
            "- Тон уверенный, без излишней вежливости.\n"
            "- 4–6 абзацев.\n\n"
            "Завершение — нейтральное предложение обсудить задачи.")

    return base_rules + ("\n- 5–7 коротких абзацев.\n"
                         "- Фокус на измеримых результатах.\n\n"
                         'В конце: "Подробности — в резюме."')


async def generate_cover_letter(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if not has_access(user_id):
        await query.edit_message_text(
            "⚠️ Ты использовал 3 бесплатных AI-отклика.\n\n"
            "За это время:\n"
            "• Ты получил персонализированные письма\n"
            "• Увидел релевантные вакансии\n"
            "• Увеличил шанс приглашения\n\n"
            "Чтобы продолжить и не упустить свежие вакансии, выбери пакет                      ниже.\n\n"
            "Большинство пользователей находят работу в течение 2–4 недель активных             откликов.\n\n",
            parse_mode="HTML",
            reply_markup=get_tariff_keyboard())
        return STEP_VACANCY

    if user_id not in user_data_store:
        await query.edit_message_text("Сессия истекла. Начни заново: /start")
        return ConversationHandler.END

    resume = user_data_store[user_id].get('resume')
    vacancy = user_data_store[user_id].get('current_vacancy')

    if not resume:
        await query.edit_message_text("Резюме не найдено. Начни заново: /start"
                                      )
        return ConversationHandler.END

    if not vacancy:
        await query.edit_message_text(
            "Вакансия не выбрана. Начни заново: /start")
        return ConversationHandler.END

    show_upsell = use_credit(user_id)

    user = get_user(user_id)
    clean_applied_history(user)
    vac_id = str(vacancy.get('id', ''))
    if vac_id:
        existing_ids = [v["id"] for v in user["applied_vacancies"]]
        if vac_id not in existing_ids:
            user["applied_vacancies"].append({
                "id": vac_id,
                "ts": int(time.time())
            })
            user["stats"]["total_applies"] += 1
            save_users_db()

    await query.edit_message_text("Анализирую резюме и вакансию (15-30 сек)..."
                                  )

    description = vacancy.get('description', '')
    from html import unescape
    import re
    description = re.sub(r'<[^>]+>', ' ', description)
    description = unescape(description)
    description = ' '.join(description.split())[:2000]

    vacancy_text = f"{vacancy.get('name', '')} {description}"
    level = detect_level(vacancy_text)
    system_prompt = get_cover_letter_system_prompt(level)

    try:
        resume_json, vacancy_json = await asyncio.gather(
            call_openrouter(RESUME_STRUCTURING_PROMPT,
                            f"Резюме:\n{resume[:2500]}", 600),
            call_openrouter(
                VACANCY_ANALYSIS_PROMPT,
                f"Вакансия:\nНазвание: {vacancy.get('name', '')}\nОписание: {description}",
                400))

        prompt = f"""Напиши сопроводительное письмо на русском языке.

СТРУКТУРИРОВАННОЕ РЕЗЮМЕ:
{resume_json}

АНАЛИЗ ВАКАНСИИ:
{vacancy_json}

Компания: {vacancy.get('employer', {}).get('name', '')}
Должность: {vacancy.get('name', '')}

Напиши только текст письма, без заголовков и подписей. Длина: 120-180 слов."""

        cover_letter = await call_openrouter(system_prompt, prompt, 800)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"**Сопроводительное письмо:**\n\n{cover_letter}",
                parse_mode='Markdown')
        except Exception:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Сопроводительное письмо:\n\n{cover_letter}")

        await context.bot.send_message(
            chat_id=user_id,
            text=f"Ссылка: {vacancy.get('alternate_url', '')}\n\n"
            "Скопируй и отправь сопроводительное письмо",
            reply_markup=get_vacancy_action_keyboard())

        if show_upsell:
            upsell_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔥 Active — 180 звёзд (60 откликов)",
                                     callback_data="buy_active")
            ]])
            await context.bot.send_message(
                chat_id=user_id,
                text="Ты активно откликаешься 🔥\n\n"
                "Active пакет даст 60 откликов и обойдётся дешевле за каждый отклик.\n"
                "Обновить тариф?",
                reply_markup=upsell_kb)

        return STEP_VACANCY

    except Exception as e:
        logger.error(f"Error generating cover letter: {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Ошибка генерации: {str(e)[:200]}",
            reply_markup=get_vacancy_action_keyboard())
        return STEP_VACANCY


async def adapt_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if not has_access(user_id):
        await query.edit_message_text(
            "⚠️ Бесплатные 3 отклика использованы.\n\n"
            "Чтобы продолжить и не терять актуальные вакансии, выбери пакет ниже.\n"
            "Самые быстрые кандидаты получают приглашения первыми.",
            parse_mode="HTML",
            reply_markup=get_tariff_keyboard())
        return STEP_VACANCY

    if user_id not in user_data_store:
        await query.edit_message_text("Сессия истекла. Начни заново: /start")
        return ConversationHandler.END

    resume = user_data_store[user_id].get('resume')
    vacancy = user_data_store[user_id].get('current_vacancy')

    if not resume or not vacancy:
        await query.edit_message_text("Данные не найдены. Начни заново: /start"
                                      )
        return ConversationHandler.END

    show_upsell = use_credit(user_id)
    await query.edit_message_text(
        "Анализирую и адаптирую резюме (10-20 сек)...")

    description = vacancy.get('description', '')
    from html import unescape
    import re
    description = re.sub(r'<[^>]+>', ' ', description)
    description = unescape(description)
    description = ' '.join(description.split())[:2000]

    prompt = f"""
    Ты карьерный стратег и эксперт по ATS-оптимизации.

    Твоя задача — провести глубокий аудит резюме под конкретную вакансию.

    Никаких общих советов.
    Только точечные правки с объяснением проблемы.

    --------------------------------------------------
    ШАГ 1. Проанализируй вакансию и выдели:

    - 5–10 ключевых hard skills
    - 3–5 обязательных требований
    - Основные задачи роли
    - Ключевые слова для ATS

    --------------------------------------------------
    ШАГ 2. Проанализируй резюме:

    - Где требования уже отражены
    - Где они отражены слабо
    - Где отсутствуют
    - Где формулировки слишком общие
    - Где нет цифр, но можно усилить конкретикой (без фантазий)

    --------------------------------------------------
    ШАГ 3. Дай конкретные правки.

    ФОРМАТ:

    📝 ПРАВКИ В РЕЗЮМЕ:

    1. Раздел "Опыт работы" → [компания / должность]

    БЫЛО:
    "[точная цитата]"

    ПРОБЛЕМА:
    [нет результата / нет цифр / не отражает требование вакансии / слишком общее]

    СТАЛО:
    "[переписанная версия с конкретным результатом]"

    --------------------------------------------------

    2. Раздел "Навыки"

    ДОБАВИТЬ:
    - [навык из вакансии, если он уже подтверждён опытом]

    ПЕРЕФОРМУЛИРОВАТЬ:
    - [конкретный навык → более точная формулировка]

    --------------------------------------------------

    3. Раздел "О себе" / "Профиль"

    БЫЛО:
    "[цитата]"

    ПРОБЛЕМА:
    [слишком общее / нет специализации / нет фокуса]

    СТАЛО:
    "[версия под конкретную вакансию]"

    --------------------------------------------------

    🎯 КЛЮЧЕВЫЕ СЛОВА ДЛЯ ATS:

    - [слово] → вставить в [конкретный раздел]
    - [слово] → вставить в [конкретный раздел]

    --------------------------------------------------

    📊 ГЛАВНЫЕ РАЗРЫВЫ МЕЖДУ РЕЗЮМЕ И ВАКАНСИЕЙ:

    1.
    2.
    3.

    Дай 5–8 конкретных правок.
    Не придумывай новый опыт.
    Работай как редактор executive-уровня.
    """

    try:
        recommendations = await call_openrouter(
            "Ты эксперт по резюме. Давай только конкретные правки в формате БЫЛО/СТАЛО.",
            prompt, 1000)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=
                f"**Рекомендации по адаптации резюме:**\n\n{recommendations}",
                parse_mode='Markdown')
        except Exception:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Рекомендации по адаптации резюме:\n\n{recommendations}")

        await context.bot.send_message(
            chat_id=user_id,
            text="Что дальше?",
            reply_markup=get_vacancy_action_keyboard())

        if show_upsell:
            upsell_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔥 Active — 180 звёзд (60 откликов)",
                                     callback_data="buy_active")
            ]])
            await context.bot.send_message(
                chat_id=user_id,
                text="Ты активно откликаешься 🔥\n\n"
                "Active пакет даст 60 откликов и обойдётся дешевле за каждый отклик.\n"
                "Обновить тариф?",
                reply_markup=upsell_kb)

        return STEP_VACANCY

    except Exception as e:
        logger.error(f"Error adapting resume: {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Ошибка анализа: {str(e)}",
            reply_markup=get_vacancy_action_keyboard())
        return STEP_VACANCY


async def back_to_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if user_id not in user_data_store or not user_data_store[user_id].get(
            'vacancies'):
        await query.edit_message_text("Сессия истекла. Начни заново: /start")
        return ConversationHandler.END

    vacancies = user_data_store[user_id]['vacancies']
    page = user_data_store[user_id].get('current_page', 0)
    total = user_data_store[user_id].get('total_found', len(vacancies))

    keyboard = build_vacancy_keyboard(vacancies, page)

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"Найдено {total} вакансий.\n\nНажми на вакансию:",
        reply_markup=reply_markup)
    return STEP_VACANCY


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "🤖 <b>HH Resume Helper</b>\n\n"
        "Автоматический поиск работы по самым свежим вакансиям из 130+ источников\n\n"
        "<b>Что умеет бот:</b>\n"
        "• Анализирует твоё резюме\n"
        "• Ищет вакансии с фильтрами (зарплата, удалёнка, опыт)\n"
        "• Генерирует сопроводительные письма на основании требований работодателя и твоих навыков в резюме\n"
        "• Даёт рекомендации по адаптации резюме\n\n"
        "<b>Как пользоваться:</b>\n"
        "1️⃣ Загрузи резюме (PDF, Word или текст)\n"
        "2️⃣ Укажи пожелания (например: «удалёнка, от 150к»)\n"
        "3️⃣ Напиши должность для поиска\n"
        "4️⃣ Выбери вакансию и получи сопроводительное письмо\n\n"
        "<b>Команды:</b>\n"
        "/start — Начать поиск работы\n"
        "/buy — Оплатить тариф для дальнейшей работы\n"
        "/mystats — Твоя статистика\n"
        "/help — Справка\n"
        "/cancel — Отменить поиск\n\n"
        "<b>Задать вопрос в поддержку или написать пожелание</b> - @Tek_flow",
        parse_mode="HTML")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено. Для нового поиска: /start")
    return ConversationHandler.END


async def post_init(application):
    await application.bot.set_my_commands([("start", "Начать поиск работы"),
                                           ("buy", "Купить пакет откликов"),
                                           ("help", "Справка и возможности"),
                                           ("cancel", "Отменить текущий поиск")
                                           ])


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "💼 <b>Выбери пакет и продолжай откликаться без ограничений</b>\n\n"
        "Ты уже увидел, как работает персонализация откликов.\n"
        "Теперь можно масштабировать результат.\n\n"
        "📦 <b>Start</b> — <b>70 звёзд</b>\n"
        "20 AI-откликов\n\n"
        "🚀 <b>Active</b> — <b>180 звёзд</b>\n"
        "60 AI-откликов (лучший баланс цены и результата)\n\n"
        "🔥 <b>Turbo</b> — <b>330 звёзд</b>\n"
        "Безлимит откликов на 30 дней\n\n"
        "Чем больше откликов — тем выше шанс получить оффер.\n",
        parse_mode="HTML",
        reply_markup=get_tariff_keyboard())


async def handle_buy_callback(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pkg = query.data.replace("buy_", "")

    prices_map = {
        "start": ("Start", 70, 20),
        "active": ("Active", 180, 60),
        "turbo": ("Turbo", 330, None),
    }

    if pkg not in prices_map:
        return

    title, amount, credits = prices_map[pkg]

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"{title} пакет",
        description="Оплата через Telegram Stars",
        payload=pkg,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(title, amount)],
    )


async def handle_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower().strip()

    if text not in ["start", "active", "turbo"]:
        return

    prices_map = {
        "start": ("Start", 70, 20),
        "active": ("Active", 180, 30),
        "turbo": ("Turbo", 330, None),
    }

    title, amount, credits = prices_map[text]

    await update.message.reply_invoice(
        title=f"{title} пакет",
        description="Оплата через Telegram Stars",
        payload=text,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(title, amount)],
    )


async def precheckout_callback(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    logger.info(
        f"precheckout: user={query.from_user.id}, payload={query.invoice_payload}, amount={query.total_amount}"
    )
    await query.answer(ok=True)


async def successful_payment(update: Update,
                             context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    logger.info(
        f"successful_payment: user={user_id}, payload={payload}, amount={payment.total_amount}"
    )

    user = get_user(user_id)
    old_credits = user.get("credits", 0)

    if payload == "start":
        user["credits"] += 20
        user["purchased_start"] = True
        user["used_after_start"] = 0
    elif payload == "active":
        user["credits"] += 60
        user["purchased_start"] = False
        user["used_after_start"] = 0
    elif payload == "turbo":
        user["turbo_until"] = (datetime.now() + timedelta(days=30)).isoformat()
        user["purchased_start"] = False
        user["used_after_start"] = 0

    save_users_db()
    logger.info(
        f"payment applied: user={user_id}, credits {old_credits} -> {user.get('credits', 0)}"
    )
    await update.message.reply_text(
        "Оплата прошла успешно ✅ Доступ активирован.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        return

    if not OPENROUTER_API_KEY:
        logger.error("OPENROUTER_API_KEY not set!")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(
        post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            STEP_RESUME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               receive_resume),
                MessageHandler(filters.Document.ALL, receive_resume)
            ],
            STEP_PREFERENCES: [
                CallbackQueryHandler(skip_preferences_callback,
                                     pattern='^skip_preferences$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               receive_preferences)
            ],
            STEP_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               search_vacancies)
            ],
            STEP_VACANCY: [
                CallbackQueryHandler(noop_callback, pattern=r'^noop_'),
                CallbackQueryHandler(vacancy_selected, pattern=r'^vac_\d+$'),
                CallbackQueryHandler(vacancy_selected, pattern='^new_search$'),
                CallbackQueryHandler(vacancy_selected,
                                     pattern='^reset_history$'),
                CallbackQueryHandler(vacancy_selected,
                                     pattern='^back_search$'),
                CallbackQueryHandler(vacancy_selected, pattern=r'^page_\d+$'),
                CallbackQueryHandler(back_to_list, pattern='^back_to_list$'),
                CallbackQueryHandler(generate_cover_letter,
                                     pattern='^gen_cover$'),
                CallbackQueryHandler(adapt_resume, pattern='^adapt_resume$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               search_vacancies)
            ]
        },
        fallbacks=[
            CommandHandler('start', start),
            CommandHandler('cancel', cancel)
        ],
        allow_reentry=True)

    application.add_handler(conv_handler)
    application.add_handler(
        CallbackQueryHandler(handle_buy_callback,
                             pattern=r'^buy_(start|active|turbo)$'))

    async def fallback_document(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
        """Handle documents sent outside of conversation - tell user to /start"""
        await update.message.reply_text(
            "Чтобы загрузить резюме, сначала нажми /start")

    application.add_handler(
        MessageHandler(filters.Document.ALL, fallback_document))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('myid', myid_command))
    application.add_handler(CommandHandler('mystats', mystats_command))
    application.add_handler(CommandHandler('buy', buy_command))
    application.add_handler(
        MessageHandler(
            filters.Regex(r'(?i)^(start|active|turbo)$') & ~filters.COMMAND,
            handle_package))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    logger.info("Bot starting...")

    async def run_parser_periodically():
        """Run telegram parser every 12 hours"""
        await asyncio.sleep(300)
        while True:
            try:
                logger.info("Starting scheduled parser run...")
                proc = await asyncio.create_subprocess_exec(
                    'python',
                    'bot/telegram_parser.py',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                            timeout=300)
                    if proc.returncode == 0:
                        logger.info("Parser completed successfully")
                    else:
                        logger.error(f"Parser error: {stderr.decode()[:500]}")
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.error("Parser timed out after 300s")
            except Exception as e:
                logger.error(f"Parser exception: {e}")
            await asyncio.sleep(30 * 60)

    async def run_bot():
        async with application:
            await application.initialize()
            await application.start()
            await application.bot.set_my_commands([
                ("start", "Начать поиск работы"), ("mystats",
                                                   "Моя статистика"),
                ("buy", "Купить пакет откликов"),
                ("help", "Справка и возможности"),
                ("cancel", "Отменить текущий поиск")
            ])
            logger.info("Bot commands menu updated")
            asyncio.create_task(run_parser_periodically())
            await application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES)
            await asyncio.Event().wait()

    asyncio.run(run_bot())


if __name__ == '__main__':
    main()
