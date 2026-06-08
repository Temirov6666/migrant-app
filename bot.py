import asyncio
import logging
import logging.handlers
import os
import hashlib
import aiosqlite
import random
import base64
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from employer_requests_handlers import router as er_router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import anthropic

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "@migr_pomoshnik")
GROUP_ID = os.getenv("GROUP_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DB_PATH = "migr_bot.db"


# ─── Production logging ─────────────────────────────────────────────────────
_log_fmt = logging.Formatter(
    fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
)
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_sh = logging.StreamHandler()
_sh.setFormatter(_log_fmt)
_root.addHandler(_sh)
_fh = logging.handlers.RotatingFileHandler(
    "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setFormatter(_log_fmt)
_root.addHandler(_fh)
# Убираем спам от библиотек
for _lib in ("aiogram", "aiosqlite", "asyncio", "httpcore", "httpx"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ФИКС #1: AsyncAnthropic вместо Anthropic (синхронный клиент крашил async функции)
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

pending_posts = {}

# ============================================================
# ТИПЫ ДОКУМЕНТОВ
# ============================================================
DOC_TYPES = {
    "patent":  {"name": "🪪 Патент на работу",    "days": 365,  "warn_days": 30, "emoji": "🪪"},
    "rvp":     {"name": "📘 РВП",                  "days": 1095, "warn_days": 60, "emoji": "📘"},
    "rvpo":    {"name": "📗 РВПО",                 "days": 1095, "warn_days": 60, "emoji": "📗"},
    "vnj":     {"name": "🏠 ВНЖ",                  "days": 1825, "warn_days": 90, "emoji": "🏠"},
    "mk":      {"name": "📄 Миграционная карта",   "days": 90,   "warn_days": 14, "emoji": "📄"},
    "other":   {"name": "📋 Другой документ",      "days": 365,  "warn_days": 30, "emoji": "📋"},
}

# ============================================================
# ТАРИФЫ STARS
# ============================================================
STAR_PLANS = {
    "1m":  {"stars": 100,  "days": 30,  "label": "1 месяц"},
    "2m":  {"stars": 190,  "days": 60,  "label": "2 месяца"},
    "3m":  {"stars": 270,  "days": 90,  "label": "3 месяца"},
    "6m":  {"stars": 490,  "days": 180, "label": "6 месяцев"},
    "12m": {"stars": 850,  "days": 365, "label": "12 месяцев"},
}

B2B_PLANS = {
    "b2b_10":  {"stars": 500,  "days": 30, "limit": 10,  "label": "До 10 сотрудников"},
    "b2b_30":  {"stars": 1200, "days": 30, "limit": 30,  "label": "До 30 сотрудников"},
    "b2b_100": {"stars": 3000, "days": 30, "limit": 100, "label": "До 100 сотрудников"},
}

# ============================================================
# ЯЗЫКИ
# ============================================================
LANG_PROMPTS = {
    "ru": "Ты опытный миграционный юрист в России. Отвечай на русском языке. Дай развёрнутый полезный ответ.",
    "uz": "Siz Rossiyada tajribali migratsiya huquqshunosis. O'zbek tilida to'liq javob bering.",
    "tj": "Шумо юристи муҳоҷиратии Россия ҳастед. Ба тоҷикӣ ҷавоб диҳед.",
    "kz": "Сіз Ресейдегі миграция заңгерісіз. Қазақ тілінде жауап беріңіз.",
    "ky": "Сиз Россиядагы миграция юристисиз. Кыргыз тилинде жооп бериңиз.",
    "az": "Siz Rusiyada miqrasiya hüquqşünasısınız. Azərbaycan dilində cavab verin.",
}

THINKING_MSGS = {
    "ru": "⏳ Анализирую вопрос...",
    "uz": "⏳ Tahlil qilyapman...",
    "tj": "⏳ Таҳлил мекунам...",
    "kz": "⏳ Талдаймын...",
    "ky": "⏳ Талдап жатам...",
    "az": "⏳ Təhlil edirəm...",
}

# ФИКС #2: Расширенный fallback на все языки
FALLBACK_MSGS = {
    "ru": (
        "🤖 <b>AI-юрист временно недоступен.</b>\n\n"
        "📞 Задайте вопрос напрямую — юрист ответит в течение 2 часов.\n\n"
        "Или запишитесь на консультацию 👇"
    ),
    "uz": (
        "🤖 <b>AI-yurist vaqtincha mavjud emas.</b>\n\n"
        "📞 Savolingizni to'g'ridan-to'g'ri yuboring — yurist 2 soat ichida javob beradi. 👇"
    ),
    "tj": (
        "🤖 <b>AI-юрист муваққатан дастрас нест.</b>\n\n"
        "📞 Саволро мустақиман фиристед — юрист дар давоми 2 соат ҷавоб медиҳад. 👇"
    ),
    "kz": (
        "🤖 <b>AI-заңгер уақытша қолжетімсіз.</b>\n\n"
        "📞 Сұрақты тікелей жіберіңіз — заңгер 2 сағат ішінде жауап береді. 👇"
    ),
    "ky": (
        "🤖 <b>AI-юрист убактылуу жеткиликсиз.</b>\n\n"
        "📞 Суроңузду түз жөнөтүңүз — юрист 2 саат ичинде жооп берет. 👇"
    ),
    "az": (
        "🤖 <b>AI-hüquqşünas müvəqqəti əlçatmazdır.</b>\n\n"
        "📞 Sualınızı birbaşa göndərin — hüquqşünas 2 saat ərzində cavab verəcək. 👇"
    ),
}

LANG_KEYWORDS = {
    "uz": ["rahmat", "salom", "nima", "qanday", "men", "sizga", "kerak", "ishlab", "ruxsat", "hujjat", "ўзбек", "тошкент"],
    "tj": ["ман", "шумо", "ташаккур", "хайр", "чӣ", "кадом", "ҳуҷҷат", "кор", "иҷозат", "тоҷик", "душанбе"],
    "kz": ["рахмет", "жарайды", "қалай", "құжат", "қазақ", "алматы", "астана", "жұмыс"],
    "ky": ["рахмат", "кандай", "кыргыз", "бишкек", "иштөө"],
    "az": ["sağ ol", "necə", "sənəd", "azərbaycan", "bakı", "işləmək"],
}

def detect_language(text: str) -> str:
    text_lower = text.lower()
    scores = {}
    for lang, keywords in LANG_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[lang] = score
    return max(scores, key=scores.get) if scores else "ru"

# ============================================================
# ТЕМЫ ДЛЯ AI ПОСТОВ
# ============================================================
AI_POST_TOPICS = [
    "Как правильно оплатить патент на работу в России в 2025 году чтобы он не аннулировался",
    "Что делать если просрочен патент на работу — пошаговая инструкция",
    "Как получить РВП в России: полный список документов в 2025",
    "Новые правила регистрации иностранных граждан в России",
    "Как получить ВНЖ после патента: когда и как подавать документы",
    "Права иностранного работника в России при задержке зарплаты",
    "Как проверить запрет на въезд в Россию через МВД",
    "Что такое РВПО и кто может его получить в 2025 году",
    "Как уведомить МВД о трудоустройстве иностранного гражданина",
    "Штрафы за нарушение миграционного законодательства в 2025",
    "Миграционная карта: как правильно заполнить и сколько действует",
    "Как продлить патент на работу без выезда из России",
    "Какие документы нужны узбеку для работы в Москве в 2025",
    "Медицинское освидетельствование для патента: где пройти и сколько стоит",
    "Как получить СНИЛС и ИНН иностранному гражданину",
    "Что делать если работодатель не оформляет официально",
    "Как сменить работодателя с патентом на работу",
    "Правила 90/180 дней: как правильно считать срок пребывания",
    "Ответственность за нелегальную работу в России — штрафы и депортация",
    "Как узбекистанцу получить гражданство России в 2025",
]

# ============================================================
# СЦЕНАРИИ "ЧТО ДЕЛАТЬ ЕСЛИ..."
# ============================================================
WHAT_IF_SCENARIOS = {
    "wif_patent_lost": {
        "title": "Потерял патент на работу",
        "text": (
            "📋 <b>Потерял патент — что делать:</b>\n\n"
            "1️⃣ <b>Не паникуй</b> — работать пока нельзя\n"
            "2️⃣ <b>Идите в МФЦ</b> с паспортом и заявлением об утере\n"
            "3️⃣ <b>Напишите заявление</b> на выдачу дубликата\n"
            "4️⃣ <b>Оплатите госпошлину</b> — ~1 600 руб.\n"
            "5️⃣ <b>Срок выдачи</b> — до 10 рабочих дней\n\n"
            "⚠️ <b>Важно:</b> Продолжайте оплачивать НДФЛ пока восстанавливаете!\n"
            "Неоплата даже 1 месяца = аннулирование патента."
        )
    },
    "wif_expired": {
        "title": "Просрочен патент",
        "text": (
            "🔴 <b>Патент просрочен — срочные действия:</b>\n\n"
            "1️⃣ <b>Немедленно прекратите работу</b> — штраф до 7 000 руб.\n"
            "2️⃣ <b>Вариант 1 — продление:</b>\n"
            "   • Если просрочка менее 1 года — можно продлить через МФЦ\n"
            "   • Доплатите пропущенные месяцы НДФЛ\n"
            "3️⃣ <b>Вариант 2 — выезд/въезд:</b>\n"
            "   • Выехать из России\n"
            "   • Въехать заново с отметкой «работа» в миграционной карте\n"
            "   • Получить новый патент\n\n"
            "⚡ <b>Нужна срочная консультация юриста!</b>"
        )
    },
    "wif_reg_expired": {
        "title": "Просрочена регистрация",
        "text": (
            "⚠️ <b>Просрочена регистрация — что делать:</b>\n\n"
            "1️⃣ <b>Срочно обратитесь к работодателю</b> — он должен зарегистрировать вас\n"
            "2️⃣ <b>Или в МФЦ/УВМ МВД</b> для продления регистрации\n"
            "3️⃣ <b>Документы:</b> паспорт + патент/РВП + договор аренды/от работодателя\n\n"
            "💰 <b>Штрафы:</b>\n"
            "• За вас: 2 000 — 5 000 руб.\n"
            "• За работодателя/арендодателя: до 500 000 руб.\n\n"
            "⏰ Регистрация должна совпадать с местом фактического проживания!"
        )
    },
    "wif_ban": {
        "title": "Обнаружен запрет на въезд",
        "text": (
            "🚫 <b>Запрет на въезд — порядок действий:</b>\n\n"
            "1️⃣ <b>Узнайте причину</b> — запрос через МВД или юриста\n"
            "2️⃣ <b>Частые причины:</b>\n"
            "   • Просрочка пребывания более 30 дней\n"
            "   • Неоплаченные штрафы\n"
            "   • Нарушение миграционного режима\n"
            "3️⃣ <b>Обжалование:</b>\n"
            "   • Жалоба в ГУВМ МВД — 10 дней\n"
            "   • Административный суд\n"
            "   • Через российского супруга/ребёнка — смягчающие обстоятельства\n\n"
            "⚡ <b>Нужен юрист — обжалование сложное!</b>"
        )
    },
    "wif_employer": {
        "title": "Работодатель не платит / кинул",
        "text": (
            "💼 <b>Работодатель не платит зарплату:</b>\n\n"
            "1️⃣ <b>Письменное требование</b> — отправьте заказным письмом\n"
            "2️⃣ <b>Трудовая инспекция</b> — жалоба онлайн: онлайнинспекция.рф\n"
            "3️⃣ <b>Прокуратура</b> — заявление о невыплате зарплаты\n"
            "4️⃣ <b>Суд</b> — иск о взыскании (госпошлину не платите — трудовые споры)\n\n"
            "⚠️ <b>Важно для мигрантов:</b>\n"
            "Иностранец имеет те же права по ТК РФ что и гражданин!\n"
            "Незаконное увольнение за обращение в инспекцию = отдельный штраф работодателю.\n\n"
            "📞 <b>Горячая линия Роструда:</b> 8-800-707-88-41 (бесплатно)"
        )
    },
    "wif_changed_employer": {
        "title": "Сменил работодателя",
        "text": (
            "🔄 <b>Сменил работодателя — что нужно сделать:</b>\n\n"
            "1️⃣ <b>Уведомить МВД</b> о расторжении прежнего договора — 3 рабочих дня\n"
            "2️⃣ <b>Уведомить МВД</b> о новом трудоустройстве — 3 рабочих дня\n"
            "3️⃣ <b>Проверить регион патента</b> — работать можно только в регионе выдачи!\n"
            "4️⃣ <b>Переоформить регистрацию</b> если поменяли адрес\n\n"
            "⚠️ <b>Внимание:</b> При смене региона — нужен НОВЫЙ патент!\n"
            "Продолжайте платить НДФЛ ежемесячно!"
        )
    },
}

# ============================================================
# HEALTH SCORE — МИГРАЦИОННЫЙ СТАТУС
# ============================================================
async def calculate_health_score(user_id: int) -> dict:
    """Считает Migration Health Score пользователя"""
    docs = await db_get_docs(user_id)
    now = datetime.now()
    score = 100
    issues = []
    tips = []

    if not docs:
        return {
            "score": 0,
            "color": "⚪",
            "label": "Нет данных",
            "issues": ["Документы не добавлены"],
            "tips": ["Добавьте ваши документы для мониторинга"],
            "next_action": "Добавить документ"
        }

    for d in docs:
        dt = DOC_TYPES.get(d["doc_type"], DOC_TYPES["patent"])
        try:
            exp = datetime.strptime(d["expiry_date"], "%d.%m.%Y")
            dl = (exp - now).days
            if dl < 0:
                score -= 40
                issues.append(f"🔴 {dt['emoji']} {dt['name']} ПРОСРОЧЕН на {abs(dl)} дней!")
                tips.append(f"Срочно обратитесь к юристу по {dt['name']}")
            elif dl <= 7:
                score -= 30
                issues.append(f"🔴 {dt['emoji']} {dt['name']} истекает через {dl} дней!")
                tips.append(f"Немедленно занимайтесь продлением {dt['name']}")
            elif dl <= 14:
                score -= 20
                issues.append(f"🟠 {dt['emoji']} {dt['name']} — осталось {dl} дней")
                tips.append(f"Подготовьте документы для продления {dt['name']}")
            elif dl <= 30:
                score -= 10
                issues.append(f"🟡 {dt['emoji']} {dt['name']} — осталось {dl} дней")
                tips.append(f"Запланируйте продление {dt['name']}")
        except Exception as e:
            log.warning(f"Health score parse error for doc {d.get('id')}: {e}")

    score = max(0, score)

    if score >= 80:
        color, label = "🟢", "Всё в порядке"
    elif score >= 60:
        color, label = "🟡", "Требует внимания"
    elif score >= 40:
        color, label = "🟠", "Есть риски"
    else:
        color, label = "🔴", "Критическая ситуация"

    next_action = tips[0] if tips else "Документы в порядке ✅"

    return {
        "score": score,
        "color": color,
        "label": label,
        "issues": issues if issues else ["✅ Нет проблем"],
        "tips": tips if tips else ["Все документы в норме"],
        "next_action": next_action
    }

# ============================================================
# AI ROADMAP — ПЕРСОНАЛЬНЫЙ МАРШРУТ
# ============================================================
async def build_ai_roadmap(user_id: int) -> str:
    """Строит персональный чеклист действий"""
    docs = await db_get_docs(user_id)
    user = await db_get_user(user_id)
    now = datetime.now()

    steps = []
    urgent = []

    for d in docs:
        dt = DOC_TYPES.get(d["doc_type"], DOC_TYPES["patent"])
        try:
            exp = datetime.strptime(d["expiry_date"], "%d.%m.%Y")
            dl = (exp - now).days
            if dl < 0:
                urgent.append(f"🔴 Просрочен {dt['emoji']} {dt['name']} — срочно к юристу!")
            elif dl <= 30:
                urgent.append(f"🟠 Продлить {dt['emoji']} {dt['name']} — через {dl} дней!")
        except Exception as e:
            log.warning(f"Roadmap parse error: {e}")

    # Общие шаги
    has_patent = any(d["doc_type"] == "patent" for d in docs)
    has_rvp = any(d["doc_type"] in ["rvp", "rvpo"] for d in docs)
    has_vnj = any(d["doc_type"] == "vnj" for d in docs)

    if not docs:
        steps.append("➕ Добавьте ваши документы в бот")
        steps.append("📅 Укажите дату въезда в Россию")

    if has_patent and not has_rvp and not has_vnj:
        steps.append("📘 Узнайте про РВП — следующий шаг после патента")
        steps.append("📬 Убедитесь что работодатель уведомил МВД о вас")

    if has_rvp:
        steps.append("🏠 Планируйте подачу на ВНЖ (через 3 года РВП)")

    steps.append("🔍 Проверьте статус в реестре контролируемых лиц")
    steps.append("🧾 Сохраните чеки оплаты НДФЛ за все месяцы")
    steps.append("📍 Убедитесь что регистрация актуальна")

    text = ""
    if urgent:
        text += "⚡ <b>СРОЧНО:</b>\n"
        text += "\n".join(urgent) + "\n\n"

    text += "📋 <b>Ваш план действий:</b>\n"
    for i, step in enumerate(steps[:6], 1):
        text += f"{i}. {step}\n"

    return text

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                phone TEXT,
                city TEXT,
                citizenship TEXT,
                lang TEXT DEFAULT 'ru',
                user_type TEXT DEFAULT 'personal',
                company_name TEXT,
                sub_until TEXT,
                b2b_plan TEXT,
                b2b_limit INTEGER DEFAULT 0,
                stars_total INTEGER DEFAULT 0,
                ai_daily_count INTEGER DEFAULT 0,
                ai_last_date TEXT,
                channel_subscribed INTEGER DEFAULT 0,
                channel_checked_at TEXT,
                source TEXT DEFAULT 'direct',
                total_questions INTEGER DEFAULT 0,
                last_active TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                owner_type TEXT DEFAULT 'personal',
                employee_id INTEGER DEFAULT 0,
                doc_type TEXT DEFAULT 'patent',
                series TEXT,
                number TEXT,
                blank_series TEXT,
                blank_number TEXT,
                issue_date TEXT,
                expiry_date TEXT,
                region TEXT,
                last_check_status TEXT,
                last_checked TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employer_id INTEGER,
                full_name TEXT,
                citizenship TEXT,
                passport_series TEXT,
                passport_number TEXT,
                position TEXT,
                hired_date TEXT,
                notes TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan TEXT,
                stars INTEGER,
                days INTEGER,
                activated_at TEXT,
                expires_at TEXT,
                charge_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                file_id TEXT,
                description TEXT,
                amount TEXT,
                receipt_date TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                doc_id INTEGER,
                remind_at TEXT,
                sent INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                bonus_days INTEGER DEFAULT 7,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS consultations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT, phone TEXT, city TEXT,
                topic TEXT, urgency TEXT, created_at TEXT
            )
        """)
        # ФИКС #3: Персистентный seen_news вместо set() который сбрасывался при рестарте
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_hash TEXT UNIQUE,
                created_at TEXT
            )
        """)
        # ── АНАЛИТИКА ПОЛЬЗОВАТЕЛЕЙ ──────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                details TEXT,
                created_at TEXT
            )
        """)
        # ── OCR РЕЗУЛЬТАТЫ ────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ocr_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                doc_type TEXT,
                raw_text TEXT,
                extracted_json TEXT,
                confidence TEXT,
                doc_id INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, company TEXT, city TEXT,
                salary_from INTEGER DEFAULT 0,
                salary_to INTEGER DEFAULT 0,
                description TEXT, requirements TEXT,
                doc_types TEXT, housing INTEGER DEFAULT 0,
                schedule TEXT, contact TEXT,
                active INTEGER DEFAULT 1,
                views INTEGER DEFAULT 0, created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS job_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER, user_id INTEGER,
                name TEXT, phone TEXT,
                doc_type TEXT, created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS housing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, city TEXT,
                price INTEGER DEFAULT 0,
                rooms TEXT, address TEXT,
                description TEXT, contact TEXT,
                active INTEGER DEFAULT 1,
                views INTEGER DEFAULT 0, created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS housing_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                housing_id INTEGER, user_id INTEGER,
                name TEXT, phone TEXT, created_at TEXT
            )
        """)
        await db.commit()
    log.info("БД инициализирована")

