import telebot
from telebot import types
import sqlite3
from datetime import datetime, date, timedelta
import time

# --- НАСТРОЙКИ ---
TOKEN = '8535742126:AAEV-0tpWPOnLgJ0dcgZQ4pGQmRMhJptIIY'

PRICE_VEG = 1.88
PRICE_FRESH = 0.99
PRICE_DRY = 1.08
PRICE_ALC = 1.08
PRICE_FREEZE = 1.26  # заморозка

MORNING_START_HOUR = 5   # с 05:00
MORNING_END_HOUR = 11    # по 11:59

bot = telebot.TeleBot(TOKEN)

# --- БАЗА ДАННЫХ ---
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
            hired_month INTEGER
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- ВСПОМОГАТЕЛЬНЫЕ ДАТЫ/МЕСЯЦЫ ---

def is_morning():
    now = datetime.now()
    h = now.hour
    return MORNING_START_HOUR <= h <= MORNING_END_HOUR

def months_diff(h_year, h_month):
    today = date.today()
    return (today.year - h_year) * 12 + (today.month - h_month)

def get_prev_month(year: int, month: int):
    """Вернуть (год, месяц) предыдущего месяца."""
    if month == 1:
        return year - 1, 12
    return year, month - 1

def get_logical_month_for_now():
    """
    Определяем, к какому месяцу относится смена:
    - если сегодня 1-е и время < 12:00, относим к предыдущему месяцу;
    - иначе к текущему.
    """
    now = datetime.now()
    y, m = now.year, now.month
    if now.day == 1 and now.hour < 12:
        y, m = get_prev_month(y, m)
    return f"{y:04d}-{m:02d}"

def get_current_and_previous_logical_month():
    """
    Для кнопки 'Итог за месяц':
    - текущий логический месяц (обычно сейчас, но 1-го до 12:00 — предыдущий),
    - предыдущий логический месяц (для отчётов).
    """
    now = datetime.now()
    y, m = now.year, now.month
    if now.day == 1 and now.hour < 12:
        # сейчас фактически новый месяц, но логически работаем с прошлым
        cur_y, cur_m = get_prev_month(y, m)
    else:
        cur_y, cur_m = y, m

    prev_y, prev_m = get_prev_month(cur_y, cur_m)

    current_lm = f"{cur_y:04d}-{cur_m:02d}"
    prev_lm = f"{prev_y:04d}-{prev_m:02d}"
    return current_lm, prev_lm

# --- СТАЖ ---

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
    if m < 6:
        return 0.0
    elif 6 <= m < 12:
        return 127.25
    elif 12 <= m < 24:
        return 254.0
    elif 24 <= m < 36:
        return 305.4
    else:
        return 381.75

def save_user_hire_date(user_id, year, month):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO users(user_id, hired_year, hired_month) VALUES(?, ?, ?) '
        'ON CONFLICT(user_id) DO UPDATE SET hired_year=excluded.hired_year, hired_month=excluded.hired_month',
        (user_id, year, month)
    )  # UPSERT [web:131][web:137]
    conn.commit()
    conn.close()

def get_user_hire_date(user_id):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT hired_year, hired_month FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

# --- РАСЧЁТ ДОХОДА ---

def calculate_income(veg, fresh, dry, alc, freeze, user_id):
    base = (
        veg * PRICE_VEG +
        fresh * PRICE_FRESH +
        dry * PRICE_DRY +
        alc * PRICE_ALC +
        freeze * PRICE_FREEZE
    )

    detail_lines = []
    detail_lines.append(f"Базовая сумма S = {base:.2f} руб.")

    if base > 1840:
        x = base - 1840
        y = x * 2.5
        z = y + 5404.9
        detail_lines.append(
            f"S > 1840 → (S - 1840) * 2.5 + 5404.9 = ({base:.2f} - 1840) * 2.5 + 5404.9 = {z:.2f}"
        )
    else:
        z = base + 3104.9
        detail_lines.append(
            f"S ≤ 1840 → S + 3104.9 = {base:.2f} + 3104.9 = {z:.2f}"
        )

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
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    end_dt = now.strftime("%Y-%m-%d %H:%M:%S")
    logical_month = get_logical_month_for_now()  # сюда попадёт прошлый месяц, если 1-е до 12:00 [web:164][web:173]
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
    cursor.execute(
        'SELECT SUM(total_income) FROM shifts WHERE user_id = ? AND logical_month = ?',
        (user_id, logical_month)
    )
    result = cursor.fetchone()[0]
    conn.close()
    return result if result else 0.0

