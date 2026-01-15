import os
import telebot
from telebot import types
import sqlite3
from datetime import datetime, date, timedelta
import time
import threading

# ==========================================
#               КОНФИГУРАЦИЯ
# ==========================================

TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = 844947566

# Цены
PRICE_VEG = 1.88
PRICE_FRESH = 0.99
PRICE_DRY = 1.08
PRICE_ALC = 1.08
PRICE_FREEZE = 1.36 

# Часы утренней смены
MORNING_START_HOUR = 5 
MORNING_END_HOUR = 11

# Названия кнопок (чтобы не путаться в тексте)
BTN_NEW_SHIFT = "🧮 Новая смена (по этапам)"
BTN_MONTH_TOTAL = "💰 Итог за месяц"
BTN_STATS = "📊 Статистика"
BTN_INFO = "ℹ️ Инфо"
BTN_SUPPORT = "💙 Поддержать проект"
BTN_PARAMS = "⚙️ Параметры"

# Кнопки меню Параметры
BTN_CHANGE_HIRE = "🗓 Изменить дату трудоустройства"
BTN_DEL_LAST = "🧻 Удалить последнюю смену"
BTN_RESET_MONTH = "🗑 Сбросить месяц"
BTN_REMIND_ON = "🔔 Настроить напоминания"
BTN_REMIND_OFF = "🔕 Выключить напоминания"
BTN_BACK = "⬅️ В главное меню"

# Кнопки Статистики
BTN_STAT_CURR_FULL = "📅 Текущий месяц (все смены)"
BTN_STAT_PREV_FULL = "📅 Прошлый месяц (все смены)"
BTN_STAT_CURR_1_15 = "📆 Текущий месяц: 1–15"
BTN_STAT_CURR_16_END = "📆 Текущий месяц: 16–конец"

# Часовой пояс
TIMEZONE_OFFSET = 5

bot = telebot.TeleBot(TOKEN)

# Глобальные переменные состояния (для пошаговых действий)
step_data = {}      # {chat_id: {...}}
hire_waiting = set()
cycle_waiting = set()

# ==========================================
#               БАЗА ДАННЫХ
# ==========================================

def init_db():
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            veg_qty REAL,
            fresh_qty REAL,
            dry_qty REAL,
            alc_qty REAL,
            freeze_qty REAL,
            total_income REAL,
            end_datetime TEXT,
            logical_month TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            hired_year INTEGER,
            hired_month INTEGER,
            remind_enabled INTEGER DEFAULT 0,
            preset_start_date TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ==========================================
#          ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def get_local_now():
    """Возвращает текущее время с учетом часового пояса пользователя"""
    return datetime.now() + timedelta(hours=TIMEZONE_OFFSET)

def is_morning():
    now = get_local_now()  # <-- Используем наше время
    h = now.hour
    return MORNING_START_HOUR <= h <= MORNING_END_HOUR


def months_diff(h_year, h_month):
    today = date.today()
    return (today.year - h_year) * 12 + (today.month - h_month)

def get_prev_month(year: int, month: int):
    if month == 1:
        return year - 1, 12
    return year, month - 1

def get_logical_month_for_now():
    now = get_local_now()  # <-- Изменено
    y, m = now.year, now.month
    if now.day == 1 and now.hour < 12:
        y, m = get_prev_month(y, m)
    return f"{y:04d}-{m:02d}"

def get_current_and_previous_logical_month():
    now = get_local_now()  # <-- Изменено
    y, m = now.year, now.month
    if now.day == 1 and now.hour < 12:
        cur_y, cur_m = get_prev_month(y, m)
    else:
        cur_y, cur_m = y, m

    prev_y, prev_m = get_prev_month(cur_y, cur_m)
    return f"{cur_y:04d}-{cur_m:02d}", f"{prev_y:04d}-{prev_m:02d}"


# --- Работа с пользователями (Users) ---

def get_experience_bonus(user_id):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT hired_year, hired_month FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or row[0] is None or row[1] is None:
        return 0.0
    h_year, h_month = row
    m = months_diff(h_year, h_month)
    if m < 6: return 0.0
    elif 6 <= m < 12: return 127.25
    elif 12 <= m < 24: return 254.0
    elif 24 <= m < 36: return 305.4
    else: return 381.75