async def db_register_user(user: types.User, source: str = "direct"):
    """Полная регистрация пользователя с трекингом источника."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR IGNORE INTO users
                  (user_id, name, username, source, last_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user.id, user.full_name, user.username, source, now, now))
            # Обновляем last_active при каждом входе
            await db.execute(
                "UPDATE users SET last_active=? WHERE user_id=?",
                (now, user.id)
            )
            await db.commit()
    except Exception as e:
        log.error(f"db_register_user error for {user.id}: {e}")


async def track_action(user_id: int, action: str, details: str = ""):
    """Записываем действие пользователя для аналитики."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO user_analytics (user_id, action, details, created_at) VALUES (?,?,?,?)",
                (user_id, action, details[:500], datetime.now().strftime("%d.%m.%Y %H:%M"))
            )
            await db.execute(
                "UPDATE users SET last_active=? WHERE user_id=?",
                (datetime.now().strftime("%d.%m.%Y %H:%M"), user_id)
            )
            await db.commit()
    except Exception as e:
        log.warning(f"track_action error for {user_id}: {e}")


async def check_channel_subscription(user_id: int) -> bool:
    """Проверяем подписку на канал через Telegram API."""
    if not CHANNEL_ID or CHANNEL_ID == "@migr_pomoshnik":
        return True  # Канал не настроен — пропускаем
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        is_sub = member.status not in ("left", "kicked", "restricted")
        # Сохраняем статус в БД
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET channel_subscribed=?, channel_checked_at=? WHERE user_id=?",
                (1 if is_sub else 0, datetime.now().strftime("%d.%m.%Y %H:%M"), user_id)
            )
            await db.commit()
        return is_sub
    except Exception as e:
        log.warning(f"check_channel_subscription error for {user_id}: {e}")
        return True  # При ошибке не блокируем пользователя


async def save_ocr_result(user_id: int, doc_type: str, raw_text: str,
                           extracted: dict, confidence: str = "medium") -> int | None:
    """Сохраняем результат OCR в БД."""
    import json
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO ocr_results
                   (user_id, doc_type, raw_text, extracted_json, confidence, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, doc_type, raw_text[:2000],
                 json.dumps(extracted, ensure_ascii=False),
                 confidence, datetime.now().strftime("%d.%m.%Y %H:%M"))
            )
            await db.commit()
            return cur.lastrowid
    except Exception as e:
        log.error(f"save_ocr_result error for {user_id}: {e}")
        return None

async def db_get_user(user_id: int):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    return dict(zip(cols, row))
    except Exception as e:
        log.error(f"db_get_user error: {e}")
    return None

async def db_update_user(user_id: int, **kwargs):
    try:
        fields = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values()) + [user_id]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE users SET {fields} WHERE user_id=?", values)
            await db.commit()
    except Exception as e:
        log.error(f"db_update_user error: {e}")

async def db_get_docs(user_id: int, owner_type: str = "personal"):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT * FROM documents WHERE user_id=? AND owner_type=? ORDER BY id DESC",
                (user_id, owner_type)
            ) as cur:
                rows = await cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log.error(f"db_get_docs error: {e}")
    return []

async def db_add_doc(user_id, doc_type, series, number, blank_series, blank_number,
                     issue_date, expiry_date, region, owner_type="personal", employee_id=0):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                INSERT INTO documents (user_id, owner_type, employee_id, doc_type, series, number,
                blank_series, blank_number, issue_date, expiry_date, region, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (user_id, owner_type, employee_id, doc_type, series, number,
                  blank_series, blank_number, issue_date, expiry_date, region,
                  datetime.now().strftime("%d.%m.%Y %H:%M")))
            doc_id = cur.lastrowid

            # Напоминания — с нормальным логированием
            try:
                exp = datetime.strptime(expiry_date, "%d.%m.%Y")
                dt = DOC_TYPES.get(doc_type, DOC_TYPES["patent"])
                for d in [dt["warn_days"], dt["warn_days"] // 2, 7]:
                    remind_at = (exp - timedelta(days=d)).strftime("%d.%m.%Y")
                    await db.execute(
                        "INSERT INTO reminders (user_id, doc_id, remind_at) VALUES (?,?,?)",
                        (user_id, doc_id, remind_at)
                    )
            except Exception as e:
                log.warning(f"Reminder creation error for doc {doc_id}: {e}")

            await db.commit()
            return doc_id
    except Exception as e:
        log.error(f"db_add_doc error: {e}")
        return None

async def db_get_employees(employer_id: int):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT * FROM employees WHERE employer_id=? ORDER BY full_name", (employer_id,)) as cur:
                rows = await cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log.error(f"db_get_employees error: {e}")
    return []

async def db_add_employee(employer_id, full_name, citizenship, passport_series,
                          passport_number, position, hired_date, notes=""):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                INSERT INTO employees (employer_id, full_name, citizenship, passport_series,
                passport_number, position, hired_date, notes, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (employer_id, full_name, citizenship, passport_series,
                  passport_number, position, hired_date, notes,
                  datetime.now().strftime("%d.%m.%Y %H:%M")))
            await db.commit()
            return cur.lastrowid
    except Exception as e:
        log.error(f"db_add_employee error: {e}")
        return None

async def db_count_users():
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                r = await cur.fetchone()
                return r[0] if r else 0
    except Exception as e:
        log.error(f"db_count_users error: {e}")
        return 0

async def check_subscription(user_id: int) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT sub_until FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    return datetime.strptime(row[0], "%d.%m.%Y") > datetime.now()
    except Exception as e:
        log.error(f"check_subscription error: {e}")
    return False

# ── FREE лимит: 3 вопроса в день ──────────────────────────────────────────────
FREE_AI_LIMIT = 3

async def check_and_increment_ai_limit(user_id: int) -> dict:
    """
    Возвращает:
      allowed=True  — можно задать вопрос
      allowed=False — лимит исчерпан
      is_pro=True   — PRO пользователь (без лимита)
      used / limit  — для отображения
    """
    is_pro = await check_subscription(user_id)
    if is_pro:
        return {"allowed": True, "is_pro": True, "used": 0, "limit": 999}

    today = datetime.now().strftime("%d.%m.%Y")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT ai_daily_count, ai_last_date FROM users WHERE user_id=?",
                (user_id,)
            ) as cur:
                row = await cur.fetchone()

            count = row[0] if row else 0
            last_date = row[1] if row else None

            # Новый день — сбрасываем счётчик
            if last_date != today:
                count = 0
                await db.execute(
                    "UPDATE users SET ai_daily_count=0, ai_last_date=? WHERE user_id=?",
                    (today, user_id)
                )
                await db.commit()

            if count >= FREE_AI_LIMIT:
                return {"allowed": False, "is_pro": False, "used": count, "limit": FREE_AI_LIMIT}

            # Инкрементируем
            await db.execute(
                "UPDATE users SET ai_daily_count=ai_daily_count+1, ai_last_date=? WHERE user_id=?",
                (today, user_id)
            )
            await db.commit()
            return {"allowed": True, "is_pro": False, "used": count + 1, "limit": FREE_AI_LIMIT}
    except Exception as e:
        log.error(f"check_ai_limit error for {user_id}: {e}")
        return {"allowed": True, "is_pro": False, "used": 0, "limit": FREE_AI_LIMIT}

async def is_topic_seen(topic: str) -> bool:
    """ФИКС: Персистентная проверка дубликатов через БД"""
    topic_hash = hashlib.md5(topic.encode()).hexdigest()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id FROM seen_news WHERE topic_hash=?", (topic_hash,)) as cur:
                return await cur.fetchone() is not None
    except Exception as e:
        log.error(f"is_topic_seen error: {e}")
    return False

async def mark_topic_seen(topic: str):
    topic_hash = hashlib.md5(topic.encode()).hexdigest()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO seen_news (topic_hash, created_at) VALUES (?,?)",
                (topic_hash, datetime.now().strftime("%d.%m.%Y %H:%M"))
            )
            await db.commit()
    except Exception as e:
        log.error(f"mark_topic_seen error: {e}")

# ============================================================
# FSM СОСТОЯНИЯ
# ============================================================
class QuestionState(StatesGroup):
    waiting = State()

class ConsultState(StatesGroup):
    name = State()
    phone = State()
    city = State()
    topic = State()
    urgency = State()

class AddDocState(StatesGroup):
    doc_type = State()
    series = State()
    number = State()
    blank_series = State()
    blank_number = State()
    issue_date = State()
    expiry_date = State()
    region = State()

class AddEmployeeState(StatesGroup):
    full_name = State()
    citizenship = State()
    passport = State()
    position = State()
    hired_date = State()

class AddEmpDocState(StatesGroup):
    employee_id = State()
    doc_type = State()
    series = State()
    number = State()
    issue_date = State()
    expiry_date = State()
    region = State()

class BroadcastState(StatesGroup):
    message = State()

class CalcState(StatesGroup):
    doc_type = State()
    issue_date = State()

class ChecklistState(StatesGroup):
    citizenship = State()
    goal = State()

# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def safe_url(handle: str) -> str:
    clean = handle.replace("@", "").strip()
    return f"https://t.me/{clean}" if all(ord(c) < 128 for c in clean) else f"https://t.me/{CHANNEL_ID.replace('@', '')}"

def main_menu(user_type: str = "personal"):
    ch = safe_url(CHANNEL_ID)
    gr = safe_url(GROUP_ID) if GROUP_ID else ch
    # Получаем ссылку на Mini App
    rows = [
        [InlineKeyboardButton(text="❓ Задать вопрос AI-юристу", callback_data="question")],
        [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
        [InlineKeyboardButton(text="🆘 Что делать если...", callback_data="what_if_menu")],
        [InlineKeyboardButton(text="🎁 Пригласить друга (+7 дней)", callback_data="referral")],
        [InlineKeyboardButton(text="📢 Канал", url=ch),
         InlineKeyboardButton(text="👥 Чат", url=gr)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_btn(cb="start"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data=cb)]])

def doc_type_kb(prefix="add_doc_type_"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪪 Патент на работу", callback_data=f"{prefix}patent")],
        [InlineKeyboardButton(text="📘 РВП", callback_data=f"{prefix}rvp")],
        [InlineKeyboardButton(text="📗 РВПО", callback_data=f"{prefix}rvpo")],
        [InlineKeyboardButton(text="🏠 ВНЖ", callback_data=f"{prefix}vnj")],
        [InlineKeyboardButton(text="📄 Миграционная карта", callback_data=f"{prefix}mk")],
        [InlineKeyboardButton(text="📋 Другой документ", callback_data=f"{prefix}other")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
    ])