def delete_last_shift(user_id):
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id FROM shifts WHERE user_id = ? ORDER BY id DESC LIMIT 1',
        (user_id,)
    )
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
    cursor.execute(
        'DELETE FROM shifts WHERE user_id = ? AND logical_month = ?',
        (user_id, logical_month)
    )
    conn.commit()
    conn.close()

def get_shifts_by_logical_month(user_id, logical_month, date_from=None, date_to=None):
    """
    Получить список смен за логический месяц.
    Если date_from/date_to заданы (строки 'YYYY-MM-DD'), ограничиваем диапазон дат.
    Возвращаем список кортежей (date, end_datetime, total_income).
    """
    conn = sqlite3.connect('earnings.db')
    cursor = conn.cursor()

    if date_from and date_to:
        cursor.execute(
            '''
            SELECT date, end_datetime, total_income
            FROM shifts
            WHERE user_id = ?
              AND logical_month = ?
              AND date BETWEEN ? AND ?
            ORDER BY end_datetime
            ''',
            (user_id, logical_month, date_from, date_to)
        )
    else:
        cursor.execute(
            '''
            SELECT date, end_datetime, total_income
            FROM shifts
            WHERE user_id = ?
              AND logical_month = ?
            ORDER BY end_datetime
            ''',
            (user_id, logical_month)
        )

    rows = cursor.fetchall()
    conn.close()
    return rows
def format_shifts_list(rows):
    """Форматирование списка смен в текст и средний доход."""
    if not rows:
        return "Смен пока нет.", 0.0

    lines = []
    total = 0.0
    count = 0

    for date_str, end_dt, income in rows:
        # дата/время для красоты
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
    text = "\n".join(lines)
    return text, avg

@bot.message_handler(func=lambda m: m.text == "📅 Текущий месяц (все смены)")
def stats_current_full(message):
    current_lm, _ = get_current_and_previous_logical_month()
    rows = get_shifts_by_logical_month(message.chat.id, current_lm)
    text, avg = format_shifts_list(rows)
    bot.send_message(
        message.chat.id,
        "📅 Текущий логический месяц — список смен:\n\n"
        f"{text}\n\n"
        f"Средний доход за смену: ~{avg:.2f} руб.\n\n"
        "⚠️ Расчёты примерные и могут немного отличаться от действительных."
    )

@bot.message_handler(func=lambda m: m.text == "📅 Прошлый месяц (все смены)")
def stats_prev_full(message):
    current_lm, prev_lm = get_current_and_previous_logical_month()
    rows = get_shifts_by_logical_month(message.chat.id, prev_lm)
    text, avg = format_shifts_list(rows)
    bot.send_message(
        message.chat.id,
        "📅 Прошлый логический месяц — список смен:\n\n"
        f"{text}\n\n"
        f"Средний доход за смену: ~{avg:.2f} руб.\n\n"
        "⚠️ Расчёты примерные и могут немного отличаться от действительных."
    )

@bot.message_handler(func=lambda m: m.text == "📆 Текущий месяц: 1–15")
def stats_current_first_half(message):
    current_lm, _ = get_current_and_previous_logical_month()
    # Берём первое и 15-е число для фильтра [web:160][web:173]
    now = datetime.now()
    y, m = map(int, current_lm.split("-"))
    date_from = f"{y:04d}-{m:02d}-01"
    date_to = f"{y:04d}-{m:02d}-15"
    rows = get_shifts_by_logical_month(message.chat.id, current_lm, date_from, date_to)
    text, avg = format_shifts_list(rows)
    bot.send_message(
        message.chat.id,
        "📆 Текущий логический месяц, смены с 1 по 15 число:\n\n"
        f"{text}\n\n"
        f"Средний доход за смену: ~{avg:.2f} руб.\n\n"
        "⚠️ Расчёты примерные и могут немного отличаться от действительных."
    )