def save_user_hire_date(user_id, year, month):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO users(user_id, hired_year, hired_month) VALUES(?, ?, ?) '
        'ON CONFLICT(user_id) DO UPDATE SET hired_year=excluded.hired_year, hired_month=excluded.hired_month',
        (user_id, year, month)
    )
    conn.commit()
    conn.close()

def get_user_hire_date(user_id):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT hired_year, hired_month FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def set_user_cycle_start(user_id, start_date_str):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO users(user_id, preset_start_date, remind_enabled) '
        'VALUES(?, ?, 1) '
        'ON CONFLICT(user_id) DO UPDATE SET '
        'preset_start_date=excluded.preset_start_date, '
        'remind_enabled=1',
        (user_id, start_date_str)
    )
    conn.commit()
    conn.close()

def disable_user_reminder(user_id):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET remind_enabled = 0 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def get_users_with_cycle():
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, preset_start_date FROM users WHERE remind_enabled = 1 AND preset_start_date IS NOT NULL')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_total_users():
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    result = cursor.fetchone()[0]
    conn.close()
    return result or 0

# --- Расчеты и работа со сменами (Shifts) ---

def calculate_income(veg, fresh, dry, alc, freeze, user_id):
    base = (veg * PRICE_VEG + fresh * PRICE_FRESH + dry * PRICE_DRY + alc * PRICE_ALC + freeze * PRICE_FREEZE)
    detail_lines = []
    detail_lines.append(f"Базовая сумма S = {base:.2f} руб.")

    if base > 1840:
        x = base - 1840
        y = x * 2.5
        z = y + 5404.9
        # Используем символ '×' вместо '*', чтобы не ломать Markdown
        detail_lines.append(f"S > 1840 → ({base:.2f} - 1840) × 2.5 + 5404.9 = {z:.2f}")
    else:
        z = base + 3104.9
        detail_lines.append(f"S ≤ 1840 → {base:.2f} + 3104.9 = {z:.2f}")

    if is_morning():
        z += 300
        detail_lines.append(f"Смена утром → +300 руб. = {z:.2f}")
    else:
        detail_lines.append(f"Смена не утром → без доплаты = {z:.2f}")

    bonus = get_experience_bonus(user_id)
    
    if bonus > 0:
        total = z + bonus
        detail_lines.append(f"Доплата за стаж: +{bonus:.2f} руб. = {total:.2f}")
    else:
        total = z
        detail_lines.append("Доплата за стаж не начисляется (меньше 6 месяцев).")

    return total, base, detail_lines, bonus