# ============================================================
# СТАРТ
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    # Определяем источник (реферал или прямой)
    args = message.text.split()
    source = "direct"
    if len(args) > 1:
        if args[1].startswith("ref_"):
            source = f"referral_{args[1].replace('ref_', '')}"
        else:
            source = f"deeplink_{args[1][:20]}"

    await db_register_user(message.from_user, source=source)
    await track_action(message.from_user.id, "start", source)
    uid = message.from_user.id

    # Рефеальная логика
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1].replace("ref_", ""))
            if ref_id != uid:
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute("SELECT id FROM referrals WHERE referred_id=?", (uid,)) as cur:
                        if not await cur.fetchone():
                            now_s = datetime.now().strftime("%d.%m.%Y %H:%M")
                            await db.execute(
                                "INSERT INTO referrals (referrer_id, referred_id, bonus_days, created_at) VALUES (?,?,?,?)",
                                (ref_id, uid, 7, now_s)
                            )
                            for bid in [ref_id, uid]:
                                async with db.execute("SELECT sub_until FROM users WHERE user_id=?", (bid,)) as c2:
                                    row = await c2.fetchone()
                                base = datetime.now()
                                if row and row[0]:
                                    try:
                                        b = datetime.strptime(row[0], "%d.%m.%Y")
                                        if b > base:
                                            base = b
                                    except Exception as e:
                                        log.warning(f"Referral date parse error: {e}")
                                new_exp = (base + timedelta(days=7)).strftime("%d.%m.%Y")
                                await db.execute("UPDATE users SET sub_until=? WHERE user_id=?", (new_exp, bid))
                            await db.commit()
                            try:
                                await bot.send_message(
                                    ref_id,
                                    "🎉 <b>+7 дней подписки!</b>\n\nПо вашей ссылке зарегистрировался новый пользователь!",
                                    parse_mode="HTML"
                                )
                            except Exception as e:
                                log.warning(f"Referral notify error for {ref_id}: {e}")
                            await message.answer("🎁 <b>Вы получили +7 дней подписки!</b>\nВас пригласил друг 🎉", parse_mode="HTML")
        except Exception as e:
            log.warning(f"Referral processing error: {e}")

    user = await db_get_user(uid)
    user_type = user.get("user_type", "personal") if user else "personal"

    await message.answer(
        "👋 <b>МигрантПро</b> — ваш помощник по документам в России.\n\n"
        "Всё необходимое доступно прямо здесь 👇\n\n"
        "🤖 AI-юрист отвечает на вопросы по миграции\n"
        "📄 Документы, сроки, напоминания\n"
        "🏢 B2B панель для работодателей\n"
        "🔍 Проверки МВД, реестр, запрет въезда",
        parse_mode="HTML",
        reply_markup=main_menu(user_type)
    )

@dp.callback_query(F.data == "start")
async def cb_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user = await db_get_user(callback.from_user.id)
    user_type = user.get("user_type", "personal") if user else "personal"
    try:
        await callback.message.edit_text(
            "👋 Главное меню. Выберите нужное 👇",
            reply_markup=main_menu(user_type)
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"cb_start edit error (OK if query too old): {e}")

# ============================================================
# HEALTH SCORE — МИГРАЦИОННЫЙ СТАТУС
# ============================================================
@dp.callback_query(F.data == "health_score")
async def show_health_score(callback: types.CallbackQuery):
    uid = callback.from_user.id
    try:
        await callback.answer("Считаю ваш статус...")
    except Exception as e:
        log.debug(f"callback.answer error: {e}")

    hs = await calculate_health_score(uid)

    score_bar = "█" * (hs["score"] // 10) + "░" * (10 - hs["score"] // 10)
    issues_text = "\n".join(f"• {i}" for i in hs["issues"][:5])
    tips_text = "\n".join(f"✅ {t}" for t in hs["tips"][:3])

    text = (
        f"📊 <b>Ваш миграционный статус</b>\n\n"
        f"{hs['color']} <b>{hs['label']}</b>\n"
        f"Рейтинг: {hs['score']}/100\n"
        f"[{score_bar}]\n\n"
        f"<b>Ситуация:</b>\n{issues_text}\n\n"
    )
    if hs["tips"] and hs["tips"][0] != "Все документы в норме":
        text += f"<b>Что делать:</b>\n{tips_text}\n\n"

    text += f"⚡ <b>Ближайший шаг:</b>\n{hs['next_action']}"

    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗺️ Мой план действий", callback_data="roadmap")],
                [InlineKeyboardButton(text="➕ Добавить документ", callback_data="add_doc")],
                [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
    except Exception as e:
        log.error(f"health_score edit error: {e}")

# ============================================================
# AI ROADMAP
# ============================================================
@dp.callback_query(F.data == "roadmap")
async def show_roadmap(callback: types.CallbackQuery):
    uid = callback.from_user.id
    try:
        await callback.answer("Строю ваш план...")
    except Exception as e:
        log.debug(f"callback.answer error: {e}")

    roadmap_text = await build_ai_roadmap(uid)

    try:
        await callback.message.edit_text(
            f"🗺️ <b>Ваш персональный план</b>\n\n{roadmap_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Мой статус", callback_data="health_score")],
                [InlineKeyboardButton(text="➕ Добавить документ", callback_data="add_doc")],
                [InlineKeyboardButton(text="📬 Уведомить МВД", callback_data="notify_employment")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
    except Exception as e:
        log.error(f"roadmap edit error: {e}")

# ============================================================
# ЧТО ДЕЛАТЬ ЕСЛИ...
# ============================================================
@dp.callback_query(F.data == "what_if")
async def what_if_menu(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "❓ <b>Что делать если...</b>\n\nВыберите вашу ситуацию:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🪪 Потерял патент", callback_data="wif_patent_lost")],
                [InlineKeyboardButton(text="🔴 Просрочен патент", callback_data="wif_expired")],
                [InlineKeyboardButton(text="📍 Просрочена регистрация", callback_data="wif_reg_expired")],
                [InlineKeyboardButton(text="🚫 Нашёл запрет на въезд", callback_data="wif_ban")],
                [InlineKeyboardButton(text="💼 Работодатель не платит", callback_data="wif_employer")],
                [InlineKeyboardButton(text="🔄 Сменил работодателя", callback_data="wif_changed_employer")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.error(f"what_if_menu error: {e}")

@dp.callback_query(F.data.startswith("wif_"))
async def what_if_scenario(callback: types.CallbackQuery):
    scenario = WHAT_IF_SCENARIOS.get(callback.data)
    if not scenario:
        try:
            await callback.answer("Сценарий не найден")
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
        return

    try:
        await callback.message.edit_text(
            scenario["text"],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                [InlineKeyboardButton(text="❓ Задать вопрос AI-юристу", callback_data="question")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="what_if")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.error(f"what_if_scenario error: {e}")

# ============================================================
# ЛИЧНЫЙ КАБИНЕТ
# ============================================================
@dp.callback_query(F.data == "cabinet")
async def cabinet(callback: types.CallbackQuery):
    uid = callback.from_user.id
    user = await db_get_user(uid)
    docs = await db_get_docs(uid)
    sub = user.get("sub_until", "не активна") if user else "не активна"
    user_type = user.get("user_type", "personal") if user else "personal"

    expiring = []
    for d in docs:
        try:
            exp = datetime.strptime(d["expiry_date"], "%d.%m.%Y")
            dl = (exp - datetime.now()).days
            if dl <= 30:
                dt = DOC_TYPES.get(d["doc_type"], DOC_TYPES["patent"])
                expiring.append(f"⚠️ {dt['emoji']} {d.get('series', '')} {d['number']} — {dl} дней!")
        except Exception as e:
            log.debug(f"Cabinet expiring parse error: {e}")

    warn = "\n" + "\n".join(expiring) if expiring else ""
    type_label = "🏢 Работодатель" if user_type == "b2b" else "👤 Физическое лицо"

    kb_rows = [
        [InlineKeyboardButton(text="📊 Мой миграционный статус", callback_data="health_score")],
        [InlineKeyboardButton(text="🗺️ Мой план действий", callback_data="roadmap")],
        [InlineKeyboardButton(text="📄 Мои документы", callback_data="my_docs")],
        [InlineKeyboardButton(text="➕ Добавить документ", callback_data="add_doc")],
        [InlineKeyboardButton(text="🧾 Мои чеки", callback_data="my_receipts")],
    ]
    if user_type == "b2b":
        kb_rows.append([InlineKeyboardButton(text="🏢 Панель работодателя", callback_data="b2b_panel")])
    else:
        kb_rows.append([InlineKeyboardButton(text="🏢 Стать работодателем", callback_data="become_b2b")])
    kb_rows.append([InlineKeyboardButton(text="⭐ Подписка", callback_data="subscription")])
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")])

    try:
        await callback.message.edit_text(
            f"👤 <b>Личный кабинет</b>\n\n"
            f"Имя: {callback.from_user.full_name}\n"
            f"Тип: {type_label}\n"
            f"Документов: {len(docs)}\n"
            f"Подписка до: {sub}"
            f"{warn}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"cabinet edit error: {e}")

# ============================================================
# МОИ ДОКУМЕНТЫ
# ============================================================
@dp.callback_query(F.data == "my_docs")
async def my_docs(callback: types.CallbackQuery):
    docs = await db_get_docs(callback.from_user.id)
    if not docs:
        try:
            await callback.message.edit_text(
                "📄 У вас нет сохранённых документов.\nДобавьте патент, РВП или другой документ для отслеживания сроков.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить документ", callback_data="add_doc")],
                    [InlineKeyboardButton(text="◀️ Кабинет", callback_data="cabinet")],
                ])
            )
            await callback.answer()
        except Exception as e:
            log.debug(f"my_docs edit error: {e}")
        return

    text = "📄 <b>Ваши документы:</b>\n\n"
    for d in docs:
        dt = DOC_TYPES.get(d["doc_type"], DOC_TYPES["patent"])
        try:
            exp = datetime.strptime(d["expiry_date"], "%d.%m.%Y")
            dl = (exp - datetime.now()).days
            icon = "🟢" if dl > 30 else ("🟠" if dl > 14 else "🔴")
            status = f"{icon} {dl} дней" if dl >= 0 else f"🔴 просрочен на {abs(dl)} дней"
        except Exception as e:
            log.debug(f"my_docs date parse error: {e}")
            status = "⚪ неизвестно"
        text += (
            f"{dt['emoji']} <b>{dt['name']}</b>\n"
            f"   Серия/№: {d.get('series', '')} {d['number']}\n"
            f"   Выдан: {d['issue_date']} | До: {d['expiry_date']}\n"
            f"   Регион: {d.get('region', '—')}\n"
            f"   Статус: {status}\n\n"
        )
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить документ", callback_data="add_doc")],
                [InlineKeyboardButton(text="◀️ Кабинет", callback_data="cabinet")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"my_docs list edit error: {e}")

@dp.callback_query(F.data == "my_receipts")
async def my_receipts(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "🧾 <b>Чеки об оплате</b>\n\n"
            "Отправьте фото или скриншот чека — бот сохранит его в архив.\n\n"
            "Архив чеков поможет при проверках!",
            parse_mode="HTML",
            reply_markup=back_btn("cabinet")
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"my_receipts error: {e}")

# ============================================================
# ДОБАВИТЬ ДОКУМЕНТ
# ============================================================
@dp.callback_query(F.data == "add_doc")
async def add_doc_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddDocState.doc_type)
    try:
        await callback.message.edit_text(
            "➕ <b>Добавление документа</b>\n\nВыберите тип документа:",
            parse_mode="HTML",
            reply_markup=doc_type_kb()
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"add_doc_start error: {e}")

@dp.callback_query(F.data.startswith("add_doc_type_"))
async def add_doc_type(callback: types.CallbackQuery, state: FSMContext):
    doc_type = callback.data.replace("add_doc_type_", "")
    await state.update_data(doc_type=doc_type)
    dt = DOC_TYPES.get(doc_type, DOC_TYPES["patent"])
    await state.set_state(AddDocState.series)
    try:
        await callback.message.edit_text(
            f"{dt['emoji']} <b>{dt['name']}</b>\n\n"
            f"Шаг 1/6 — Серия документа\n(если нет серии — напишите прочерк «-»)",
            parse_mode="HTML",
            reply_markup=back_btn()
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"add_doc_type error: {e}")

@dp.message(AddDocState.series)
async def add_doc_series(message: types.Message, state: FSMContext):
    await state.update_data(series=message.text.strip())
    await state.set_state(AddDocState.number)
    await message.answer("Шаг 2/6 — Номер документа:")

@dp.message(AddDocState.number)
async def add_doc_number(message: types.Message, state: FSMContext):
    await state.update_data(number=message.text.strip())
    data = await state.get_data()
    if data.get("doc_type") == "patent":
        await state.set_state(AddDocState.blank_series)
        await message.answer("Шаг 3/6 — Серия бланка (оборотная сторона, если нет — «-»):")
    else:
        await state.update_data(blank_series="-", blank_number="-")
        await state.set_state(AddDocState.issue_date)
        await message.answer("Шаг 3/6 — Дата выдачи (ДД.ММ.ГГГГ):")

@dp.message(AddDocState.blank_series)
async def add_doc_blank_series(message: types.Message, state: FSMContext):
    await state.update_data(blank_series=message.text.strip())
    await state.set_state(AddDocState.blank_number)
    await message.answer("Шаг 4/6 — Номер бланка (оборотная сторона):")

@dp.message(AddDocState.blank_number)
async def add_doc_blank_number(message: types.Message, state: FSMContext):
    await state.update_data(blank_number=message.text.strip())
    await state.set_state(AddDocState.issue_date)
    await message.answer("Шаг 5/6 — Дата выдачи (ДД.ММ.ГГГГ):")

@dp.message(AddDocState.issue_date)
async def add_doc_issue(message: types.Message, state: FSMContext):
    try:
        datetime.strptime(message.text.strip(), "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите ДД.ММ.ГГГГ (например: 15.03.2025):")
        return
    await state.update_data(issue_date=message.text.strip())
    await state.set_state(AddDocState.expiry_date)
    await message.answer("Шаг 6/6 — Дата окончания (ДД.ММ.ГГГГ):")

@dp.message(AddDocState.expiry_date)
async def add_doc_expiry(message: types.Message, state: FSMContext):
    try:
        datetime.strptime(message.text.strip(), "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите ДД.ММ.ГГГГ:")
        return
    await state.update_data(expiry_date=message.text.strip())
    await state.set_state(AddDocState.region)
    await message.answer("Регион действия (например: Москва, или «-» если не нужно):")

@dp.message(AddDocState.region)
async def add_doc_region(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id
    dt = DOC_TYPES.get(data["doc_type"], DOC_TYPES["patent"])
    exp = datetime.strptime(data["expiry_date"], "%d.%m.%Y")
    dl = (exp - datetime.now()).days

    await db_add_doc(
        uid, data["doc_type"], data.get("series", "-"), data["number"],
        data.get("blank_series", "-"), data.get("blank_number", "-"),
        data["issue_date"], data["expiry_date"], message.text.strip()
    )

    await message.answer(
        f"✅ <b>{dt['name']} добавлен!</b>\n\n"
        f"Серия/№: {data.get('series', '')} {data['number']}\n"
        f"Выдан: {data['issue_date']}\n"
        f"Истекает: {data['expiry_date']} (через {dl} дней)\n\n"
        f"🔔 Напоминания настроены!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Посмотреть статус", callback_data="health_score")],
            [InlineKeyboardButton(text="🗺️ Мой план действий", callback_data="roadmap")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
        ])
    )

    # ФИКС: Уведомление админа с try/except
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📄 <b>Новый документ добавлен</b>\n"
            f"👤 @{message.from_user.username or '—'} ({uid})\n"
            f"Тип: {dt['name']}\n"
            f"№: {data.get('series', '')} {data['number']}\n"
            f"До: {data['expiry_date']}",
            parse_mode="HTML"
        )
    except Exception as e:
        log.warning(f"Admin notification error: {e}")

# ============================================================
# ПРОВЕРКИ МВД
# ============================================================
@dp.callback_query(F.data == "checks_menu")
async def checks_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪪 Проверить патент / разрешение", callback_data="check_patent_info")],
        [InlineKeyboardButton(text="🛡️ Реестр контролируемых лиц", callback_data="check_registry_info")],
        [InlineKeyboardButton(text="🚫 Запрет на въезд в Россию", callback_data="check_ban_info")],
        [InlineKeyboardButton(text="📋 Готовность патента в ММЦ", callback_data="check_ready_info")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
    ])
    try:
        await callback.message.edit_text(
            "🔍 <b>Проверки в государственных базах</b>\n\n"
            "Все проверки осуществляются на официальных сайтах МВД РФ.\n"
            "Выберите нужную проверку:",
            parse_mode="HTML",
            reply_markup=kb
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"checks_menu error: {e}")