@bot.message_handler(func=lambda m: m.text == "📆 Текущий месяц: 16–конец")
def stats_current_second_half(message):
    current_lm, _ = get_current_and_previous_logical_month()
    y, m = map(int, current_lm.split("-"))
    # Найдём последний день месяца [web:173][web:177]
    if m == 12:
        last_day_date = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        last_day_date = date(y, m + 1, 1) - timedelta(days=1)
    date_from = f"{y:04d}-{m:02d}-16"
    date_to = last_day_date.strftime("%Y-%m-%d")

    rows = get_shifts_by_logical_month(message.chat.id, current_lm, date_from, date_to)
    text, avg = format_shifts_list(rows)
    bot.send_message(
        message.chat.id,
        "📆 Текущий логический месяц, смены с 16 числа до конца:\n\n"
        f"{text}\n\n"
        f"Средний доход за смену: ~{avg:.2f} руб.\n\n"
        "⚠️ Расчёты примерные и могут немного отличаться от действительных."
    )


# --- ВРЕМЕННЫЙ СТЕЙТ ---

step_data = {}   # {chat_id: {...}}
hire_waiting = set()

def ask_number(message, text, next_handler, field_name):
    msg = bot.send_message(message.chat.id, text)
    bot.register_next_step_handler(msg, next_handler, field_name)

def parse_number_from_message(message):
    txt = message.text.replace(',', '.').strip()
    try:
        value = float(txt)
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        return None

def parse_hire_date(text):
    t = text.strip().lower()
    months = {
        'январь': 1, 'января': 1,
        'февраль': 2, 'февраля': 2,
        'март': 3, 'марта': 3,
        'апрель': 4, 'апреля': 4,
        'май': 5, 'мая': 5,
        'июнь': 6, 'июня': 6,
        'июль': 7, 'июля': 7,
        'август': 8, 'августа': 8,
        'сентябрь': 9, 'сентября': 9,
        'октябрь': 10, 'октября': 10,
        'ноябрь': 11, 'ноября': 11,
        'декабрь': 12, 'декабря': 12
    }
    parts = t.split()
    if len(parts) != 2:
        return None, None
    month_word, year_str = parts
    month = months.get(month_word)
    try:
        year = int(year_str)
    except ValueError:
        return None, None
    return year, month

# --- ОБРАБОТЧИКИ ---

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.chat.id
    hire = get_user_hire_date(user_id)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = types.KeyboardButton("🧮 Новая смена (по этапам)")
    btn2 = types.KeyboardButton("💰 Итог за месяц")
    btn_stats = types.KeyboardButton("📊 Статистика")
    btn3 = types.KeyboardButton("🧻 Удалить последнюю смену")
    btn4 = types.KeyboardButton("🗑 Сбросить месяц")
    markup.add(btn1)
    markup.add(btn2, btn_stats)
    markup.add(btn3, btn4)


    intro = (
        "Привет! Я бот, который помогает считать примерный доход за смену на складе.\n\n"
        "Что я делаю:\n"
        "• Считаю доход по алкоголю, сухому, овощам, фрешу и заморозке.\n"
        "• Учитываю утреннюю доплату и доплату за стаж.\n"
        "• Сохраняю смены и показываю примерный доход за месяц.\n"
        "• Позволяю удалять ошибочные смены или сбрасывать месяц.\n\n"
        "⚠️ Расчёты примерные и могут немного отличаться от действительных. "
        "При расчёте не учитывается доплата за подход к ячейкам.\n"
    )

    if not hire:
        hire_waiting.add(user_id)
        bot.send_message(
            message.chat.id,
            intro +
            "\nСначала укажи дату трудоустройства, чтобы я считал доплату за стаж.\n"
            "Напиши месяц и год трудоустройства, например:\n"
            "`декабрь 2024`",
            parse_mode="Markdown",
            reply_markup=markup
        )
    else:
        bot.send_message(
            message.chat.id,
            intro + "\nВыбирай действие на клавиатуре ниже.",
            reply_markup=markup
        )