def save_shift(user_id, veg, fresh, dry, alc, freeze, total):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    
    now = get_local_now()  # <-- ВАЖНО: берем правильное время
    
    current_date = now.strftime("%Y-%m-%d")
    end_dt = now.strftime("%Y-%m-%d %H:%M:%S")
    logical_month = get_logical_month_for_now()
    
    cursor.execute('''
        INSERT INTO shifts (
            user_id, date, veg_qty, fresh_qty, dry_qty, alc_qty, freeze_qty,
            total_income, end_datetime, logical_month
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, current_date, veg, fresh, dry, alc, freeze, total, end_dt, logical_month))
    conn.commit()
    conn.close()


def get_month_sum_by_logical(user_id, logical_month):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT SUM(total_income) FROM shifts WHERE user_id = ? AND logical_month = ?', (user_id, logical_month))
    result = cursor.fetchone()[0]
    conn.close()
    return result if result else 0.0

def delete_last_shift(user_id):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM shifts WHERE user_id = ? ORDER BY id DESC LIMIT 1', (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    last_id = row[0]
    cursor.execute('DELETE FROM shifts WHERE id = ?', (last_id,))
    conn.commit()
    conn.close()
    return True

def delete_month_shifts(user_id, logical_month):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM shifts WHERE user_id = ? AND logical_month = ?', (user_id, logical_month))
    conn.commit()
    conn.close()

def get_shifts_by_logical_month(user_id, logical_month, date_from=None, date_to=None):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    query = '''SELECT date, end_datetime, total_income FROM shifts WHERE user_id = ? AND logical_month = ?'''
    params = [user_id, logical_month]
    
    if date_from and date_to:
        query += ' AND date BETWEEN ? AND ?'
        params.extend([date_from, date_to])
        
    query += ' ORDER BY end_datetime'
    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()
    conn.close()
    return rows

def format_shifts_list(rows):
    if not rows: return "Смен пока нет.", 0.0
    lines = []
    total = 0.0
    count = 0
    for date_str, end_dt, income in rows:
        try:
            if end_dt:
                dt = datetime.strptime(end_dt, "%Y-%m-%d %H:%M:%S")
                human = dt.strftime("%d.%m %H:%M")
            else:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                human = dt.strftime("%d.%m")
        except Exception:
            human = f"{date_str}"
        lines.append(f"{human} — ~{income:.2f} руб.")
        total += income
        count += 1
    avg = total / count if count else 0.0
    return "\n".join(lines), avg

def get_today_shifts_count(user_id=None):
    today = get_local_now().strftime("%Y-%m-%d")  # <-- Изменено
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    if user_id is None:
        cursor.execute('SELECT COUNT(*) FROM shifts WHERE date = ?', (today,))
    else:
        cursor.execute('SELECT COUNT(*) FROM shifts WHERE date = ? AND user_id = ?', (today, user_id))
    result = cursor.fetchone()[0]
    conn.close()
    return result or 0


def get_stats_30_days():
    """
    Считает статистику за последние 30 дней:
    1. Сколько всего было расчётов (смен).
    2. Сколько уникальных людей пользовалось ботом.
    """
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    
    # Вычисляем дату, которая была 30 дней назад
    date_30_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    # 1. Считаем общее количество смен (расчётов) за этот период
    cursor.execute('SELECT COUNT(*) FROM shifts WHERE date >= ?', (date_30_ago,))
    total_calcs = cursor.fetchone()[0] or 0
    
    # 2. Считаем уникальных пользователей (DISTINCT user_id)
    # Это покажет, сколько именно ЛЮДЕЙ заходило, даже если один человек сделал 100 расчётов.
    cursor.execute('SELECT COUNT(DISTINCT user_id) FROM shifts WHERE date >= ?', (date_30_ago,))
    active_users = cursor.fetchone()[0] or 0
    
    conn.close()
    return total_calcs, active_users


# --- Парсеры ---

def ask_number(message, text, next_handler, field_name):
    msg = bot.send_message(message.chat.id, text)
    bot.register_next_step_handler(msg, next_handler, field_name)

def parse_number_from_message(message):
    txt = message.text.replace(',', '.').strip()
    try:
        value = float(txt)
        if value < 0: raise ValueError
        return value
    except ValueError:
        return None

def parse_hire_date(text):
    t = text.strip().lower()
    months = {
        'январь': 1, 'января': 1, 'февраль': 2, 'февраля': 2, 'март': 3, 'марта': 3,
        'апрель': 4, 'апреля': 4, 'май': 5, 'мая': 5, 'июнь': 6, 'июня': 6,
        'июль': 7, 'июля': 7, 'август': 8, 'августа': 8, 'сентябрь': 9, 'сентября': 9,
        'октябрь': 10, 'октября': 10, 'ноябрь': 11, 'ноября': 11, 'декабрь': 12, 'декабря': 12
    }
    parts = t.split()
    if len(parts) != 2: return None, None
    month_word, year_str = parts
    month = months.get(month_word)
    try: year = int(year_str)
    except ValueError: return None, None
    return year, month

def parse_cycle_start_date(text):
    try:
        dt = datetime.strptime(text.strip(), "%d-%m-%Y")
        return dt.date()
    except Exception:
        return None

# ==========================================
#              КЛАВИАТУРЫ (UI)
# ==========================================

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    # 1 ряд
    markup.add(types.KeyboardButton(BTN_NEW_SHIFT))
    # 2 ряд
    markup.add(types.KeyboardButton(BTN_MONTH_TOTAL), types.KeyboardButton(BTN_STATS))
    # 3 ряд
    markup.add(types.KeyboardButton(BTN_INFO), types.KeyboardButton(BTN_SUPPORT))
    # 4 ряд - Параметры
    markup.add(types.KeyboardButton(BTN_PARAMS))
    return markup

def get_params_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    # Настройки стажа и смен
    markup.add(types.KeyboardButton(BTN_CHANGE_HIRE))
    markup.add(types.KeyboardButton(BTN_DEL_LAST), types.KeyboardButton(BTN_RESET_MONTH))
    # Напоминания
    markup.add(types.KeyboardButton(BTN_REMIND_ON), types.KeyboardButton(BTN_REMIND_OFF))
    # Назад
    markup.add(types.KeyboardButton(BTN_BACK))
    return markup

def get_stats_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton(BTN_STAT_CURR_FULL))
    markup.add(types.KeyboardButton(BTN_STAT_PREV_FULL))
    markup.add(types.KeyboardButton(BTN_STAT_CURR_1_15), types.KeyboardButton(BTN_STAT_CURR_16_END))
    markup.add(types.KeyboardButton(BTN_BACK))
    return markup

# ==========================================
#              ОБРАБОТЧИКИ
# ==========================================

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.chat.id
    hire = get_user_hire_date(user_id)
    
    # --- ВОТ ЗДЕСЬ НОВЫЙ ТЕКСТ ---
    intro = (
        "Привет! Я бот-помощник для склада. 🤖\n\n"
        "**Что я умею:**\n"
        "✅ **Считать зарплату:** учитываю коробки (алко, сухой, фреш и т.д.), ночные часы и стаж.\n"
        "✅ **Вести статистику:** сохраняю историю смен, показываю доход за месяц или за половину месяца.\n"
        "✅ **Напоминать о сменах:** могу присылать уведомления по твоему графику, чтобы ты не забыл посчитать доход за смену и записать результат.\n"
        "✅ **Управлять данными:** если ошибся, можно удалить последнюю смену или сбросить весь месяц.\n\n"
        "⚠️ _Расчёты примерные, доплата за подход к ячейкам не учитывается._"
    )

    if not hire:
        hire_waiting.add(user_id)
        bot.send_message(
            message.chat.id,
            intro + "\n\n🚀 **Для начала работы нужно настроить стаж.**\nНапиши месяц и год трудоустройства, например: `декабрь 2024`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
    else:
        bot.send_message(
            message.chat.id,
            intro + "\n\n👇 **Выбирай действие в меню ниже:**",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )


# --- Навигация по меню ---

@bot.message_handler(func=lambda m: m.text == BTN_PARAMS)
def menu_parameters(message):
    bot.send_message(message.chat.id, "⚙️ Меню параметров:", reply_markup=get_params_keyboard())

@bot.message_handler(func=lambda m: m.text == BTN_BACK)
def menu_back(message):
    start(message)

@bot.message_handler(func=lambda m: m.text == BTN_STATS)
def menu_stats(message):
    bot.send_message(message.chat.id, "📊 Выбери вариант статистики:", reply_markup=get_stats_keyboard())

# --- Обработчики из меню ПАРАМЕТРЫ ---

@bot.message_handler(func=lambda m: m.text == BTN_CHANGE_HIRE)
def handle_change_hire(message):
    user_id = message.chat.id
    hire_waiting.add(user_id)
    bot.send_message(
        message.chat.id,
        "Введи новую дату трудоустройства (например: `декабрь 2024`):",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: m.chat.id in hire_waiting)
def handle_hire_input(message):
    user_id = message.chat.id
    year, month = parse_hire_date(message.text)
    if not year or not month:
        bot.send_message(message.chat.id, "Не понял дату. Введи так: `декабрь 2024`.", parse_mode="Markdown")
        return
    save_user_hire_date(user_id, year, month)
    hire_waiting.discard(user_id)
    bot.send_message(message.chat.id, f"Дата трудоустройства сохранена: {message.text.strip()}")

@bot.message_handler(func=lambda m: m.text == BTN_REMIND_ON)
def handle_cycle_setup(message):
    user_id = message.chat.id
    cycle_waiting.add(user_id)
    bot.send_message(
        user_id,
        "Включаю напоминания.\nВведи дату первой дневной смены цикла (ДЕНЬ 1).\nФормат: `ДД-ММ-ГГГГ` (01-01-2025).",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: m.chat.id in cycle_waiting)
@bot.message_handler(func=lambda m: m.chat.id in cycle_waiting)
def handle_cycle_start_date(message):
    user_id = message.chat.id
    dt_start = parse_cycle_start_date(message.text)
    
    if not dt_start:
        bot.send_message(user_id, "Не понял дату. Пример: `01-01-2025`.", parse_mode="Markdown")
        return

    # Сохраняем настройки
    set_user_cycle_start(user_id, dt_start.strftime("%Y-%m-%d"))
    cycle_waiting.discard(user_id)

    # --- ГЕНЕРИРУЕМ ПРОГНОЗ НА 14 ДНЕЙ ---
    forecast_lines = []
    
    # Будем проверять каждый день, начиная с сегодня (по местному времени)
    today = get_local_now().date()
    
    for i in range(16): # Смотрим на 16 дней вперед
        check_date = today + timedelta(days=i)
        
        # Считаем, какой это день цикла относительно введенной даты старта
        delta = (check_date - dt_start).days
        if delta < 0: continue # Этот день был до начала цикла
        
        day_idx = delta % 8 # 0..7
        
        h, m = get_preset_time_for_day(day_idx)
        
        if h is not None:
            # Красивый формат даты
            date_str = check_date.strftime("%d.%m")
            # День недели (по-русски грубо, но понятно)
            wd = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"][check_date.weekday()]
            
            type_name = "День" if h == 19 else "Утро"
            forecast_lines.append(f"• {date_str} ({wd}) в {h:02d}:{m:02d} — {type_name}")

    forecast_text = "\n".join(forecast_lines)

    bot.send_message(
        user_id,
        f"✅ **Напоминания включены!**\n\n"
        f"Дата начала цикла: {dt_start.strftime('%d.%m.%Y')}\n"
        f"Ближайшие напоминания (по времени ЕКБ):\n\n"
        f"{forecast_text}\n\n"
        "_(Бот напишет, только если ты сам не записал смену раньше)_",
        parse_mode="Markdown"
    )


@bot.message_handler(func=lambda m: m.text == BTN_REMIND_OFF)
def handle_reminder_off(message):
    disable_user_reminder(message.chat.id)
    bot.send_message(message.chat.id, "🔕 Напоминания отключены.")

@bot.message_handler(func=lambda m: m.text == BTN_DEL_LAST)
def handle_delete_last(message):
    ok = delete_last_shift(message.chat.id)
    current_lm, _ = get_current_and_previous_logical_month()
    if ok:
        new_sum = get_month_sum_by_logical(message.chat.id, current_lm)
        bot.send_message(message.chat.id, f"Последняя смена удалена.\nНовая сумма за месяц: ~{new_sum:.2f} руб.")
    else:
        bot.send_message(message.chat.id, "Смен для удаления нет.")

@bot.message_handler(func=lambda m: m.text == BTN_RESET_MONTH)
def handle_reset_month(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=True)
    markup.add(types.KeyboardButton("✅ Да, удалить месяц"), types.KeyboardButton("❌ Нет, отмена"))
    bot.send_message(message.chat.id, "Удалить ВСЕ смены за текущий месяц?", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "✅ Да, удалить месяц")
def confirm_reset_month(message):
    current_lm, _ = get_current_and_previous_logical_month()
    delete_month_shifts(message.chat.id, current_lm)
    bot.send_message(message.chat.id, "Смены за месяц удалены.", reply_markup=get_params_keyboard())

@bot.message_handler(func=lambda m: m.text == "❌ Нет, отмена")
def cancel_reset_month(message):
    bot.send_message(message.chat.id, "Отмена.", reply_markup=get_params_keyboard())

# --- Обработчики ОСНОВНОГО меню ---

@bot.message_handler(func=lambda m: m.text == BTN_MONTH_TOTAL)
def handle_month(message):
    current_lm, _ = get_current_and_previous_logical_month()
    total = get_month_sum_by_logical(message.chat.id, current_lm)
    bot.send_message(message.chat.id, f"📅 Доход за месяц: ~{total:.2f} руб.")

@bot.message_handler(func=lambda m: m.text == BTN_INFO)
def handle_info(message):
    # 1. Сбор статистики
    total_users_all_time = get_total_users()
    user_today = get_today_shifts_count(message.chat.id)
    all_today = get_today_shifts_count(None)
    calcs_30, people_30 = get_stats_30_days()

    # 2. Получение времени
    server_now = datetime.now()        # Реальное время сервера (обычно UTC)
    user_now = get_local_now()         # Время с твоей поправкой (+5)

    # Форматируем в красивый вид: ДД.ММ ЧЧ:ММ
    server_str = server_now.strftime("%d.%m %H:%M")
    user_str = user_now.strftime("%d.%m %H:%M")

    bot.send_message(
        message.chat.id,
        "ℹ️ **Статистика бота:**\n\n"
        f"👥 **Всего пользователей:** {total_users_all_time}\n"
        "_(люди, которые указали дату стажа)_\n\n"
        
        "📅 **Активность за 30 дней:**\n"
        f"• Живых людей: **{people_30}**\n"
        f"• Проведено расчётов: **{calcs_30}**\n\n"
        
        "📆 **Сегодня:**\n"
        f"• Твоих расчётов: **{user_today}**\n"
        f"• Всего по боту: **{all_today}**\n\n"
        
        "🕒 **Проверка времени:**\n"
        f"🖥 Сервер: `{server_str}`\n"
        f"🏠 Твоё: `{user_str}`",
        parse_mode="Markdown"
    )



@bot.message_handler(func=lambda m: m.text == BTN_SUPPORT)
def handle_support(message):
    bot.send_message(
        message.chat.id,
        "💙 Поддержать проект\n\n• Автор: Дмитрий\n• Telegram: @rambamboooff\n• Карта/СБП: +79292346466 (Альфа, Сбер, Т-Банк)"
    )

# --- Статистика ---

@bot.message_handler(func=lambda m: m.text == BTN_STAT_CURR_FULL)
def stats_curr(m):
    lm, _ = get_current_and_previous_logical_month()
    rows = get_shifts_by_logical_month(m.chat.id, lm)
    text, avg = format_shifts_list(rows)
    bot.send_message(m.chat.id, f"📅 Текущий месяц:\n\n{text}\n\nСредний: ~{avg:.2f}")

@bot.message_handler(func=lambda m: m.text == BTN_STAT_PREV_FULL)
def stats_prev(m):
    _, plm = get_current_and_previous_logical_month()
    rows = get_shifts_by_logical_month(m.chat.id, plm)
    text, avg = format_shifts_list(rows)
    bot.send_message(m.chat.id, f"📅 Прошлый месяц:\n\n{text}\n\nСредний: ~{avg:.2f}")

@bot.message_handler(func=lambda m: m.text == BTN_STAT_CURR_1_15)
def stats_half1(m):
    lm, _ = get_current_and_previous_logical_month()
    y, mon = map(int, lm.split("-"))
    rows = get_shifts_by_logical_month(m.chat.id, lm, f"{y:04d}-{mon:02d}-01", f"{y:04d}-{mon:02d}-15")
    text, avg = format_shifts_list(rows)
    bot.send_message(m.chat.id, f"📆 1–15 число:\n\n{text}\n\nСредний: ~{avg:.2f}")

@bot.message_handler(func=lambda m: m.text == BTN_STAT_CURR_16_END)
def stats_half2(m):
    lm, _ = get_current_and_previous_logical_month()
    y, mon = map(int, lm.split("-"))
    last_day = (date(y + 1, 1, 1) - timedelta(days=1)) if mon == 12 else (date(y, mon + 1, 1) - timedelta(days=1))
    rows = get_shifts_by_logical_month(m.chat.id, lm, f"{y:04d}-{mon:02d}-16", last_day.strftime("%Y-%m-%d"))
    text, avg = format_shifts_list(rows)
    bot.send_message(m.chat.id, f"📆 16–конец:\n\n{text}\n\nСредний: ~{avg:.2f}")

# --- Новая смена (Steps) ---

@bot.message_handler(func=lambda m: m.text == BTN_NEW_SHIFT)
def start_step_by_step(message):
    step_data[message.chat.id] = {'alc': 0.0, 'dry': 0.0, 'veg': 0.0, 'fresh': 0.0, 'freeze': 0.0}
    ask_number(message, "Сколько АЛКОГОЛЯ?\nВведи число.", process_step_1, 'alc')

def process_step_1(message, field_name):
    val = parse_number_from_message(message)
    if val is None: return bot.send_message(message.chat.id, "❌ Число!")
    step_data[message.chat.id][field_name] = val
    ask_number(message, "Сколько СУХОЙ?", process_step_2, 'dry')

def process_step_2(message, field_name):
    val = parse_number_from_message(message)
    if val is None: return bot.send_message(message.chat.id, "❌ Число!")
    step_data[message.chat.id][field_name] = val
    ask_number(message, "Сколько ОВОЩЕЙ?", process_step_3, 'veg')

def process_step_3(message, field_name):
    val = parse_number_from_message(message)
    if val is None: return bot.send_message(message.chat.id, "❌ Число!")
    step_data[message.chat.id][field_name] = val
    ask_number(message, "Сколько ФРЕШ?", process_step_4, 'fresh')

def process_step_4(message, field_name):
    val = parse_number_from_message(message)
    if val is None: return bot.send_message(message.chat.id, "❌ Число!")
    step_data[message.chat.id][field_name] = val
    ask_number(message, "Сколько ЗАМОРОЗКИ?", process_step_5, 'freeze')

def process_step_5(message, field_name):
    # Получаем последнее число (заморозка)
    val = parse_number_from_message(message)
    if val is None: return bot.send_message(message.chat.id, "❌ Число!")
    step_data[message.chat.id][field_name] = val

    # Достаем все сохраненные данные
    d = step_data.get(message.chat.id)
    alc, dry, veg, fresh, freeze = d['alc'], d['dry'], d['veg'], d['fresh'], d['freeze']
    
    # Считаем итог
    total, base, lines, bonus = calculate_income(veg, fresh, dry, alc, freeze, message.chat.id)
    
    # Считаем прогноз за месяц
    lm, _ = get_current_and_previous_logical_month()
    sum_after = get_month_sum_by_logical(message.chat.id, lm) + total
    
    # Сохраняем в базу
    save_shift(message.chat.id, veg, fresh, dry, alc, freeze, total)
    sum_boxes = alc + dry + veg + fresh + freeze

    
    # Считаем сумму по каждой категории отдельно для красивого вывода
    sum_alc = alc * PRICE_ALC
    sum_dry = dry * PRICE_DRY
    sum_veg = veg * PRICE_VEG
    sum_fresh = fresh * PRICE_FRESH
    sum_freeze = freeze * PRICE_FREEZE

    # Формируем сообщение
    # Используем символ '×' (крестик), чтобы не ломать Markdown звездочками
    txt = (
        "✅ Смена посчитана!\n\n"
        f"🍷 Алко: {alc} × {PRICE_ALC} = {sum_alc:.2f}\n"
        f"📦 Сухой: {dry} × {PRICE_DRY} = {sum_dry:.2f}\n"
        f"🥦 Овощи: {veg} × {PRICE_VEG} = {sum_veg:.2f}\n"
        f"🍎 Фреш: {fresh} × {PRICE_FRESH} = {sum_fresh:.2f}\n"
        f"❄️ Заморозка: {freeze} × {PRICE_FREEZE} = {sum_freeze:.2f}\n\n"
        
        f"📦 Всего коробок: *{sum_boxes:.0f}*\n\n"
        
        "🧮 *Расчёт зарплаты:*\n" + "\n".join(lines) + "\n\n"
        
        f"💵 Итог за смену: *{total:.2f} руб.*\n"
        f"📅 Итог за месяц: *~{sum_after:.2f} руб.*"
    )
    
    bot.send_message(message.chat.id, txt, parse_mode="Markdown")
    
    # Очищаем временные данные
    step_data.pop(message.chat.id, None)

# Придумай сложное имя команды (никто не должен его знать)
SECRET_CMD = "super_admin_msg_9911" 
# Придумай пароль для рассылки
BROADCAST_PASS = "7733"

@bot.message_handler(commands=[SECRET_CMD])
def handle_broadcast(message):
    # 1. Проверка на админа по ID (как и было)
    if message.chat.id != ADMIN_ID:
        return

    # 2. Разбираем сообщение: /команда ПАРОЛЬ Текст
    parts = message.text.split(' ', 2) # Делим максимум на 3 части
    
    # Должно быть 3 части: [команда, пароль, текст]
    if len(parts) < 3:
        bot.send_message(message.chat.id, "⚠️ Формат: `/команда ПАРОЛЬ Текст`", parse_mode="Markdown")
        return
    
    password = parts[1]
    text_to_send = parts[2]
    
    # 3. Проверка пароля
    if password != BROADCAST_PASS:
        bot.send_message(message.chat.id, "⛔️ Неверный пароль!")
        return
    
    # 4. Получаем список пользователей
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users')
    users = cursor.fetchall()
    conn.close()
    
    # 5. Рассылка
    count_ok = 0
    count_err = 0
    
    bot.send_message(message.chat.id, f"📢 Рассылка началась ({len(users)} получателей)...")
    
    for row in users:
        uid = row[0]
        try:
            bot.send_message(uid, f"🔔 **Объявление:**\n\n{text_to_send}", parse_mode="Markdown")
            count_ok += 1
            time.sleep(0.1) 
        except Exception:
            count_err += 1
            
    bot.send_message(
        message.chat.id,
        f"✅ **Готово!**\nДоставлено: {count_ok}\nОшибок: {count_err}"
    )



@bot.message_handler(commands=['backup'])
def handle_manual_backup(message):
    # Проверка на админа
    if message.chat.id != ADMIN_ID:
        return

    try:
        with open('earnings.db', 'rb') as file:
            bot.send_document(
                message.chat.id,
                file,
                caption=f"📦 **Резервная копия БД**\n📅 {get_local_now().strftime('%d.%m.%Y %H:%M')}",
                parse_mode="Markdown"
            )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка:\n{e}")



# ==========================================
#              НАПОМИНАНИЯ (ФОН)
# ==========================================

def user_has_shift_today(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM shifts WHERE user_id = ? AND date = ?', (user_id, today))
    res = cursor.fetchone()[0]
    conn.close()
    return bool(res)

def get_preset_time_for_day(day_index):
    # 0,1 - день (19:50), 5,6 - ночь (07:50)
    if day_index in (0, 1): return 19, 50
    elif day_index in (5, 6): return 7, 50
    else: return None, None

def reminder_loop():
    """
    Фоновый цикл: ТОЛЬКО напоминания.
    Авто-бэкап убран для безопасности.
    """
    while True:
        try:
            now = get_local_now()
            today_date = now.date()
            current_minutes = now.hour * 60 + now.minute

            users = get_users_with_cycle()
            for user_id, start_str in users:
                if not start_str: continue
                try:
                    start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
                except: continue

                delta_days = (today_date - start_date).days
                if delta_days < 0: continue
                
                day_index = delta_days % 8
                h, m = get_preset_time_for_day(day_index)
                if h is None: continue

                target_minutes = h * 60 + m
                
                # Если сейчас нужное время (±5 минут)
                if abs(current_minutes - target_minutes) <= 5:
                    if not user_has_shift_today(user_id):
                        try:
                            bot.send_message(user_id, "🔔 Напоминание: не забудь записать смену.")
                        except: pass
        except Exception as e:
            print(f"Ошибка в цикле напоминаний: {e}")
                    
        time.sleep(300) # Спим 5 минут




if __name__ == "__main__":
    reminder_thread = threading.Thread(target=reminder_loop, daemon=True)
    reminder_thread.start()
    bot.infinity_polling()