@dp.callback_query(F.data == "check_patent_info")
async def check_patent_info(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "🪪 <b>Проверка патента / разрешения на работу</b>\n\n"
            "📌 Проверка осуществляется на официальном сервисе ГУВМ МВД РФ.\n\n"
            "Для проверки вам понадобятся:\n"
            "• Серия и номер патента (лицевая сторона)\n"
            "• Серия и номер бланка (оборотная сторона)\n"
            "• Номер паспорта\n"
            "• Гражданство\n\n"
            "⚠️ МВД требует ввод капчи — проверка выполняется вручную на сайте МВД.\n\n"
            "Нажмите кнопку ниже чтобы перейти на сайт МВД:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Открыть проверку на МВД.РФ",
                                      url="https://xn--c1abzb4b.xn--b1aew.xn--p1ai/tm")],
                [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="checks_menu")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"check_patent_info error: {e}")

@dp.callback_query(F.data == "check_registry_info")
async def check_registry_info(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "🛡️ <b>Реестр контролируемых лиц МВД РФ</b>\n\n"
            "Проверьте — есть ли вы в реестре МВД.\n\n"
            "Для проверки нужны:\n"
            "• Фамилия, Имя, Отчество\n"
            "• Дата рождения\n"
            "• Серия и номер паспорта\n"
            "• Дата выдачи паспорта\n"
            "• Гражданство\n\n"
            "Нажмите кнопку — откроется официальная страница МВД:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Открыть реестр на МВД.РФ",
                                      url="https://xn--b1aew.xn--p1ai/rkl")],
                [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="checks_menu")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"check_registry_info error: {e}")

@dp.callback_query(F.data == "check_ban_info")
async def check_ban_info(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "🚫 <b>Запрет на въезд в Россию</b>\n\n"
            "Проверьте — есть ли у вас запрет на въезд.\n\n"
            "Для проверки нужны точные данные как в паспорте:\n"
            "• Фамилия, Имя (как в паспорте)\n"
            "• Дата рождения\n"
            "• Серия и номер паспорта\n"
            "• Дата выдачи и срок действия\n"
            "• Гражданство\n\n"
            "⚠️ Ошибка в данных = неверный результат!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Проверить на МВД.РФ",
                                      url="https://xn--c1abzb4b.xn--b1aew.xn--p1ai/services/1697527")],
                [InlineKeyboardButton(text="⚡ Нашёл запрет — нужна помощь!", callback_data="wif_ban")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="checks_menu")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"check_ban_info error: {e}")

@dp.callback_query(F.data == "check_ready_info")
async def check_ready_info(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "📋 <b>Готовность патента в ММЦ Сахарово</b>\n\n"
            "Для проверки готовности документов:\n\n"
            "📞 Горячая линия ММЦ:\n"
            "+7 (495) 203-88-20\n\n"
            "🌐 Официальный сайт:\nmc.mos.ru\n\n"
            "⏰ Режим работы: ежедневно 8:00–20:00\n"
            "📍 Адрес: Варшавское шоссе, 64-й км, д.1",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Открыть сайт ММЦ", url="https://mc.mos.ru")],
                [InlineKeyboardButton(text="📞 Позвонить в ММЦ", url="tel:+74952038820")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="checks_menu")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"check_ready_info error: {e}")

# ============================================================
# ГОСУСЛУГИ МЕНЮ
# ============================================================
@dp.callback_query(F.data == "gosuslugi_menu")
async def gosuslugi_menu(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "🏛️ <b>Госуслуги и документы</b>\n\nВыберите нужную услугу:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📬 Уведомить МВД о трудоустройстве",
                                      callback_data="notify_employment")],
                [InlineKeyboardButton(text="🪪 Получить СНИЛС", callback_data="kb_snils")],
                [InlineKeyboardButton(text="🔢 Получить ИНН", callback_data="kb_inn")],
                [InlineKeyboardButton(text="📝 Запись в ММЦ Сахарово",
                                      url="https://mmc.mos.ru/client-office")],
                [InlineKeyboardButton(text="📚 Экзамен по русскому языку",
                                      url="https://mc.mos.ru/ru/services/testing/")],
                [InlineKeyboardButton(text="🏥 Медицинское освидетельствование",
                                      url="https://mc.mos.ru/ru/services/medical/")],
                [InlineKeyboardButton(text="🔢 Узнать ИНН на ФНС",
                                      url="https://service.nalog.ru/inn.do")],
                [InlineKeyboardButton(text="⚖️ Проверить долги ФССП",
                                      url="https://fssprus.ru/iss/ip")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"gosuslugi_menu error: {e}")

@dp.callback_query(F.data == "notify_employment")
async def notify_employment(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "📬 <b>Уведомление о трудоустройстве через Госуслуги</b>\n\n"
            "Работодатель обязан уведомить МВД в течение <b>3 рабочих дней</b> "
            "после заключения трудового договора с иностранным гражданином.\n\n"
            "<b>Как подать уведомление:</b>\n"
            "1️⃣ Зайдите на Госуслуги по кнопке ниже\n"
            "2️⃣ Войдите в аккаунт\n"
            "3️⃣ Заполните форму уведомления\n"
            "4️⃣ Подпишите и отправьте\n\n"
            "<b>Штраф за несвоевременное уведомление:</b>\n"
            "• ФЛ: 2 000 — 5 000 руб.\n"
            "• ЮЛ: 400 000 — 1 000 000 руб.!\n\n"
            "⚠️ Не затягивайте — штрафы огромные!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏛️ Подать уведомление на Госуслугах",
                                      url="https://www.gosuslugi.ru/329523")],
                [InlineKeyboardButton(text="📋 Нужна помощь юриста", callback_data="consult")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="gosuslugi_menu")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"notify_employment error: {e}")

@dp.callback_query(F.data == "consult_urgent")
async def consult_urgent(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ConsultState.name)
    await state.update_data(urgency="🔴 СРОЧНО — запрет на въезд")
    try:
        await callback.message.edit_text(
            "⚠️ <b>СРОЧНАЯ консультация по запрету въезда</b>\n\n"
            "Запрет на въезд — серьёзная ситуация. "
            "Юрист свяжется с вами в течение 1 часа.\n\n"
            "Шаг 1/3 — Как вас зовут?",
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"consult_urgent error: {e}")

# ============================================================
# ВОПРОС AI — ФИКС: AsyncAnthropic + правильная модель + retries
# ============================================================
@dp.callback_query(F.data == "question")
async def ask_question(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(QuestionState.waiting)
    try:
        await callback.message.edit_text(
            "❓ <b>AI-юрист онлайн</b>\n\n"
            "Напишите ваш вопрос по миграции.\n"
            "Отвечу на русском, узбекском, таджикском, казахском и других языках СНГ.\n\n"
            "✍️ Пишите вопрос:",
            parse_mode="HTML",
            reply_markup=back_btn()
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"ask_question error: {e}")

@dp.message(QuestionState.waiting)
async def process_question(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    lang = detect_language(message.text)

    # ── ПРОВЕРКА ЛИМИТА FREE ────────────────────────────────────────────────
    limit_check = await check_and_increment_ai_limit(uid)

    if not limit_check["allowed"]:
        log.info(f"AI limit hit for user {uid} (free, {limit_check['used']}/{limit_check['limit']})")
        await message.answer(
            "🔒 <b>Лимит бесплатных вопросов исчерпан</b>\n\n"
            f"Вы использовали все <b>{FREE_AI_LIMIT} бесплатных вопроса</b> сегодня.\n\n"
            "Чтобы получить неограниченный доступ к AI-юристу:\n\n"
            "⭐ <b>PRO подписка</b> — от 100 Stars/мес\n"
            "✅ Безлимитные вопросы\n"
            "✅ История диалогов\n"
            "✅ Анализ рисков\n"
            "✅ Напоминания о сроках 24/7\n"
            "✅ Фото-анализ документов\n\n"
            "🕐 Или подождите до завтра — лимит сбросится автоматически.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Оформить PRO подписку", callback_data="subscription")],
                [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        return

    # Показываем счётчик FREE пользователям
    if not limit_check["is_pro"]:
        remaining = limit_check["limit"] - limit_check["used"]
        try:
            await message.answer(
                f"💬 <i>Бесплатных вопросов осталось сегодня: {remaining}</i>",
                parse_mode="HTML"
            )
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, uid))
            await db.commit()
    except Exception as e:
        log.warning(f"Language update error: {e}")

    thinking = await message.answer(THINKING_MSGS.get(lang, THINKING_MSGS["ru"]))

    answer = None
    # ФИКС: Async вызов + правильная модель + retries
    for attempt in range(3):
        try:
            response = await asyncio.wait_for(
                claude.messages.create(
                    model="claude-3-5-sonnet-latest",   # ФИКС: модель которая существует
                    max_tokens=1200,
                    messages=[{"role": "user", "content": (
                        f"{LANG_PROMPTS.get(lang, LANG_PROMPTS['ru'])}\n"
                        "Дай подробный структурированный ответ. Укажи сроки и суммы если они есть. "
                        "В конце предложи записаться на консультацию.\n\n"
                        f"Вопрос: {message.text}"
                    )}]
                ),
                timeout=30.0
            )
            answer = response.content[0].text
            break
        except asyncio.TimeoutError:
            log.warning(f"Claude timeout on attempt {attempt + 1}")
            if attempt < 2:
                await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Claude API error on attempt {attempt + 1}: {e}")
            if attempt < 2:
                await asyncio.sleep(2)

    try:
        await thinking.delete()
    except Exception as e:
        log.debug(f"Thinking message delete error: {e}")

    if answer:
        # Убираем Markdown из ответа если есть — используем HTML
        try:
            await message.answer(
                f"💬 {answer}",
                parse_mode=None,  # Без parse_mode — безопаснее для AI-текста
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                    [InlineKeyboardButton(text="❓ Ещё вопрос", callback_data="question")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
                ])
            )
        except Exception as e:
            log.error(f"Answer send error: {e}")
            await message.answer(answer[:4000])

        # Уведомление админа
        try:
            await bot.send_message(
                ADMIN_ID,
                f"❓ <b>Вопрос [{lang.upper()}]</b>\n"
                f"👤 @{message.from_user.username or '—'} ({uid})\n\n"
                f"{message.text[:500]}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✍️ Написать клиенту", url=f"tg://user?id={uid}")
                ]])
            )
        except Exception as e:
            log.warning(f"Admin question notify error: {e}")
    else:
        # ФИКС: Fallback на все языки
        fallback = FALLBACK_MSGS.get(lang, FALLBACK_MSGS["ru"])
        try:
            await message.answer(
                fallback,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
                ])
            )
        except Exception as e:
            log.error(f"Fallback send error: {e}")

# ============================================================
# КОНСУЛЬТАЦИЯ
# ============================================================
@dp.callback_query(F.data == "consult")
async def consult_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ConsultState.name)
    try:
        await callback.message.edit_text(
            "📋 <b>Запись на консультацию</b>\n\nШаг 1/5 — Как вас зовут?",
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"consult_start error: {e}")

@dp.message(ConsultState.name)
async def consult_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(ConsultState.phone)
    await message.answer("📞 Шаг 2/5 — Ваш номер телефона:")

@dp.message(ConsultState.phone)
async def consult_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await state.set_state(ConsultState.city)
    await message.answer("🏙️ Шаг 3/5 — Ваш город:")

@dp.message(ConsultState.city)
async def consult_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text)
    await state.set_state(ConsultState.topic)
    await message.answer("📂 Шаг 4/5 — По какому вопросу?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪪 Патент", callback_data="ct_patent")],
        [InlineKeyboardButton(text="📘 РВП/РВПО", callback_data="ct_rvp")],
        [InlineKeyboardButton(text="🏠 ВНЖ", callback_data="ct_vnj")],
        [InlineKeyboardButton(text="🇷🇺 Гражданство", callback_data="ct_citizen")],
        [InlineKeyboardButton(text="🚫 Депортация/запрет", callback_data="ct_deport")],
        [InlineKeyboardButton(text="🏢 Вопрос работодателя", callback_data="ct_b2b")],
        [InlineKeyboardButton(text="📂 Другое", callback_data="ct_other")],
    ]))

@dp.callback_query(F.data.startswith("ct_"))
async def consult_topic(callback: types.CallbackQuery, state: FSMContext):
    topics = {"ct_patent": "Патент", "ct_rvp": "РВП/РВПО", "ct_vnj": "ВНЖ",
              "ct_citizen": "Гражданство", "ct_deport": "Депортация/запрет",
              "ct_b2b": "Вопрос работодателя", "ct_other": "Другое"}
    await state.update_data(topic=topics.get(callback.data, "Другое"))
    await state.set_state(ConsultState.urgency)
    try:
        await callback.message.edit_text("⚡ Шаг 5/5 — Срочность?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 СРОЧНО (сегодня)", callback_data="cu_urgent")],
            [InlineKeyboardButton(text="🟡 В течение недели", callback_data="cu_week")],
            [InlineKeyboardButton(text="🟢 Не срочно", callback_data="cu_normal")],
        ]))
        await callback.answer()
    except Exception as e:
        log.debug(f"consult_topic error: {e}")

@dp.callback_query(F.data.startswith("cu_"))
async def consult_urgency(callback: types.CallbackQuery, state: FSMContext):
    urg = {"cu_urgent": "🔴 СРОЧНО", "cu_week": "🟡 В течение недели", "cu_normal": "🟢 Не срочно"}
    data = await state.get_data()
    await state.clear()
    uid = callback.from_user.id
    urgency = data.get("urgency") or urg.get(callback.data, "Не срочно")

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO consultations (user_id,name,phone,city,topic,urgency,created_at) VALUES (?,?,?,?,?,?,?)",
                (uid, data.get("name"), data.get("phone"), data.get("city"),
                 data.get("topic"), urgency, datetime.now().strftime("%d.%m.%Y %H:%M"))
            )
            await db.commit()
    except Exception as e:
        log.error(f"Consultation save error: {e}")

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
            f"👤 {data.get('name')} | 📞 {data.get('phone')}\n"
            f"🏙️ {data.get('city')} | 📂 {data.get('topic')}\n"
            f"⚡ {urgency}\n"
            f"🆔 @{callback.from_user.username or '—'} ({uid})\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✍️ Написать клиенту", url=f"tg://user?id={uid}")
            ]])
        )
    except Exception as e:
        log.error(f"Admin consult notify error: {e}")

    try:
        await callback.message.edit_text(
            f"✅ <b>Заявка принята, {data.get('name')}!</b>\n\n"
            f"Юрист свяжется в ближайшее время.",
            parse_mode="HTML", reply_markup=main_menu()
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"consult_urgency final edit error: {e}")

# ============================================================
# КАЛЬКУЛЯТОР СРОКОВ
# ============================================================
@dp.callback_query(F.data == "calculator")
async def calculator_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CalcState.doc_type)
    try:
        await callback.message.edit_text(
            "⏱️ <b>Калькулятор сроков документов</b>\n\nВыберите тип:",
            parse_mode="HTML",
            reply_markup=doc_type_kb("calc_type_")
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"calculator_start error: {e}")

@dp.callback_query(F.data.startswith("calc_type_"))
async def calc_type(callback: types.CallbackQuery, state: FSMContext):
    doc_type = callback.data.replace("calc_type_", "")
    await state.update_data(doc_type=doc_type)
    await state.set_state(CalcState.issue_date)
    dt = DOC_TYPES.get(doc_type, DOC_TYPES["patent"])
    try:
        await callback.message.edit_text(
            f"📅 <b>{dt['name']}</b>\n\nВведите дату выдачи (ДД.ММ.ГГГГ):",
            parse_mode="HTML", reply_markup=back_btn()
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"calc_type error: {e}")