@bot.message_handler(func=lambda m: m.chat.id in hire_waiting)
def handle_hire_input(message):
    user_id = message.chat.id
    year, month = parse_hire_date(message.text)
    if not year or not month:
        bot.send_message(
            message.chat.id,
            "Не понял дату. Введи так: `декабрь 2024` (месяц русскими буквами и год цифрами).",
            parse_mode="Markdown"
        )
        return
    save_user_hire_date(user_id, year, month)
    hire_waiting.discard(user_id)
    bot.send_message(
        message.chat.id,
        f"Дата трудоустройства сохранена: {message.text.strip()}.\n"
        f"Теперь могу учитывать доплату за стаж."
    )

@bot.message_handler(func=lambda m: m.text == "💰 Итог за месяц")
def handle_month(message):
    current_lm, prev_lm = get_current_and_previous_logical_month()
    # 1-го до 12:00 мы уже считаем текущим прошлый месяц, но логика выше это учла [web:173]
    total_month = get_month_sum_by_logical(message.chat.id, current_lm)
    bot.send_message(
        message.chat.id,
        "📅 Примерный доход за выбранный месяц: "
        f"~{total_month:.2f} руб.\n\n"
        "⚠️ Важно: расчёты примерные и могут немного отличаться от действительных. "
        "При расчёте не учитывается доплата за подход к ячейкам."
    )

@bot.message_handler(func=lambda m: m.text == "🧻 Удалить последнюю смену")
def handle_delete_last(message):
    ok = delete_last_shift(message.chat.id)
    current_lm, _ = get_current_and_previous_logical_month()
    if ok:
        new_sum = get_month_sum_by_logical(message.chat.id, current_lm)
        bot.send_message(
            message.chat.id,
            f"Последняя смена удалена.\n"
            f"Новая примерная сумма за месяц: ~{new_sum:.2f} руб.\n\n"
            "⚠️ Расчёты примерные и могут немного отличаться от действительных. "
            "При расчёте не учитывается доплата за подход к ячейкам."
        )
    else:
        bot.send_message(
            message.chat.id,
            "У тебя ещё нет записанных смен, нечего удалять."
        )

@bot.message_handler(func=lambda m: m.text == "🗑 Сбросить месяц")
def handle_reset_month(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=True)
    yes_btn = types.KeyboardButton("✅ Да, удалить месяц")
    no_btn = types.KeyboardButton("❌ Нет, отмена")
    markup.add(yes_btn, no_btn)
    bot.send_message(
        message.chat.id,
        "Ты точно хочешь удалить ВСЕ смены за текущий месяц?\n"
        "Это действие нельзя отменить.",
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text == "✅ Да, удалить месяц")
def confirm_reset_month(message):
    current_lm, _ = get_current_and_previous_logical_month()
    delete_month_shifts(message.chat.id, current_lm)
    start(message)
    bot.send_message(
        message.chat.id,
        "Все смены за текущий месяц удалены.\n"
        "Сумма за месяц: 0.00 руб.\n\n"
        "⚠️ Расчёты примерные и могут немного отличаться от действительных. "
        "При расчёте не учитывается доплата за подход к ячейкам."
    )

@bot.message_handler(func=lambda m: m.text == "❌ Нет, отмена")
def cancel_reset_month(message):
    start(message)
    bot.send_message(
        message.chat.id,
        "Отмена. Данные за месяц сохранены."
    )

@bot.message_handler(func=lambda m: m.text == "🧮 Новая смена (по этапам)")
def start_step_by_step(message):
    step_data[message.chat.id] = {
        'alc': 0.0,
        'dry': 0.0,
        'veg': 0.0,
        'fresh': 0.0,
        'freeze': 0.0
    }
    ask_number(
        message,
        "Сколько АЛКОГОЛЯ?\nВведи только число (например: 120).",
        process_step_1,
        'alc'
    )

def process_step_1(message, field_name):
    value = parse_number_from_message(message)
    if value is None:
        bot.send_message(message.chat.id, "❌ Нужно ввести число. Нажми «Новая смена (по этапам)» и начнем заново.")
        return
    step_data[message.chat.id][field_name] = value
    ask_number(
        message,
        "Сколько СУХОЙ?\nВведи только число.",
        process_step_2,
        'dry'
    )

