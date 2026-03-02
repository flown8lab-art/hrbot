import os
import json
import asyncio
import logging
import re
import sqlite3
import aiohttp
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNELS = [
    # Основные IT каналы
    'remote_it_jobs', 'devjobs', 'finder_jobs', 'tproger_official',
    'getitru', 'fordev', 'helocareer', 'myresume_jobs',
    'ukrainiandevjobs', 'djinni_jobs_all',
    # Remote/Freelance
    'remotework', 'itfreelance', 'remotejobss', 'Remotelist', 'RemoteIT',
    'Remote_Jobs_Channel', 'remote_jobs_relocate', 'Relocation_Jobs',
    # Frontend/Backend/Fullstack
    'frontend_jobs', 'backend_jobs', 'fullstack_jobs',
    'frontend_jobs_ru', 'backend_jobs_ru', 'fullstack_jobs_ru',
    'javascript_jobs', 'forwebjob', 'frontend_job_offers',
    # Языки программирования
    'python_jobs', 'python_jobs_ru', 'php_jobs', 'java_jobs_ru',
    'dotnet_jobs_ru', 'proJVMjobs',
    # DevOps/QA/Data
    'devops_jobs', 'devops_sre_jobs', 'qa_jobs', 'qa_jobs_ru',
    'data_jobs', 'data_science_jobs_ru', 'analyst_jobs_ru', 'it_analytics_jobs',
    # Mobile/GameDev
    'mobile_developer_jobs', 'mobile_jobs_ru', 'game_dev_jobs', 'gamedev_jobs_ru',
    # Design/Creative
    'design_work', 'creative_jobs', 'graphic_jobs', 'web_designers',
    'designers_jobs', 'uiux_jobs', 'rabota_dizajnera',
    'vakansii_dlja_dizajnerov', 'ux_ui_jobs_ru',
    # Marketing/SMM/Content
    'smm_jobs', 'smm_jobs_ru', 'marketing_jobs', 'marketing_jobs_ru',
    'content_managers', 'copywriting_jobs_ru', 'writing_jobs',
    'Work4writers', 'work_editor', 'redachredach', 'adgoodashell',
    'Digital_Marketing_Jobs', 'seo_vacancies',
    # Product/Project Management
    'product_jobs', 'productjobgo', 'productvacancy', 'hireproproduct',
    'forproducts', 'productmanagers_jobs', 'pm_jobs_ru', 'project_managers',
    # HR/Recruiting
    'hr_job', 'therecruitmenthub', 'talentnetwork', 'huggabletalents',
    'careerspace', 'cozy_hr',
    # Entry Level/Education
    'entry_level_jobs', 'remote_education_jobs', 'edujobs', 'go_careers',
    'jobsinternshipswale', 'offcampus_phodenge', 'Campus_Placements',
    'akashthedeveloper', 'riddhi_dutta',
    # Other Industries
    'legal_remote_jobs', 'finance_remote_jobs', 'consulting_remote_jobs',
    'customer_support_jobs', 'digital_nomads', 'Crypto_Freelancers',
    'it_freelancer_outsourcing',
    # Russian job channels
    'it_jobs_ru', 'it_vakansii_jobs', 'budujobs', 'careerwithh',
    'vacanciesbest', 'mnogovakansiy', 'evacuatejobs', 'zarubezhom_jobs',
    'digital_rabota', 'mirkreatorovjob', 'dddwork', 'antirabstvoru',
    'yojob', 'perezvonyu', 'time2find', 'locale_jobs',
    # International
    'Hub_Jobs', 'job_board', 'getjobss', 'Jobs_A_to_Z', 'findITJobsLink',
    'jobs_usa_uk', 'USA_Jobs_Channel', 'usa_jobs_channel',
    'india_jobs_channel', 'VRS_Jobs', 'Mechanical_Engineering_Jobs', 'SalesforceA',
    # Singapore
    'sgparttimers', 'snapjobssg', 'jobprop', 'jobhitchpt', 'searchforjob',
]

VACANCIES_FILE = 'bot/telegram_vacancies.json'

JOB_KEYWORDS = [
    'вакансия', 'ищем', 'hiring', 'требуется', 'нужен', 'открыта позиция',
    'junior', 'middle', 'senior', 'lead', 'разработчик', 'developer',
    'менеджер', 'manager', 'аналитик', 'analyst', 'дизайнер', 'designer',
    'тестировщик', 'qa', 'devops', 'frontend', 'backend', 'fullstack',
    'python', 'java', 'javascript', 'react', 'vue', 'angular', 'node',
    'product', 'project', 'pm', 'hr', 'recruiter', 'зарплата', 'salary',
    'оклад', 'remote', 'удалённ', 'удаленн'
]

SALARY_PATTERN = re.compile(
    r'(?:от\s*)?(\d+[\s,.]?\d*)\s*(?:[-–—до]\s*(\d+[\s,.]?\d*))?\s*(?:тыс|k|к|₽|руб|rub|\$|usd|eur)?',
    re.IGNORECASE
)