@dp.message(CalcState.issue_date)
async def calc_result(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    dt = DOC_TYPES.get(data.get("doc_type", "patent"), DOC_TYPES["patent"])
    try:
        issue = datetime.strptime(message.text.strip(), "%d.%m.%Y")
        expiry = issue + timedelta(days=dt["days"])
        dl = (expiry - datetime.now()).days
        if dl < 0:
            icon, status = "🔴", f"ПРОСРОЧЕН на {abs(dl)} дней!"
        elif dl <= dt["warn_days"] // 2:
            icon, status = "🔴", f"КРИТИЧНО! Осталось {dl} дней"
        elif dl <= dt["warn_days"]:
            icon, status = "🟠", f"Осталось {dl} дней — пора продлевать"
        else:
            icon, status = "🟢", f"Действует ещё {dl} дней"

        await message.answer(
            f"📅 <b>Результат</b>\n\n"
            f"{dt['emoji']} {dt['name']}\n"
            f"Выдан: {issue.strftime('%d.%m.%Y')}\n"
            f"Истекает: <b>{expiry.strftime('%d.%m.%Y')}</b>\n\n"
            f"{icon} <b>{status}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                [InlineKeyboardButton(text="➕ Добавить в отслеживание", callback_data="add_doc")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
    except ValueError:
        await message.answer("❌ Неверный формат. Введите ДД.ММ.ГГГГ:")
        await state.set_state(CalcState.issue_date)

# ============================================================
# БАЗА ЗНАНИЙ
# ============================================================
KB = {
    "kb_patent": "🪪 <b>ПАТЕНТ НА РАБОТУ</b>\n\n<b>Кому нужен:</b> гражданам безвизовых стран (Узбекистан, Таджикистан, Молдова, Азербайджан и др.)\n<b>Срок:</b> 1–12 мес\n<b>Стоимость:</b> ~7 000–10 000 руб./мес\n\n1️⃣ Въезд с отметкой «работа» в миграционной карте\n2️⃣ В течение 30 дней — документы в МФЦ\n3️⃣ Экзамен по русскому языку\n4️⃣ Медкомиссия\n5️⃣ Оплата НДФЛ авансом\n\n❗ Работать только в регионе выдачи!",
    "kb_rvp": "📘 <b>РВП</b>\n\n<b>Срок:</b> 3 года → ВНЖ\n\n<b>Без квоты:</b>\n✅ Супруги граждан РФ\n✅ Казахстан, Беларусь, Кыргызстан\n✅ Программа переселения\n\n<b>Госпошлина:</b> 1 600 руб.",
    "kb_rvpo": "📗 <b>РВПО (для участников госпрограммы)</b>\n\nВыдаётся участникам Государственной программы переселения соотечественников.\n\n✅ Без квоты\n✅ Упрощённый порядок\n✅ Быстрый путь к гражданству\n\nПодать заявку на участие в программе — через консульство РФ или МФЦ.",
    "kb_vnj": "🏠 <b>ВНЖ</b>\n\n<b>Срок:</b> 5 лет (продлевается)\n\n✅ Работать в любом регионе\n✅ Бесплатная медпомощь\n✅ Свободный въезд/выезд\n\n<b>Госпошлина:</b> 5 000 руб.",
    "kb_citizenship": "🇷🇺 <b>ГРАЖДАНСТВО</b>\n\nОбщий путь: РВП → ВНЖ (5 лет) → Гражданство\n\n<b>Упрощённо:</b>\n✅ Брак с гражданином РФ (3+ лет)\n✅ Программа переселения\n✅ Беларусь, Казахстан, Молдова, Украина\n\n<b>Госпошлина:</b> 3 500 руб.",
    "kb_deport": "🚫 <b>ДЕПОРТАЦИЯ</b>\n\n<b>Обжаловать:</b>\n1️⃣ Жалоба в суд — 10 дней\n2️⃣ Прокуратура\n3️⃣ Временное убежище\n\n⚡ Срочно нужен юрист!",
    "kb_snils": "🪪 <b>СНИЛС</b>\n\n<b>Через работодателя:</b>\n1️⃣ Паспорт + миграционные документы\n2️⃣ Работодатель подаёт в СФР\n3️⃣ Готово через 5 дней\n\n<b>Через МФЦ:</b>\nЗаявление АДВ-1 + паспорт + РВП/ВНЖ/патент\n\n<b>Стоимость:</b> Бесплатно",
    "kb_inn": "🔢 <b>ИНН</b>\n\n<b>Онлайн:</b> nalog.ru → «Узнать ИНН»\n\n<b>Документы:</b>\n• Паспорт + нотариальный перевод\n• Миграционная карта\n• Уведомление о постановке на учёт\n\n<b>Стоимость:</b> Бесплатно",
}

@dp.callback_query(F.data == "knowledge")
async def knowledge_base(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "📚 <b>База знаний</b>\n\nВыберите тему:", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🪪 Патент на работу", callback_data="kb_patent")],
                [InlineKeyboardButton(text="📘 РВП", callback_data="kb_rvp")],
                [InlineKeyboardButton(text="📗 РВПО", callback_data="kb_rvpo")],
                [InlineKeyboardButton(text="🏠 ВНЖ", callback_data="kb_vnj")],
                [InlineKeyboardButton(text="🇷🇺 Гражданство", callback_data="kb_citizenship")],
                [InlineKeyboardButton(text="🚫 Депортация/запрет", callback_data="kb_deport")],
                [InlineKeyboardButton(text="🪪 СНИЛС", callback_data="kb_snils")],
                [InlineKeyboardButton(text="🔢 ИНН", callback_data="kb_inn")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"knowledge_base error: {e}")

@dp.callback_query(F.data.startswith("kb_"))
async def knowledge_item(callback: types.CallbackQuery):
    text = KB.get(callback.data, "Информация не найдена")
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                [InlineKeyboardButton(text="◀️ База знаний", callback_data="knowledge")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"knowledge_item error: {e}")

# ============================================================
# ПОДПИСКА
# ============================================================
@dp.callback_query(F.data == "subscription")
async def subscription_menu(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "⭐ <b>Подписка МигрантПро</b>\n\n"
            "Оплата через Telegram Stars — работает из любой страны!\n\n"
            "✅ Ежедневная проверка документов\n"
            "✅ Реестр контролируемых лиц\n"
            "✅ Умные напоминания о сроках\n"
            "✅ Хранение документов\n\n"
            "Выберите тариф:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ 100 Stars — 1 месяц", callback_data="buy_1m")],
                [InlineKeyboardButton(text="⭐ 190 Stars — 2 месяца", callback_data="buy_2m")],
                [InlineKeyboardButton(text="⭐ 270 Stars — 3 месяца (-16%)", callback_data="buy_3m")],
                [InlineKeyboardButton(text="⭐ 490 Stars — 6 месяцев (-22%)", callback_data="buy_6m")],
                [InlineKeyboardButton(text="⭐ 850 Stars — 12 месяцев (-35%)", callback_data="buy_12m")],
                [InlineKeyboardButton(text="🏢 Тарифы для работодателей", callback_data="become_b2b")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"subscription_menu error: {e}")

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    await message.answer(
        "⭐ <b>Подписка МигрантПро</b>\n\nВыберите тариф:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ {p['stars']} Stars — {p['label']}", callback_data=f"buy_{k}")]
            for k, p in STAR_PLANS.items()
        ])
    )

@dp.callback_query(F.data.startswith("buy_") & ~F.data.startswith("buy_b2b_"))
async def buy_plan(callback: types.CallbackQuery):
    plan_key = callback.data.replace("buy_", "")
    plan = STAR_PLANS.get(plan_key)
    if not plan:
        try:
            await callback.answer("Тариф не найден")
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
        return
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"🛡️ МигрантПро — {plan['label']}",
            description=f"Подписка на {plan['label']}",
            payload=f"sub_{plan_key}_{callback.from_user.id}",
            currency="XTR",
            prices=[types.LabeledPrice(label=plan['label'], amount=plan['stars'])],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"⭐ Оплатить {plan['stars']} Stars", pay=True)
            ]])
        )
        try:
            await callback.answer()
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
    except Exception as e:
        log.error(f"Invoice error for plan {plan_key}: {e}")

@dp.callback_query(F.data == "become_b2b")
async def become_b2b(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "🏢 <b>Тарифы для работодателей</b>\n\n"
            "Контролируйте документы всех сотрудников-мигрантов в одном месте:\n\n"
            "✅ Отслеживание сроков патентов, РВП, ВНЖ\n"
            "✅ Уведомления когда нужно продлить\n"
            "✅ Список всех сотрудников\n"
            "✅ Защита от штрафов МВД (до 1 млн руб.)\n\n"
            "Выберите тариф:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ 500 Stars — до 10 сотрудников/мес", callback_data="buy_b2b_10")],
                [InlineKeyboardButton(text="⭐ 1200 Stars — до 30 сотрудников/мес", callback_data="buy_b2b_30")],
                [InlineKeyboardButton(text="⭐ 3000 Stars — до 100 сотрудников/мес", callback_data="buy_b2b_100")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"become_b2b error: {e}")

@dp.callback_query(F.data.startswith("buy_b2b_"))
async def buy_b2b_plan(callback: types.CallbackQuery):
    plan_key = callback.data.replace("buy_", "")
    plan = B2B_PLANS.get(plan_key)
    if not plan:
        try:
            await callback.answer("Тариф не найден")
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
        return
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"🏢 МигрантПро B2B — {plan['label']}",
            description=f"Контроль сотрудников: {plan['label']} на 30 дней",
            payload=f"{plan_key}_{callback.from_user.id}",
            currency="XTR",
            prices=[types.LabeledPrice(label=plan['label'], amount=plan['stars'])],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"⭐ Оплатить {plan['stars']} Stars", pay=True)
            ]])
        )
        try:
            await callback.answer()
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
    except Exception as e:
        log.error(f"B2B invoice error for {plan_key}: {e}")

@dp.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    parts = payload.split("_")
    uid = message.from_user.id
    stars = payment.total_amount
    charge_id = payment.telegram_payment_charge_id

    try:
        # Определяем тип подписки
        if parts[0] == "sub":
            plan_key = parts[1]
            plan = STAR_PLANS.get(plan_key, STAR_PLANS["1m"])
            b2b_plan = None
        else:
            plan_key = "_".join(parts[:2])
            plan = {"days": 30, "label": plan_key}
            b2b_plan = B2B_PLANS.get(plan_key)

        now = datetime.now()
        expires = now + timedelta(days=plan["days"])
        expires_str = expires.strftime("%d.%m.%Y")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO subscriptions (user_id,plan,stars,days,activated_at,expires_at,charge_id) VALUES (?,?,?,?,?,?,?)",
                (uid, plan_key, stars, plan["days"], now.strftime("%d.%m.%Y %H:%M"), expires_str, charge_id)
            )

            # ФИКС: Разделили на два отдельных UPDATE вместо сломанной динамической строки
            await db.execute("UPDATE users SET sub_until=?, stars_total=stars_total+? WHERE user_id=?",
                             (expires_str, stars, uid))

            if b2b_plan:
                await db.execute(
                    "UPDATE users SET user_type='b2b', b2b_plan=?, b2b_limit=? WHERE user_id=?",
                    (plan_key, b2b_plan["limit"], uid)
                )

            await db.commit()

        await message.answer(
            f"🎉 <b>Подписка активирована!</b>\n\n"
            f"✅ Тариф: {plan['label']}\n"
            f"📅 Действует до: <b>{expires_str}</b>\n"
            f"⭐ Stars: {stars}",
            parse_mode="HTML",
            reply_markup=main_menu("b2b" if b2b_plan else "personal")
        )

        try:
            await bot.send_message(
                ADMIN_ID,
                f"💰 <b>ОПЛАТА</b>\n👤 @{message.from_user.username or '—'} ({uid})\n"
                f"📦 {plan['label']} | ⭐ {stars} | до {expires_str}",
                parse_mode="HTML"
            )
        except Exception as e:
            log.warning(f"Payment admin notify error: {e}")

    except Exception as e:
        log.error(f"successful_payment processing error: {e}")
        await message.answer("✅ Оплата получена! Обратитесь в поддержку если возникли проблемы.")

# ============================================================
# B2B ПАНЕЛЬ
# ============================================================
@dp.callback_query(F.data == "b2b_panel")
async def b2b_panel(callback: types.CallbackQuery):
    uid = callback.from_user.id
    user = await db_get_user(uid)
    employees = await db_get_employees(uid)
    limit = user.get("b2b_limit", 0) if user else 0

    all_docs = await db_get_docs(uid, owner_type="b2b")
    critical = 0
    for d in all_docs:
        try:
            dl = (datetime.strptime(d["expiry_date"], "%d.%m.%Y") - datetime.now()).days
            if dl <= 30:
                critical += 1
        except Exception as e:
            log.debug(f"b2b_panel doc parse error: {e}")

    try:
        await callback.message.edit_text(
            f"🏢 <b>Панель работодателя</b>\n\n"
            f"👥 Сотрудников: {len(employees)} / {limit}\n"
            f"⚠️ Требуют внимания: {critical}\n\n"
            f"Выберите действие:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 Список сотрудников", callback_data="emp_list")],
                [InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="emp_add")],
                [InlineKeyboardButton(text="⚠️ Истекающие документы", callback_data="emp_expiring")],
                [InlineKeyboardButton(text="👥 Подбор сотрудников", callback_data="er_menu")],
                [InlineKeyboardButton(text="⚠️ Риск-калькулятор штрафов", callback_data="risk_calc")],
                [InlineKeyboardButton(text="📬 Уведомить МВД о трудоустройстве", callback_data="notify_employment")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"b2b_panel error: {e}")

@dp.callback_query(F.data == "emp_list")
async def emp_list(callback: types.CallbackQuery):
    uid = callback.from_user.id
    employees = await db_get_employees(uid)
    if not employees:
        try:
            await callback.message.edit_text(
                "👥 Сотрудников нет.\nДобавьте первого сотрудника:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="emp_add")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="b2b_panel")],
                ])
            )
            await callback.answer()
        except Exception as e:
            log.debug(f"emp_list empty error: {e}")
        return

    text = "👥 <b>Ваши сотрудники:</b>\n\n"
    all_docs = await db_get_docs(uid, owner_type="b2b")

    for emp in employees[:20]:
        emp_docs = [d for d in all_docs if d.get("employee_id") == emp["id"]]
        status_icons = []
        for d in emp_docs:
            try:
                dl = (datetime.strptime(d["expiry_date"], "%d.%m.%Y") - datetime.now()).days
                dt = DOC_TYPES.get(d["doc_type"], DOC_TYPES["patent"])
                if dl < 0:
                    status_icons.append(f"{dt['emoji']}🔴")
                elif dl <= 14:
                    status_icons.append(f"{dt['emoji']}🟠")
                elif dl <= 30:
                    status_icons.append(f"{dt['emoji']}🟡")
                else:
                    status_icons.append(f"{dt['emoji']}🟢")
            except Exception as e:
                log.debug(f"emp_list doc parse error: {e}")
                status_icons.append("📋⚪")

        docs_str = " ".join(status_icons) if status_icons else "нет документов"
        text += f"• <b>{emp['full_name']}</b> ({emp['citizenship']})\n  {emp['position']} | {docs_str}\n\n"

    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="emp_add")],
                [InlineKeyboardButton(text="➕ Добавить документ сотруднику", callback_data="emp_doc_add")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="b2b_panel")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"emp_list edit error: {e}")