def process_step_2(message, field_name):
    value = parse_number_from_message(message)
    if value is None:
        bot.send_message(message.chat.id, "❌ Нужно число. Нажми «Новая смена (по этапам)» и начнем заново.")
        return
    step_data[message.chat.id][field_name] = value
    ask_number(
        message,
        "Сколько ОВОЩЕЙ?\nВведи только число.",
        process_step_3,
        'veg'
    )

def process_step_3(message, field_name):
    value = parse_number_from_message(message)
    if value is None:
        bot.send_message(message.chat.id, "❌ Нужно число. Нажми «Новая смена (по этапам)» и начнем заново.")
        return
    step_data[message.chat.id][field_name] = value
    ask_number(
        message,
        "Сколько ФРЕШ?\nВведи только число.",
        process_step_4,
        'fresh'
    )

def process_step_4(message, field_name):
    value = parse_number_from_message(message)
    if value is None:
        bot.send_message(message.chat.id, "❌ Нужно число. Нажми «Новая смена (по этапам)» и начнем заново.")
        return
    step_data[message.chat.id][field_name] = value
    ask_number(
        message,
        "Сколько ЗАМОРОЗКИ?\nВведи только число.",
        process_step_5,
        'freeze'
    )

def process_step_5(message, field_name):
    value = parse_number_from_message(message)
    if value is None:
        bot.send_message(message.chat.id, "❌ Нужно число. Нажми «Новая смена (по этапам)» и начнем заново.")
        return
    step_data[message.chat.id][field_name] = value

    data = step_data.get(message.chat.id, {'alc': 0, 'dry': 0, 'veg': 0, 'fresh': 0, 'freeze': 0})
    alc = data['alc']
    dry = data['dry']
    veg = data['veg']
    fresh = data['fresh']
    freeze = data['freeze']

    total, base, detail_lines, bonus = calculate_income(veg, fresh, dry, alc, freeze, message.chat.id)
    current_lm, _ = get_current_and_previous_logical_month()
    month_sum_before = get_month_sum_by_logical(message.chat.id, current_lm)
    month_sum_after = month_sum_before + total
    save_shift(message.chat.id, veg, fresh, dry, alc, freeze, total)

    response = (
        "✅ Смена посчитана!\n\n"
        f"Алкоголь: {alc} × {PRICE_ALC} = {alc * PRICE_ALC:.2f}\n"
        f"Сухой: {dry} × {PRICE_DRY} = {dry * PRICE_DRY:.2f}\n"
        f"Овощи: {veg} × {PRICE_VEG} = {veg * PRICE_VEG:.2f}\n"
        f"Фреш: {fresh} × {PRICE_FRESH} = {fresh * PRICE_FRESH:.2f}\n"
        f"Заморозка: {freeze} × {PRICE_FREEZE} = {freeze * PRICE_FREEZE:.2f}\n\n"
        "Расчёт:\n"
        + "\n".join(detail_lines) +
        "\n\n"
        f"💵 Итог за смену: *{total:.2f} руб.*\n"
        f"📅 Примерная сумма за месяц (после этой смены): *~{month_sum_after:.2f} руб.*\n\n"
        "⚠️ Расчёты примерные и могут немного отличаться от действительных. "
        "При расчёте не учитывается доплата за подход к ячейкам."
    )

    bot.send_message(message.chat.id, response, parse_mode="Markdown")
    step_data.pop(message.chat.id, None)

@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def handle_stats_menu(message):
    """Показываем подменю статистики."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_back = types.KeyboardButton("⬅️ В главное меню")
    btn1 = types.KeyboardButton("📅 Текущий месяц (все смены)")
    btn2 = types.KeyboardButton("📅 Прошлый месяц (все смены)")
    btn3 = types.KeyboardButton("📆 Текущий месяц: 1–15")
    btn4 = types.KeyboardButton("📆 Текущий месяц: 16–конец")
    markup.add(btn1)
    markup.add(btn2)
    markup.add(btn3, btn4)
    markup.add(btn_back)

    bot.send_message(
        message.chat.id,
        "Выбери вариант статистики:",
        reply_markup=markup
    )
@bot.message_handler(func=lambda m: m.text == "⬅️ В главное меню")
def handle_back_to_main(message):
    start(message)


# --- ЗАПУСК ---
while True:
    try:
        bot.infinity_polling()
    except Exception:
        time.sleep(5)