def load_vacancies():
    try:
        with open(VACANCIES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []

def save_vacancies(vacancies):
    with open(VACANCIES_FILE, 'w', encoding='utf-8') as f:
        json.dump(vacancies, f, ensure_ascii=False, indent=2)


def save_to_sqlite(vacancies):
    db_conn = sqlite3.connect("bot/vacancies.db")
    cur = db_conn.cursor()
    cur.execute("""
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
    for vac in vacancies:
        cur.execute("""
        INSERT OR IGNORE INTO telegram_vacancies
        (id, name, employer, salary, url, area, full_text, parsed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            vac.get("id", ""),
            vac.get("name", ""),
            vac.get("employer", {}).get("name", "") if isinstance(vac.get("employer"), dict) else str(vac.get("employer", "")),
            json.dumps(vac.get("salary")) if vac.get("salary") else "",
            vac.get("alternate_url", ""),
            vac.get("area", {}).get("name", "") if isinstance(vac.get("area"), dict) else str(vac.get("area", "")),
            vac.get("full_text", ""),
            vac.get("parsed_at", "")
        ))
    db_conn.commit()
    db_conn.close()
    logger.info(f"Saved {len(vacancies)} vacancies to SQLite")

def extract_salary(text):
    match = SALARY_PATTERN.search(text)
    if match:
        try:
            sal_from = int(match.group(1).replace(' ', '').replace(',', '').replace('.', ''))
            sal_to = int(match.group(2).replace(' ', '').replace(',', '').replace('.', '')) if match.group(2) else None
            if sal_from < 1000:
                sal_from *= 1000
            if sal_to and sal_to < 1000:
                sal_to *= 1000
            return {'from': sal_from, 'to': sal_to, 'currency': 'RUR'}
        except:
            pass
    return None

def extract_job_title(text):
    lines = text.split('\n')
    for line in lines[:10]:
        line = line.strip()
        if line.startswith('#') or line.startswith('@') or len(line) < 5:
            continue
        if line.startswith('http'):
            continue
        cleaned = re.sub(r'[#@][\w]+', '', line).strip()
        cleaned = re.sub(r'^[\s\-\–\—\•\*:]+', '', cleaned).strip()
        if 5 < len(cleaned) < 80:
            return cleaned[:80]
    cleaned_text = re.sub(r'[#@][\w]+', '', text).strip()
    first_line = cleaned_text.split('\n')[0].strip()
    if 5 < len(first_line) < 80:
        return first_line
    return text[:60].replace('#', '').replace('@', '') + '...'

def is_job_posting(text):
    if not text or len(text) < 50:
        return False
    text_lower = text.lower()
    keyword_count = sum(1 for kw in JOB_KEYWORDS if kw in text_lower)
    return keyword_count >= 1

def is_remote(text):
    remote_keywords = ['remote', 'удалённ', 'удаленн', 'дистанц', 'из дома', 'home office']
    text_lower = text.lower()
    return any(kw in text_lower for kw in remote_keywords)

def extract_company(text):
    patterns = [
        r'компания[:\s]+([А-Яа-яA-Za-z0-9\s]+)',
        r'в\s+([A-Z][A-Za-z0-9]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            company = match.group(1).strip()
            if 3 < len(company) < 30:
                return company
    return 'Telegram'

async def parse_channel_web(session, channel):
    url = f"https://t.me/s/{channel}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status != 200:
                logger.error(f"Failed to fetch {channel}: {response.status}")
                return []
            html = await response.text()
        soup = BeautifulSoup(html, 'html.parser')
        messages = soup.find_all('div', class_='tgme_widget_message_wrap')
        vacancies = []
        for msg in messages:
            text_div = msg.find('div', class_='tgme_widget_message_text')
            if not text_div:
                continue
            text = text_div.get_text(separator='\n', strip=True)
            if not is_job_posting(text):
                continue
            link_tag = msg.find('a', class_='tgme_widget_message_date')
            msg_url = link_tag['href'] if link_tag else url
            msg_id = msg_url.split('/')[-1] if msg_url else '0'
            vacancy = {
                'id': f"tg_{channel}_{msg_id}",
                'name': extract_job_title(text),
                'employer': {'name': extract_company(text)},
                'salary': extract_salary(text),
                'alternate_url': msg_url,
                'area': {'name': 'Remote' if is_remote(text) else 'Россия'},
                'source': 'telegram',
                'channel': f"@{channel}",
                'text_hash': text[:100],
                'full_text': text[:1000],
                'parsed_at': datetime.now().isoformat()
            }
            vacancies.append(vacancy)
        logger.info(f"Parsed {len(vacancies)} vacancies from @{channel}")
        return vacancies
    except Exception as e:
        logger.error(f"Error parsing {channel}: {e}")
        return []

async def parse_all_channels():
    logger.info("Starting web parser...")
    existing = load_vacancies()
    existing_hashes = set(v.get('text_hash', '')[:100] for v in existing)
    all_new_vacancies = []
    async with aiohttp.ClientSession(headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }) as session:
        for channel in CHANNELS:
            try:
                vacancies = await parse_channel_web(session, channel)
                for vac in vacancies:
                    if vac['text_hash'] not in existing_hashes:
                        all_new_vacancies.append(vac)
                        existing_hashes.add(vac['text_hash'])
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error with {channel}: {e}")
    combined = all_new_vacancies + existing
    combined = combined[:500]
    save_vacancies(combined)
    save_to_sqlite(combined)
    logger.info(f"Total: {len(all_new_vacancies)} new, {len(combined)} stored")

async def main():
    await parse_all_channels()
    logger.info("Parsing complete")

if __name__ == '__main__':
    asyncio.run(main())