@dp.callback_query(F.data == "emp_expiring")
async def emp_expiring(callback: types.CallbackQuery):
    uid = callback.from_user.id
    employees = await db_get_employees(uid)
    all_docs = await db_get_docs(uid, owner_type="b2b")

    emp_map = {e["id"]: e for e in employees}
    expiring = []
    for d in all_docs:
        try:
            dl = (datetime.strptime(d["expiry_date"], "%d.%m.%Y") - datetime.now()).days
            if dl <= 30:
                emp = emp_map.get(d.get("employee_id", 0), {})
                dt = DOC_TYPES.get(d["doc_type"], DOC_TYPES["patent"])
                icon = "🔴" if dl < 0 else ("🟠" if dl <= 14 else "🟡")
                expiring.append(f"{icon} <b>{emp.get('full_name', '—')}</b> | {dt['emoji']} {d['number']} — {dl} дней")
        except Exception as e:
            log.debug(f"emp_expiring parse error: {e}")

    text = "⚠️ <b>Документы требующие внимания:</b>\n\n"
    text += "\n".join(expiring) if expiring else "✅ Все документы в порядке!"

    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Записаться на консультацию", callback_data="consult")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="b2b_panel")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"emp_expiring edit error: {e}")

@dp.callback_query(F.data == "emp_add")
async def emp_add_start(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    user = await db_get_user(uid)
    limit = user.get("b2b_limit", 0) if user else 0
    employees = await db_get_employees(uid)

    if len(employees) >= limit and limit > 0:
        try:
            await callback.message.edit_text(
                f"⚠️ Достигнут лимит сотрудников ({limit}).\nОбновите тариф для добавления новых.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Обновить тариф", callback_data="become_b2b")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="b2b_panel")],
                ])
            )
            await callback.answer()
        except Exception as e:
            log.debug(f"emp_add limit error: {e}")
        return

    await state.set_state(AddEmployeeState.full_name)
    try:
        await callback.message.edit_text(
            "➕ <b>Добавление сотрудника</b>\n\nШаг 1/5 — ФИО сотрудника:",
            parse_mode="HTML", reply_markup=back_btn()
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"emp_add_start error: {e}")

@dp.message(AddEmployeeState.full_name)
async def emp_full_name(message: types.Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(AddEmployeeState.citizenship)
    await message.answer("Шаг 2/5 — Гражданство (страна):")

@dp.message(AddEmployeeState.citizenship)
async def emp_citizenship(message: types.Message, state: FSMContext):
    await state.update_data(citizenship=message.text.strip())
    await state.set_state(AddEmployeeState.passport)
    await message.answer("Шаг 3/5 — Номер паспорта (серия и номер через пробел, если нет серии — только номер):")

@dp.message(AddEmployeeState.passport)
async def emp_passport(message: types.Message, state: FSMContext):
    parts = message.text.strip().split()
    if len(parts) >= 2:
        await state.update_data(passport_series=parts[0], passport_number=" ".join(parts[1:]))
    else:
        await state.update_data(passport_series="-", passport_number=message.text.strip())
    await state.set_state(AddEmployeeState.position)
    await message.answer("Шаг 4/5 — Должность/профессия:")

@dp.message(AddEmployeeState.position)
async def emp_position(message: types.Message, state: FSMContext):
    await state.update_data(position=message.text.strip())
    await state.set_state(AddEmployeeState.hired_date)
    await message.answer("Шаг 5/5 — Дата трудоустройства (ДД.ММ.ГГГГ или «-» если неизвестно):")

@dp.message(AddEmployeeState.hired_date)
async def emp_hired_date(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id

    emp_id = await db_add_employee(
        uid, data["full_name"], data["citizenship"],
        data.get("passport_series", "-"), data.get("passport_number", "-"),
        data["position"], message.text.strip()
    )

    await message.answer(
        f"✅ <b>Сотрудник добавлен!</b>\n\n"
        f"👤 {data['full_name']}\n"
        f"🌍 {data['citizenship']}\n"
        f"💼 {data['position']}\n\n"
        f"Теперь добавьте документы сотрудника (патент, РВП и т.д.)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить документ сотруднику", callback_data="emp_doc_add")],
            [InlineKeyboardButton(text="👥 Список сотрудников", callback_data="emp_list")],
        ])
    )

@dp.callback_query(F.data == "emp_doc_add")
async def emp_doc_add(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    employees = await db_get_employees(uid)
    if not employees:
        try:
            await callback.message.edit_text("Сначала добавьте сотрудника.", reply_markup=back_btn("b2b_panel"))
            await callback.answer()
        except Exception as e:
            log.debug(f"emp_doc_add empty error: {e}")
        return

    buttons = [[InlineKeyboardButton(text=f"👤 {e['full_name']}", callback_data=f"emp_doc_sel_{e['id']}")]
               for e in employees[:10]]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="b2b_panel")])
    try:
        await callback.message.edit_text(
            "Выберите сотрудника для добавления документа:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"emp_doc_add select error: {e}")

@dp.callback_query(F.data.startswith("emp_doc_sel_"))
async def emp_doc_sel(callback: types.CallbackQuery, state: FSMContext):
    emp_id = int(callback.data.replace("emp_doc_sel_", ""))
    await state.update_data(employee_id=emp_id)
    await state.set_state(AddEmpDocState.doc_type)
    try:
        await callback.message.edit_text(
            "Тип документа сотрудника:",
            reply_markup=doc_type_kb("emp_doc_type_")
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"emp_doc_sel error: {e}")

@dp.callback_query(F.data.startswith("emp_doc_type_"))
async def emp_doc_type(callback: types.CallbackQuery, state: FSMContext):
    doc_type = callback.data.replace("emp_doc_type_", "")
    await state.update_data(doc_type=doc_type)
    await state.set_state(AddEmpDocState.series)
    dt = DOC_TYPES.get(doc_type, DOC_TYPES["patent"])
    try:
        await callback.message.edit_text(
            f"{dt['emoji']} Серия документа сотрудника (или «-»):",
            reply_markup=back_btn()
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"emp_doc_type error: {e}")

@dp.message(AddEmpDocState.series)
async def emp_doc_series(m: types.Message, state: FSMContext):
    await state.update_data(series=m.text.strip())
    await state.set_state(AddEmpDocState.number)
    await m.answer("Номер документа:")

@dp.message(AddEmpDocState.number)
async def emp_doc_number(m: types.Message, state: FSMContext):
    await state.update_data(number=m.text.strip())
    await state.set_state(AddEmpDocState.issue_date)
    await m.answer("Дата выдачи (ДД.ММ.ГГГГ):")

@dp.message(AddEmpDocState.issue_date)
async def emp_doc_issue(m: types.Message, state: FSMContext):
    try:
        datetime.strptime(m.text.strip(), "%d.%m.%Y")
    except ValueError:
        await m.answer("❌ Неверный формат. Введите ДД.ММ.ГГГГ:")
        return
    await state.update_data(issue_date=m.text.strip())
    await state.set_state(AddEmpDocState.expiry_date)
    await m.answer("Дата окончания (ДД.ММ.ГГГГ):")

@dp.message(AddEmpDocState.expiry_date)
async def emp_doc_expiry(m: types.Message, state: FSMContext):
    try:
        datetime.strptime(m.text.strip(), "%d.%m.%Y")
    except ValueError:
        await m.answer("❌ Неверный формат. Введите ДД.ММ.ГГГГ:")
        return
    await state.update_data(expiry_date=m.text.strip())
    await state.set_state(AddEmpDocState.region)
    await m.answer("Регион действия (или «-»):")

@dp.message(AddEmpDocState.region)
async def emp_doc_region(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid = m.from_user.id
    dt = DOC_TYPES.get(data["doc_type"], DOC_TYPES["patent"])
    exp = datetime.strptime(data["expiry_date"], "%d.%m.%Y")
    dl = (exp - datetime.now()).days

    await db_add_doc(
        uid, data["doc_type"], data.get("series", "-"), data["number"],
        "-", "-", data["issue_date"], data["expiry_date"], m.text.strip(),
        owner_type="b2b", employee_id=data.get("employee_id", 0)
    )
    await m.answer(
        f"✅ Документ сотрудника добавлен!\n\n"
        f"{dt['emoji']} {dt['name']}\n"
        f"№: {data.get('series', '')} {data['number']}\n"
        f"До: {data['expiry_date']} (через {dl} дней)\n\n"
        f"🔔 Напоминания настроены!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 К списку сотрудников", callback_data="emp_list")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
        ])
    )

# ============================================================
# РЕФЕРАЛЬНАЯ СИСТЕМА
# ============================================================
@dp.callback_query(F.data == "referral")
async def referral_menu(callback: types.CallbackQuery):
    uid = callback.from_user.id
    try:
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{uid}"
    except Exception as e:
        log.error(f"get_me error: {e}")
        ref_link = f"https://t.me/migr_pomoshnik_bot?start=ref_{uid}"

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*), SUM(bonus_days) FROM referrals WHERE referrer_id=?", (uid,)) as cur:
                row = await cur.fetchone()
        invited = row[0] or 0
        days = row[1] or 0
    except Exception as e:
        log.error(f"referral stats error: {e}")
        invited = days = 0

    try:
        await callback.message.edit_text(
            f"🎁 <b>Пригласи друга — получи +7 дней!</b>\n\n"
            f"👥 Приглашено: {invited}\n"
            f"🎁 Дней заработано: {days}\n\n"
            f"🔗 Ваша ссылка:\n<code>{ref_link}</code>\n\n"
            f"Отправьте ссылку другу-мигранту — оба получите +7 дней подписки!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Поделиться ссылкой",
                                      url=f"https://t.me/share/url?url={ref_link}&text=Помощник по документам для мигрантов в России!")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.debug(f"referral_menu error: {e}")

# ============================================================
# ОБЫЧНЫЕ СООБЩЕНИЯ
# ============================================================
# ============================================================
# ФОТО ДОКУМЕНТА — AI АНАЛИЗ
# ============================================================
@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    """Полноценный OCR с извлечением всех полей документа."""
    if message.chat.type in ["group", "supergroup"]:
        return
    if await state.get_state() is not None:
        return

    uid = message.from_user.id
    log.info(f"OCR: фото от {uid}")
    await track_action(uid, "ocr_photo", "")

    thinking = await message.answer("🔍 <b>Анализирую документ...</b>\n⏳ Распознаю тип, даты, номера...", parse_mode="HTML")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        photo_b64 = base64.b64encode(file_bytes.read()).decode()

        OCR_PROMPT = """Ты OCR система для миграционных документов России.
Посмотри на документ и верни ТОЛЬКО JSON (без markdown):
{
  "doc_type": "патент|рвп|рвпо|внж|миграционная карта|паспорт|другое|unknown",
  "series": "серия или null",
  "number": "номер или null",
  "blank_series": "серия бланка или null",
  "blank_number": "номер бланка или null",
  "full_name": "ФИО или null",
  "birth_date": "ДД.ММ.ГГГГ или null",
  "issue_date": "ДД.ММ.ГГГГ или null",
  "expiry_date": "ДД.ММ.ГГГГ или null",
  "region": "регион или null",
  "employer": "работодатель или null",
  "citizenship": "гражданство или null",
  "days_left": число дней до окончания или null,
  "risks": [],
  "quality": "хорошее|нечёткое|плохое",
  "summary": "краткое описание на русском"
}"""

        result = None
        raw = ""
        for attempt in range(3):
            try:
                resp = await asyncio.wait_for(
                    claude.messages.create(
                        model="claude-3-5-sonnet-latest",
                        max_tokens=1000,
                        messages=[{"role": "user", "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": photo_b64}},
                            {"type": "text", "text": OCR_PROMPT}
                        ]}]
                    ), timeout=35.0
                )
                raw = resp.content[0].text.strip()
                clean = raw.replace("```json","").replace("```","").strip()
                result = json.loads(clean)
                break
            except json.JSONDecodeError:
                result = {"doc_type": "unknown", "quality": "нечёткое", "summary": raw[:200]}
                break
            except asyncio.TimeoutError:
                log.warning(f"OCR timeout attempt {attempt+1} uid={uid}")
                if attempt < 2: await asyncio.sleep(2)
            except Exception as e:
                log.error(f"OCR error attempt {attempt+1} uid={uid}: {e}")
                if attempt < 2: await asyncio.sleep(2)

        try:
            await thinking.delete()
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")

        if not result:
            await message.answer("❌ Не удалось распознать. Сфотографируйте чётче или добавьте вручную.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить вручную", callback_data="add_doc")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="start")],
                ]))
            return

        await save_ocr_result(uid, result.get("doc_type","unknown"), raw, result,
                              "high" if result.get("number") else "low")

        icons = {"патент":"🪪","рвп":"📘","рвпо":"📗","внж":"🏠","миграционная карта":"📄","паспорт":"🛂"}
        icon = icons.get((result.get("doc_type") or "").lower(), "📋")
        dl = result.get("days_left")
        if dl is not None:
            if dl < 0: st = f"🔴 ПРОСРОЧЕН на {abs(dl)} дн!"
            elif dl <= 14: st = f"🔴 {dl} дней — СРОЧНО!"
            elif dl <= 30: st = f"🟠 {dl} дней"
            else: st = f"🟢 {dl} дней"
        else:
            st = "⚪ Дата не определена"

        out = [f"{icon} <b>{(result.get('doc_type') or 'Документ').upper()}</b>\n"]
        if result.get("full_name"): out.append(f"👤 ФИО: {result['full_name']}")
        if result.get("series") or result.get("number"): out.append(f"📋 Серия/Номер: {result.get('series','')} {result.get('number','')}")
        if result.get("blank_number") and result.get("blank_number") not in (None,"-","null"): out.append(f"📋 Бланк: {result.get('blank_series','')} {result['blank_number']}")
        if result.get("issue_date"): out.append(f"📅 Выдан: {result['issue_date']}")
        if result.get("expiry_date"): out.append(f"⏰ До: {result['expiry_date']}")
        if result.get("region"): out.append(f"📍 Регион: {result['region']}")
        if result.get("employer"): out.append(f"🏢 Работодатель: {result['employer']}")
        if result.get("citizenship"): out.append(f"🌍 Гражданство: {result['citizenship']}")
        out.append(f"\n{st}")
        if result.get("risks"): out.append(f"⚠️ Риски: {', '.join(result['risks'])}")
        if result.get("quality") in ("плохое","нечёткое"): out.append(f"\n📸 Качество: {result['quality']} — попробуйте переснять")

        can_save = bool(result.get("number") and result.get("expiry_date"))
        kb = []
        if can_save: kb.append([InlineKeyboardButton(text="✅ Сохранить в документы", callback_data="add_doc")])
        kb.append([InlineKeyboardButton(text="📋 Консультация юриста", callback_data="consult")])
        kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")])

        await message.answer("\n".join(out), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

        if dl is not None and dl <= 14:
            try:
                await bot.send_message(ADMIN_ID,
                    f"⚠️ <b>OCR: Критический документ</b>\n"
                    f"👤 @{message.from_user.username or '—'} ({uid})\n"
                    f"📋 {result.get('doc_type','?')}: {result.get('number','?')}\n"
                    f"⏰ До: {result.get('expiry_date','?')} ({dl} дн)",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="✍️ Написать", url=f"tg://user?id={uid}")
                    ]]))
            except Exception as _e:
                log.warning(f"Suppressed: {_e}")

        log.info(f"OCR OK uid={uid} type={result.get('doc_type')} days_left={dl}")

    except Exception as e:
        log.error(f"OCR critical uid={uid}: {e}")
        try:
            await thinking.delete()
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
        await message.answer("❌ Ошибка. Добавьте вручную.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить вручную", callback_data="add_doc")]
            ]))


@dp.message(F.web_app_data)
async def handle_web_app_data(message: types.Message, state: FSMContext):
    """Обработчик данных из Mini App (tg.sendData)"""
    uid = message.from_user.id
    user = await db_get_user(uid)

    try:
        import json as _json
        data = _json.loads(message.web_app_data.data)
        action = data.get("action", "")
        log.info(f"web_app_data from {uid}: action={action} data={data}")
    except Exception as e:
        log.error(f"web_app_data parse error: {e}")
        return

    # ── КОНСУЛЬТАЦИЯ из Mini App ──────────────────────────────
    if action == "consult":
        topic    = data.get("topic", "Другое")
        name     = data.get("name") or message.from_user.full_name or "—"
        phone    = data.get("phone", "—")
        city     = data.get("city", "—")
        urgency  = data.get("urgency", "🟡 Из приложения")
        username = message.from_user.username
        lang     = data.get("lang", "ru")

        # Сохраняем в БД
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO consultations (user_id,name,phone,city,topic,urgency,created_at) VALUES (?,?,?,?,?,?,?)",
                    (uid, name, phone, city, topic, urgency,
                     datetime.now().strftime("%d.%m.%Y %H:%M"))
                )
                await db.commit()
        except Exception as e:
            log.error(f"Consult save from webapp error: {e}")

        # Уведомление администратору с кнопками
        urgency_icon = "🔴" if "СРОЧНО" in urgency or "срочн" in topic.lower() else "🟡"
        tg_link = f"@{username}" if username else f"tg://user?id={uid}"
        admin_text = (
            f"{urgency_icon} <b>НОВАЯ ЗАЯВКА НА КОНСУЛЬТАЦИЮ</b>\n\n"
            f"👤 <b>Имя:</b> {name}\n"
            f"📞 <b>Телефон:</b> {phone}\n"
            f"🏙️ <b>Город:</b> {city}\n"
            f"📂 <b>Тема:</b> {topic}\n"
            f"⚡ <b>Срочность:</b> {urgency}\n"
            f"🆔 <b>Telegram:</b> {tg_link} (<code>{uid}</code>)\n"
            f"🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💬 Написать клиенту",
                url=f"tg://user?id={uid}"
            )],
            [InlineKeyboardButton(
                text="👤 Профиль клиента",
                url=f"https://t.me/{username}" if username else f"tg://user?id={uid}"
            )],
        ])
        try:
            await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_kb)
        except Exception as e:
            log.error(f"Admin consult notify (webapp) error: {e}")

        # Ответ пользователю — предлагаем написать напрямую
        user_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💬 Написать юристу сейчас",
                url=f"https://t.me/Temirov_official"
            )],
        ])
        try:
            await message.answer(
                f"✅ <b>Заявка принята!</b>\n\n"
                f"📂 Тема: <b>{topic}</b>\n\n"
                f"Юрист свяжется с вами в ближайшее время.\n"
                f"Или напишите напрямую 👇",
                parse_mode="HTML",
                reply_markup=user_kb
            )
        except Exception as e:
            log.debug(f"Consult reply error: {e}")

        await track_action(uid, "consult_webapp", topic)
        return

    # ── Другие действия из Mini App ───────────────────────────
    if action == "ocr_photo":
        await handle_text.__wrapped__(message, state) if hasattr(handle_text, '__wrapped__') else None
        return

    log.debug(f"web_app_data unknown action={action} from {uid}")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message, state: FSMContext):
    if message.chat.type in ["group", "supergroup"]:
        return
    if await state.get_state() is None:
        user = await db_get_user(message.from_user.id)
        user_type = user.get("user_type", "personal") if user else "personal"
        await message.answer("Воспользуйтесь меню 👇", reply_markup=main_menu(user_type))

# ============================================================
# ADMIN PANEL
# ============================================================
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        total = await db_count_users()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM referrals") as c:
                ref = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM subscriptions") as c:
                subs = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM employees") as c:
                emps = (await c.fetchone())[0]
            async with db.execute("SELECT SUM(stars) FROM subscriptions") as c:
                r = await c.fetchone()
                stars = r[0] or 0
            async with db.execute("SELECT COUNT(*) FROM consultations") as c:
                consults = (await c.fetchone())[0]

        await message.answer(
            f"🔧 <b>Панель администратора</b>\n\n"
            f"👥 Пользователей: {total}\n"
            f"📋 Заявок на консультацию: {consults}\n"
            f"👔 Сотрудников добавлено: {emps}\n"
            f"🔗 Рефералов: {ref}\n"
            f"💳 Подписок: {subs}\n"
            f"⭐ Stars заработано: {stars}\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📣 Рассылка", callback_data="broadcast")],
                [InlineKeyboardButton(text="📰 Сгенерировать посты", callback_data="check_news_now")],
            ])
        )
    except Exception as e:
        log.error(f"admin_panel error: {e}")
        await message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data == "broadcast")
async def start_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(BroadcastState.message)
    await callback.message.answer("📣 Введите текст рассылки:")
    try:
        await callback.answer()
    except Exception as e:
        log.debug(f"broadcast answer error: {e}")

@dp.message(BroadcastState.message)
async def do_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    sent = failed = 0
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users") as cur:
                rows = await cur.fetchall()
        for (uid,) in rows:
            try:
                await bot.send_message(uid, f"📢 {message.text}", parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                log.debug(f"Broadcast send to {uid} failed: {e}")
                failed += 1
        await message.answer(f"✅ Рассылка: {sent} ок, {failed} ошибок")
    except Exception as e:
        log.error(f"do_broadcast error: {e}")
        await message.answer(f"Ошибка рассылки: {e}")

@dp.callback_query(F.data == "check_news_now")
async def check_news_now_cb(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    try:
        await callback.answer("Генерирую посты...")
    except Exception as e:
        log.debug(f"check_news_now answer error: {e}")
    await callback.message.answer("⏳ Генерирую посты по миграционным темам...")
    await run_news_cycle()

# ============================================================
# НОВОСТИ — ФИКС: AsyncAnthropic + правильная модель + дедупликация
# ============================================================
async def generate_ai_post(topic: str) -> str | None:
    """Генерация визуального поста в стиле Migration Alliance."""
    log.info(f"Generating visual post for: {topic[:60]}")
    visual_prompt = (
        "Ты контент-менеджер Telegram-канала Migration Alliance.\n\n"
        f"Напиши пост на тему: {topic}\n\n"
        "ФОРМАТ (обязательно):\n"
        "— Большая эмодзи + ЖИРНЫЙ ЗАГОЛОВОК\n"
        "— 2-3 факта с эмодзи\n"
        "— Практический совет\n"
        "— Предупреждение если есть\n"
        "— 📲 @migr_pomoshnik_bot\n"
        "— 3-4 хэштега\n\n"
        "Стиль: динамичный, Instagram-формат. 120-150 слов. Русский."
    )
    answer = await ask_claude([{"role": "user", "content": visual_prompt}], max_tokens=600)
    if not answer:
        log.warning(f"AI post failed for: {topic[:50]}")
    return answer



async def generate_simple_post(topic: str) -> str:
    """Fallback пост с визуальной структурой когда AI недоступен."""
    import random
    emojis = ["📋", "⚡", "🔔", "📌", "🇷🇺", "✅"]
    emoji = random.choice(emojis)
    return (
        f"{emoji} <b>{topic.upper()}</b>\n\n"
        f"Актуальная информация для иностранных граждан, работающих в России.\n\n"
        f"✅ Следите за сроками документов\n"
        f"✅ Вовремя оплачивайте патент\n"
        f"✅ Уведомляйте МВД об изменениях\n\n"
        f"💡 <b>Есть вопросы?</b> Наш AI-юрист ответит за минуту.\n\n"
        f"📲 @migr_pomoshnik_bot\n\n"
        f"#миграция #патент #мигранты #россия"
    )

# ФИКС: Публикация поста с полным логированием ошибок
async def send_post_for_approval(topic: str, post_text: str):
    pid = hashlib.md5(topic.encode()).hexdigest()[:8]
    pending_posts[pid] = {"text": post_text, "topic": topic}
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📰 <b>ПОСТ НА ОДОБРЕНИЕ</b>\n"
            f"📌 Тема: {topic}\n\n"
            f"━━━━━━━\n{post_text[:3000]}\n━━━━━━━",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pub_{pid}"),
                InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip_{pid}"),
            ]])
        )
        log.info(f"Post sent for approval: {pid} | {topic[:50]}")
    except Exception as e:
        log.error(f"send_post_for_approval error: {e}")

# ФИКС: Полное логирование ошибок публикации
@dp.callback_query(F.data.startswith("pub_"))
async def publish_post(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        try:
            await callback.answer("Нет доступа")
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
        return

    pid = callback.data.replace("pub_", "")
    post = pending_posts.get(pid)

    if not post:
        log.warning(f"publish_post: post {pid} not found in pending_posts (возможно бот перезапускался)")
        try:
            await callback.answer("❌ Пост не найден — возможно бот перезапускался")
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
        return

    # Пробуем опубликовать
    try:
        await bot.send_message(
            CHANNEL_ID,
            post["text"],
            parse_mode=None  # ФИКС: без parse_mode чтобы HTML теги не ломали публикацию
        )
        pending_posts.pop(pid, None)
        log.info(f"Post published: {pid} to {CHANNEL_ID}")

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            log.debug(f"edit_reply_markup after publish error: {e}")

        try:
            await callback.answer("✅ Опубликовано!")
        except Exception as e:
            log.debug(f"callback.answer after publish error: {e}")

        await callback.message.answer(f"✅ <b>Опубликовано в канал {CHANNEL_ID}!</b>", parse_mode="HTML")

    except Exception as e:
        log.error(f"PUBLISH ERROR for post {pid} to channel {CHANNEL_ID}: {e}")
        try:
            await callback.answer(f"❌ Ошибка публикации!")
        except Exception as _e:
            log.warning(f"Suppressed: {_e}")
        await callback.message.answer(
            f"❌ <b>Ошибка публикации!</b>\n\n"
            f"Канал: <code>{CHANNEL_ID}</code>\n"
            f"Ошибка: <code>{str(e)[:200]}</code>\n\n"
            f"Проверьте:\n"
            f"• Бот добавлен как администратор в канал\n"
            f"• CHANNEL_ID указан верно (@username или -100xxxxxxxxxx)\n"
            f"• Бот имеет право публиковать сообщения",
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("skip_"))
async def skip_post(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    pid = callback.data.replace("skip_", "")
    pending_posts.pop(pid, None)
    log.info(f"Post skipped: {pid}")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Пропущено")
    except Exception as e:
        log.debug(f"skip_post error: {e}")

async def run_news_cycle():
    """ФИКС: Персистентная дедупликация + async AI"""
    # Фильтруем уже использованные темы
    available_topics = []
    for topic in AI_POST_TOPICS:
        if not await is_topic_seen(topic):
            available_topics.append(topic)

    # Если все темы использованы — сбрасываем (начинаем по кругу)
    if not available_topics:
        log.info("All topics seen, resetting seen_news table")
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM seen_news")
                await db.commit()
        except Exception as e:
            log.error(f"seen_news reset error: {e}")
        available_topics = AI_POST_TOPICS[:]

    topics = random.sample(available_topics, min(2, len(available_topics)))

    for topic in topics:
        log.info(f"Generating post for topic: {topic[:60]}")
        post = await generate_ai_post(topic)
        if not post:
            log.warning(f"AI generation failed, using simple post for: {topic[:60]}")
            post = await generate_simple_post(topic)

        if post:
            await send_post_for_approval(topic, post)
            await mark_topic_seen(topic)
            await asyncio.sleep(3)

# ============================================================
# НАПОМИНАНИЯ
# ============================================================
async def reminders_scheduler():
    while True:
        try:
            today = datetime.now().strftime("%d.%m.%Y")
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("""
                    SELECT r.id, r.user_id, d.doc_type, d.series, d.number, d.expiry_date, d.owner_type, d.employee_id
                    FROM reminders r
                    JOIN documents d ON r.doc_id = d.id
                    WHERE r.remind_at = ? AND r.sent = 0
                """, (today,)) as cur:
                    rows = await cur.fetchall()

                for row in rows:
                    rid, uid, doc_type, series, number, expiry, owner_type, emp_id = row
                    try:
                        dl = (datetime.strptime(expiry, "%d.%m.%Y") - datetime.now()).days
                        dt = DOC_TYPES.get(doc_type, DOC_TYPES["patent"])

                        if owner_type == "b2b" and emp_id:
                            async with db.execute("SELECT full_name FROM employees WHERE id=?", (emp_id,)) as ec:
                                er = await ec.fetchone()
                            emp_name = er[0] if er else "Сотрудник"
                            msg = (
                                f"⏰ <b>Напоминание о документе сотрудника!</b>\n\n"
                                f"👤 {emp_name}\n"
                                f"{dt['emoji']} {dt['name']}: {series} {number}\n"
                                f"⏳ Истекает: <b>{expiry}</b> (через {dl} дней)\n\n"
                                f"{'🔴 СРОЧНО продлите!' if dl <= 14 else '📋 Подготовьте документы для продления.'}"
                            )
                        else:
                            msg = (
                                f"⏰ <b>Напоминание!</b>\n\n"
                                f"{dt['emoji']} {dt['name']}: {series} {number}\n"
                                f"⏳ Истекает: <b>{expiry}</b> (через {dl} дней)\n\n"
                                f"{'🔴 СРОЧНО продлите!' if dl <= 14 else '📋 Пора готовить документы для продления.'}"
                            )

                        await bot.send_message(
                            uid, msg, parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="📊 Мой статус", callback_data="health_score"),
                                InlineKeyboardButton(text="📋 Консультация", callback_data="consult")
                            ]])
                        )
                        await db.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
                        log.info(f"Reminder sent to {uid} for {doc_type} {number}")
                    except Exception as e:
                        log.error(f"Reminder send error for user {uid}, reminder {rid}: {e}")

                await db.commit()
        except Exception as e:
            log.error(f"reminders_scheduler error: {e}")
        await asyncio.sleep(3600)

# ============================================================
# ПЛАНИРОВЩИК НОВОСТЕЙ
# ============================================================
async def news_scheduler():
    await asyncio.sleep(60)  # Ждём 1 минуту после запуска
    while True:
        try:
            log.info("News scheduler: starting news cycle")
            await run_news_cycle()
            log.info("News scheduler: cycle complete, sleeping 4 hours")
        except Exception as e:
            log.error(f"news_scheduler error: {e}")
        await asyncio.sleep(4 * 60 * 60)

# ============================================================
# ЗАПУСК
# ============================================================

# ============================================================
# FSM ДЛЯ НОВЫХ МОДУЛЕЙ
# ============================================================

# ============================================================
# FSM — ПАРТНЁРСКАЯ СИСТЕМА
# ============================================================
class EmployerRequestState(StatesGroup):
    company = State()
    city = State()
    count = State()
    doc_type = State()
    housing = State()
    salary = State()
    contact = State()

class MigrantWorkState(StatesGroup):
    name = State()
    phone = State()
    doc_type = State()
    city = State()

# ============================================================
# ПАРТНЁРСКИЙ МОДУЛЬ — МИГРАНТ "ХОЧУ РАБОТАТЬ"
# ============================================================
@dp.callback_query(F.data == "jobs_menu")
async def jobs_menu(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "💼 <b>Хочу работать в России</b>\n\n"
            "Мы сотрудничаем с проверенными работодателями:\n"
            "🏭 Склады и логистика\n"
            "🏗️ Строительство\n"
            "🛒 Ритейл (сети магазинов)\n"
            "🍽 Общественное питание\n"
            "🚛 Водители, грузчики\n\n"
            "✅ Только официальное трудоустройство\n"
            "✅ Проверка документов включена\n"
            "✅ Помощь с оформлением\n\n"
            "Оставьте заявку — подберём вакансию за 24 часа:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Хочу работать — оставить заявку", callback_data="migrant_work_start")],
                [InlineKeyboardButton(text="🏢 Я работодатель — нужны сотрудники", callback_data="employer_request_start")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.warning(f"jobs_menu failed: {e}")

@dp.callback_query(F.data == "migrant_work_start")
async def migrant_work_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(MigrantWorkState.name)
    try:
        await callback.message.edit_text(
            "✅ <b>Заявка на работу</b>\n\n"
            "Шаг 1/4 — Ваше имя (Фамилия Имя):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="jobs_menu")
            ]])
        )
        await callback.answer()
    except Exception as e:
        log.warning(f"migrant_work_start failed: {e}")

@dp.message(MigrantWorkState.name)
async def mw_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(MigrantWorkState.phone)
    await message.answer("📞 Шаг 2/4 — Ваш номер телефона:")

@dp.message(MigrantWorkState.phone)
async def mw_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await state.set_state(MigrantWorkState.doc_type)
    await message.answer(
        "📄 Шаг 3/4 — Какой документ для работы?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪪 Патент на работу", callback_data="mwdoc_patent")],
            [InlineKeyboardButton(text="📘 РВП / ВНЖ", callback_data="mwdoc_rvp")],
            [InlineKeyboardButton(text="🇰🇿🇧🇾 ЕАЭС (без патента)", callback_data="mwdoc_eaes")],
            [InlineKeyboardButton(text="❓ Документов нет, нужна помощь", callback_data="mwdoc_none")],
        ])
    )

@dp.callback_query(F.data.startswith("mwdoc_"))
async def mw_doc(callback: types.CallbackQuery, state: FSMContext):
    doc_map = {
        "mwdoc_patent": "🪪 Патент",
        "mwdoc_rvp": "📘 РВП / ВНЖ",
        "mwdoc_eaes": "🇰🇿 ЕАЭС (без патента)",
        "mwdoc_none": "❓ Нет документов"
    }
    await state.update_data(doc_type=doc_map.get(callback.data, "Не указан"))
    await state.set_state(MigrantWorkState.city)
    try:
        await callback.message.edit_text("📍 Шаг 4/4 — В каком городе хотите работать?")
        await callback.answer()
    except Exception as e:
        log.warning(f"mw_doc failed: {e}")

@dp.message(MigrantWorkState.city)
async def mw_city(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id

    # Сохраняем заявку в БД
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO job_applications (job_id,user_id,name,phone,doc_type,created_at) VALUES (?,?,?,?,?,?)",
                (0, uid, data.get("name"), data.get("phone"),
                 f"{data.get('doc_type')} | {message.text.strip()}",
                 datetime.now().strftime("%d.%m.%Y %H:%M"))
            )
            await db.commit()
        log.info(f"Migrant work application from {uid}: {data.get('name')}")
    except Exception as e:
        log.error(f"Migrant work application save error for {uid}: {e}")

    # Лид администратору
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💼 <b>НОВЫЙ КАНДИДАТ НА РАБОТУ</b>\n\n"
            f"👤 {data.get('name')}\n"
            f"📞 {data.get('phone')}\n"
            f"📄 Документ: {data.get('doc_type')}\n"
            f"📍 Город: {message.text.strip()}\n"
            f"🆔 @{message.from_user.username or '—'} ({uid})\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✍️ Написать кандидату", url=f"tg://user?id={uid}")
            ]])
        )
    except Exception as e:
        log.error(f"Failed to notify admin about work application from {uid}: {e}")

    await message.answer(
        "✅ <b>Заявка принята!</b>\n\n"
        "Наш специалист подберёт подходящие вакансии и\nсвяжется с вами в течение 24 часов.\n\n"
        "💡 Пока ждёте — проверьте ваши документы:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📄 Проверить мои документы", callback_data="my_docs")],
            [InlineKeyboardButton(text="🆘 Что делать если...", callback_data="what_if_menu")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
        ])
    )

# ============================================================
# ПАРТНЁРСКИЙ МОДУЛЬ — РАБОТОДАТЕЛЬ "НУЖНЫ СОТРУДНИКИ"
# ============================================================
@dp.callback_query(F.data == "employer_request_start")
async def employer_request_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(EmployerRequestState.company)
    try:
        await callback.message.edit_text(
            "🏢 <b>Заявка на подбор сотрудников</b>\n\n"
            "Работаем с:\n"
            "• Складами и логистикой\n"
            "• Строительными компаниями\n"
            "• Сетями розничной торговли\n"
            "• Производством и аутсорсом\n\n"
            "Шаг 1/7 — Название компании:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="jobs_menu")
            ]])
        )
        await callback.answer()
    except Exception as e:
        log.warning(f"employer_request_start failed: {e}")

@dp.message(EmployerRequestState.company)
async def er_company(message: types.Message, state: FSMContext):
    await state.update_data(company=message.text.strip())
    await state.set_state(EmployerRequestState.city)
    await message.answer("📍 Шаг 2/7 — Город / регион работы:")

@dp.message(EmployerRequestState.city)
async def er_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text.strip())
    await state.set_state(EmployerRequestState.count)
    await message.answer("👥 Шаг 3/7 — Сколько сотрудников нужно?")

@dp.message(EmployerRequestState.count)
async def er_count(message: types.Message, state: FSMContext):
    await state.update_data(count=message.text.strip())
    await state.set_state(EmployerRequestState.doc_type)
    await message.answer(
        "📄 Шаг 4/7 — Требования к документам сотрудников?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪪 Нужен патент на работу", callback_data="erdoc_patent")],
            [InlineKeyboardButton(text="📘 РВП или ВНЖ", callback_data="erdoc_rvp")],
            [InlineKeyboardButton(text="🇰🇿 Только ЕАЭС (Казахстан, Беларусь, Кыргызстан)", callback_data="erdoc_eaes")],
            [InlineKeyboardButton(text="📋 Любые легальные документы", callback_data="erdoc_any")],
        ])
    )

@dp.callback_query(F.data.startswith("erdoc_"))
async def er_doc(callback: types.CallbackQuery, state: FSMContext):
    doc_map = {
        "erdoc_patent": "Патент обязателен",
        "erdoc_rvp": "РВП или ВНЖ",
        "erdoc_eaes": "Только ЕАЭС",
        "erdoc_any": "Любые легальные документы"
    }
    await state.update_data(doc_type=doc_map.get(callback.data, "Любые"))
    await state.set_state(EmployerRequestState.housing)
    try:
        await callback.message.edit_text(
            "🏠 Шаг 5/7 — Предоставляете жильё сотрудникам?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Да, жильё есть", callback_data="erh_yes")],
                [InlineKeyboardButton(text="❌ Жильё не предоставляем", callback_data="erh_no")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.warning(f"er_doc failed: {e}")

@dp.callback_query(EmployerRequestState.housing, F.data.startswith("erh_"))
async def er_housing(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(housing="С жильём" if callback.data == "erh_yes" else "Без жилья")
    await state.set_state(EmployerRequestState.salary)
    try:
        await callback.message.edit_text(
            "💰 Шаг 6/7 — Зарплата (например: 60000-80000 ₽/мес):"
        )
        await callback.answer()
    except Exception as e:
        log.warning(f"er_housing failed: {e}")

@dp.message(EmployerRequestState.salary)
async def er_salary(message: types.Message, state: FSMContext):
    await state.update_data(salary=message.text.strip())
    await state.set_state(EmployerRequestState.contact)
    await message.answer("📞 Шаг 7/7 — Ваш контакт (телефон или @username):")

@dp.message(EmployerRequestState.contact)
async def er_contact(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id

    # Сохраняем в jobs как партнёрскую заявку
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO jobs (title,company,city,salary_from,description,doc_types,housing,contact,active,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,?)",
                (f"Заявка: {data.get('count')} чел.",
                 data.get("company"), data.get("city"), 0,
                 f"Нужно: {data.get('count')} сотрудников",
                 data.get("doc_type"), 1 if "жильём" in data.get("housing","") else 0,
                 message.text.strip(),
                 datetime.now().strftime("%d.%m.%Y %H:%M"))
            )
            await db.commit()
        log.info(f"Employer request from {uid}: {data.get('company')}, {data.get('count')} people")
    except Exception as e:
        log.error(f"Employer request save error for {uid}: {e}")

    # Лид администратору
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🏢 <b>ЗАЯВКА РАБОТОДАТЕЛЯ</b>\n\n"
            f"🏢 Компания: {data.get('company')}\n"
            f"📍 Город: {data.get('city')}\n"
            f"👥 Нужно: {data.get('count')} сотрудников\n"
            f"📄 Документы: {data.get('doc_type')}\n"
            f"🏠 Жильё: {data.get('housing')}\n"
            f"💰 Зарплата: {data.get('salary')}\n"
            f"📞 Контакт: {message.text.strip()}\n"
            f"🆔 @{message.from_user.username or '—'} ({uid})\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✍️ Написать работодателю", url=f"tg://user?id={uid}")
            ]])
        )
    except Exception as e:
        log.error(f"Failed to notify admin about employer request from {uid}: {e}")

    await message.answer(
        f"✅ <b>Заявка принята!</b>\n\n"
        f"🏢 {data.get('company')}\n"
        f"👥 {data.get('count')} сотрудников | 📍 {data.get('city')}\n\n"
        f"Наш менеджер свяжется с вами в течение 2 часов.\n\n"
        f"💼 Стоимость подбора обсуждается индивидуально.\n"
        f"Все кандидаты проходят проверку документов.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏢 Панель работодателя", callback_data="b2b_panel")],
            [InlineKeyboardButton(text="⚠️ Калькулятор рисков", callback_data="risk_calc")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
        ])
    )

# ============================================================
# ЖИЛЬЁ — упрощённо (только кнопка связи)
# ============================================================
@dp.callback_query(F.data == "housing_menu")
async def housing_menu(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(
            "🏠 <b>Жильё для мигрантов</b>\n\n"
            "Помогаем найти безопасное жильё рядом с местом работы.\n\n"
            "✅ Проверенные арендодатели\n"
            "✅ Без посредников\n"
            "✅ Все документы оформляются официально\n\n"
            "Оставьте заявку — пришлём варианты:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Найти жильё — оставить заявку", callback_data="migrant_work_start")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="start")],
            ])
        )
        await callback.answer()
    except Exception as e:
        log.warning(f"housing_menu failed: {e}")

# ============================================================
# РИСК-КАЛЬКУЛЯТОР ДЛЯ РАБОТОДАТЕЛЯ
# ============================================================
FINE_RATES = {
    "expired_doc": {"ul": (250000, 800000), "label": "Просроченный документ"},
    "no_notify":   {"ul": (400000, 1000000), "label": "Неуведомление МВД"},
    "no_contract": {"ul": (50000, 100000), "label": "Без трудового договора"},
    "wrong_region":{"ul": (250000, 800000), "label": "Работа не по региону патента"},
}

@dp.callback_query(F.data == "risk_calc")
async def risk_calc(callback: types.CallbackQuery):
    uid = callback.from_user.id
    employees = await db_get_employees(uid)

    if not employees:
        try:
            await callback.message.edit_text(
                "⚠️ <b>Риск-калькулятор</b>\n\nНет сотрудников. Добавьте сотрудников для расчёта рисков.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="emp_add")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="b2b_panel")],
                ])
            )
            await callback.answer()
        except Exception as e:
            log.warning(f"risk_calc empty failed: {e}")
        return

    all_docs = await db_get_docs(uid, owner_type="b2b")
    total_min, total_max = 0, 0
    risk_items = []

    for emp in employees:
        emp_docs = [d for d in all_docs if d.get("employee_id") == emp["id"]]
        if not emp_docs:
            risk_items.append(f"🔴 <b>{emp['full_name']}</b>: нет документов в системе")
            total_min += FINE_RATES["expired_doc"]["ul"][0]
            total_max += FINE_RATES["expired_doc"]["ul"][1]
            continue
        for doc in emp_docs:
            try:
                dl = (datetime.strptime(doc["expiry_date"], "%d.%m.%Y") - datetime.now()).days
                dt = DOC_TYPES.get(doc["doc_type"], DOC_TYPES["patent"])
                if dl < 0:
                    risk_items.append(
                        f"🔴 <b>{emp['full_name']}</b> — {dt['emoji']} просрочен {abs(dl)} дней\n"
                        f"   Штраф: до {FINE_RATES['expired_doc']['ul'][1]:,} ₽"
                    )
                    total_min += FINE_RATES["expired_doc"]["ul"][0]
                    total_max += FINE_RATES["expired_doc"]["ul"][1]
                elif dl <= 14:
                    risk_items.append(
                        f"🟠 <b>{emp['full_name']}</b> — {dt['emoji']} {dl} дней\n"
                        f"   Риск: до {FINE_RATES['expired_doc']['ul'][1]:,} ₽"
                    )
                    total_min += FINE_RATES["expired_doc"]["ul"][0] // 2
                    total_max += FINE_RATES["expired_doc"]["ul"][1] // 2
            except Exception as e:
                log.warning(f"risk_calc doc error: {e}")

    if not risk_items:
        text = "✅ <b>Рисков не обнаружено!</b>\n\nВсе документы сотрудников в порядке."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="b2b_panel")]
        ])
    else:
        text = (
            f"⚠️ <b>Риск-анализ компании</b>\n\n"
            f"{chr(10).join(risk_items[:10])}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💸 <b>Потенциальные штрафы:</b>\n"
            f"Минимум: <b>{total_min:,} ₽</b>\n"
            f"Максимум: <b>{total_max:,} ₽</b>\n\n"
            f"⚡ Устраните нарушения до проверки!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Нужна помощь юриста", callback_data="consult")],
            [InlineKeyboardButton(text="📬 Уведомить МВД", callback_data="notify_employment")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="b2b_panel")],
        ])

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await callback.answer()
    except Exception as e:
        log.warning(f"risk_calc result failed: {e}")

# ============================================================
# ADMIN ПАНЕЛЬ
# ============================================================
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as c:
                total = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM consultations") as c:
                consults = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM job_applications") as c:
                work_requests = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM jobs WHERE active=0") as c:
                employer_requests = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM subscriptions") as c:
                subs = (await c.fetchone())[0]
            async with db.execute("SELECT SUM(stars) FROM subscriptions") as c:
                r = await c.fetchone()
                stars = r[0] or 0
            async with db.execute("SELECT COUNT(*) FROM referrals") as c:
                refs = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM employees") as c:
                emps = (await c.fetchone())[0]
    except Exception as e:
        log.error(f"admin_panel db error: {e}")
        await message.answer(f"❌ Ошибка БД: {e}")
        return

    await message.answer(
        f"🔧 <b>МигрантПро — Админ панель</b>\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"👥 Пользователей: <b>{total}</b>\n"
        f"💳 Подписок: {subs} | ⭐ {stars} Stars\n"
        f"🔗 Рефералов: {refs}\n\n"
        f"📋 Заявок на консультацию: <b>{consults}</b>\n"
        f"💼 Кандидатов на работу: <b>{work_requests}</b>\n"
        f"🏢 Заявок от работодателей: <b>{employer_requests}</b>\n"
        f"👔 Сотрудников B2B: {emps}\n"
        f"📰 Постов в очереди: {len(pending_posts)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="broadcast"),
             InlineKeyboardButton(text="🔍 Тест AI", callback_data="test_ai")],
            [InlineKeyboardButton(text="📰 Сгенерировать посты", callback_data="check_news_now")],
        ])
    )



async def main():
    await init_db()
    from employer_requests_service import init_employer_requests_table
    await init_employer_requests_table()
    dp.include_router(er_router)
    log.info("Bot starting...")
    try:
        await bot.send_message(
            ADMIN_ID,
            "✅ <b>МигрантПро запущен!</b>\n\nМодуль партнёрского подбора активен.",
            parse_mode="HTML"
        )
    except Exception as e:
        log.warning(f"Admin startup notify failed: {e}")
    asyncio.create_task(news_scheduler())
    asyncio.create_task(reminders_scheduler())
    log.info("Starting polling...")

    # FIX 6: Auto-restart protection — polling не падает молча
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except Exception as e:
            log.error(f"Polling crashed: {type(e).__name__}: {e}")
            log.info("Restarting polling in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
