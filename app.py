import json
import os
import re
from datetime import datetime, timedelta, timezone
import bcrypt
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, jsonify
from urllib.parse import quote
from flask_swagger_ui import get_swaggerui_blueprint
import csv
import requests

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # 請改成安全的 key

# --- 時區設定 ---
TAIPEI_TZ = timezone(timedelta(hours=8))

@app.template_filter('to_taipei_time')
def to_taipei_time(utc_str):
    if not utc_str:
        return ""
    try:
        # 解析 UTC 時間字串
        utc_dt = datetime.strptime(utc_str, '%Y-%m-%d %H:%M:%S')
        # 標記為 UTC 時區
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        # 轉換為台北時區
        taipei_dt = utc_dt.astimezone(TAIPEI_TZ)
        return taipei_dt.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return utc_str # 如果格式錯誤，返回原字串

# 科系代碼對應表
DEPT_CODE_MAP = {
    "300": "機械",
    "310": "電機",
    "320": "化工",
    "33": "材資",  # 33X
    "340": "土木",
    "350": "分子",
    "360": "電子",
    "370": "工管",
    "380": "工設",
    "390": "建築",
    "440": "車輛",
    "450": "能源",
    "540": "英文",
    "570": "經管",
    "590": "資工",
    "650": "光電",
    "810": "機電學士班",
    "820": "電資學士班",
    "830": "工程科技學士班",
    "840": "創意設計學士班",
    "A50": "文發",
    "AC1": "互動",
    "AB0": "資財",
    "C01": "機電學院"
}

# 登入失敗次數與鎖定記錄（簡易記憶體版，若需多機部署可改DB或Redis）
LOGIN_FAIL = {}
LOCK_TIME = timedelta(minutes=10)
MAX_FAIL = 5

# 登入日誌檔案
LOGIN_LOG_FILE = os.path.join(os.path.dirname(__file__), 'login_log.json')

def parse_student_id(student_id):
    year = student_id[:3]
    rest = student_id[3:]
    dept_code = rest[:3]
    dept = None
    # 先比對長度3的科系代碼
    if dept_code in DEPT_CODE_MAP:
        dept = DEPT_CODE_MAP[dept_code]
    elif rest[:2] in DEPT_CODE_MAP:
        dept_code = rest[:2]
        dept = DEPT_CODE_MAP[dept_code]
    elif rest[:3].startswith("33"):
        dept = "材資"
        dept_code = rest[:3]
    elif rest[:3].startswith("A50"):
        dept = "文發"
        dept_code = "A50"
    elif rest[:3].startswith("AC1"):
        dept = "互動"
        dept_code = "AC1"
    elif rest[:3].startswith("AB0"):
        dept = "資財"
        dept_code = "AB0"
    else:
        dept = "未知"
    return year, dept_code, dept

# 載入使用者資料
USERS_FILE = os.path.join(os.path.dirname(__file__), 'users.json')
def load_users():
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def get_user(username):
    users = load_users()
    return users.get(username)

def load_courses(data_dir, year, sem):
    path = os.path.join(data_dir, str(year), str(sem), 'main.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def extract_classes(courses):
    """
    從課程清單中萃取所有班級名稱（去重、排序）。
    假設每個課程有 'class' 欄位，格式為 [{'name': '班級名稱'}, ...]
    """
    class_set = set()
    for course in courses:
        for cls in course.get('class', []):
            class_set.add(cls['name'])
    return sorted(class_set)

@app.route('/', methods=['GET'])
def index():
    if 'user' in session:
        user = get_user(session['user'])
        if user['role'] == 'admin':
            # 管理員首頁
            return render_template('admin_dashboard.html', user=user)
        else:
            # 學生首頁
            class_name = session.get('class_name', '未知班級')
            class_name_ch = arabic_to_chinese_number(class_name)
            years = sorted([d for d in os.listdir(os.path.join(os.path.dirname(__file__), 'data')) if d.isdigit()], reverse=True)
            sems = ['1', '2']
            year = get_current_school_year()
            sem = '1'
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            courses = load_courses(data_dir, year, sem)
            
            # 檢查選課時段
            def nowstr():
                return datetime.now().strftime('%m/%d')
            periods = load_enroll_period()
            period = None
            for p in periods:
                if p['year'] == str(year) and p['sem'] == str(sem):
                    period = p
                    break
            now = nowstr()
            can_enroll = False
            if period:
                def date_to_number(date_str):
                    try:
                        if '/' in date_str:
                            month, day = map(int, date_str.split('/'))
                        else:
                            date_int = int(date_str)
                            if date_int < 32:
                                month = datetime.now().month
                                day = date_int
                            elif date_int < 1000:
                                month = date_int // 100
                                day = date_int % 100
                            else:
                                month = date_int // 100
                                day = date_int % 100
                        if 1 <= month <= 12 and 1 <= day <= 31:
                            return month * 100 + day
                        else:
                            return 0
                    except:
                        return 0
                start_num = date_to_number(period['start'])
                end_num = date_to_number(period['end'])
                now_num = date_to_number(now)
                can_enroll = start_num <= now_num <= end_num
            
            # 取得學生已選課
            key = f"{year}-{sem}"
            record = load_enroll_record()
            enrolls = record.get(key, {})
            my_courses = enrolls.get(session['user'], [])
            MAX_CREDIT = 25
            # 取得本班所有必修課
            required_courses = [c for c in courses if c.get('courseType', '') in ['▲', '△'] and any(cls['name'] == class_name_ch for cls in c.get('class', []))]
            required_ids = [str(c['id']) for c in required_courses]
            all_course_ids = set(my_courses) | set(required_ids)
            my_credit = sum(float(c.get('credit', 0)) for c in courses if str(c.get('id')) in all_course_ids)
            
            return render_template('student_dashboard.html', user=user, class_name=class_name_ch, years=years, sems=sems, year=year, sem=sem, can_enroll=can_enroll, my_credit=my_credit, MAX_CREDIT=MAX_CREDIT, period=period)
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    msg = None
    user = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = get_user(username)
        now = datetime.now()
        # 檢查鎖定
        fail_info = LOGIN_FAIL.get(username)
        if fail_info and fail_info.get('locked_until') and now < fail_info['locked_until']:
            msg = f'帳號已鎖定，請於 {fail_info["locked_until"].strftime("%H:%M:%S")} 後再試'
            return render_template('login.html', msg=msg, user=user)
        if user and check_password(password, user['password']):
            session['user'] = username
            if username in LOGIN_FAIL:
                del LOGIN_FAIL[username]
            # 學生自動判斷班級
            if user['role'] != 'admin':
                session['class_name'] = calc_class_name(username)
            # 寫入登入日誌
            write_login_log(username, request.remote_addr)
            if user['role'] == 'admin':
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('index'))
        else:
            # 登入失敗
            if not user:
                msg = '帳號不存在'
            else:
                msg = '密碼錯誤'
                # 登入失敗次數與鎖定
                fail = LOGIN_FAIL.setdefault(username, {'count': 0, 'locked_until': None})
                fail['count'] += 1
                if fail['count'] >= MAX_FAIL:
                    fail['locked_until'] = datetime.now() + LOCK_TIME
            return render_template('login.html', msg=msg, user=user)
    # GET
    if 'user' in session:
        user = get_user(session['user'])
    return render_template('login.html', msg=msg, user=user)

@app.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('class_name', None)
    return redirect(url_for('login'))

@app.route('/admin')
def admin_dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    return render_template('admin_dashboard.html', user=user)

@app.route('/course/<course_id>')
def course_detail(course_id):
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    class_name = request.args.get('class_name')
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    course = next((c for c in courses if str(c.get('id')) == str(course_id)), None)
    if not course:
        return '找不到課程', 404
    return render_template('course_detail.html', course=course, year=year, sem=sem, class_name=class_name)

@app.route('/admin/visual')
def admin_visual():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    class_name = request.args.get('class_name')
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    class_courses = [c for c in courses if any(cls['name'] == class_name for cls in c.get('class', []))]
    return render_template('course_visual.html', user=user, year=year, sem=sem, class_name=class_name, courses=class_courses)

@app.route('/admin/export')
def admin_export():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    class_name = request.args.get('class_name')
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    class_courses = [c for c in courses if any(cls['name'] == class_name for cls in c.get('class', []))]
    # 準備CSV
    def generate():
        header = ['課程名稱', '老師', '時間', '學分', '類型']
        yield ','.join(header) + '\n'
        for course in class_courses:
            name = course['name']['zh']
            teachers = '、'.join([t['name'] for t in course.get('teacher', [])])
            time_str = time_to_readable(course['time'])
            credit = course.get('credit', '')
            ctype = course.get('courseType', '')
            row = [name, teachers, time_str, str(credit), ctype]
            yield ','.join(row) + '\n'
    filename = f"{class_name}_{year}_{sem}_課表.csv"
    return Response(generate(), mimetype='text/csv', headers={"Content-Disposition": f"attachment;filename={filename}"})

@app.route('/admin/users', methods=['GET', 'POST'])
def admin_users():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    users = load_users()
    # 只顯示學生帳號
    students = [v for k, v in users.items() if v.get('role') == 'student']
    # 後端查詢日誌
    write_query_log(session['user'], '後端', '', len(students))
    return render_template('manage_users.html', user=user, students=students)

@app.route('/admin/users/delete/<student_id>')
def admin_delete_user(student_id):
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    users = load_users()
    if student_id in users and users[student_id].get('role') == 'student':
        del users[student_id]
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        write_log(session['user'], '刪除學生', student_id)
        flash(f'已刪除學生 {student_id}')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/reset/<student_id>')
def admin_reset_user(student_id):
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    users = load_users()
    if student_id in users and users[student_id].get('role') == 'student':
        users[student_id]['password'] = hash_password('12345678')
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        write_log(session['user'], '重設密碼', student_id)
        flash(f'已重設 {student_id} 密碼為 12345678')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/add', methods=['POST'])
def admin_add_user():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    student_id = request.form['student_id']
    name = request.form['name']
    password = request.form['password']
    users = load_users()
    if student_id in users:
        flash(f'學號 {student_id} 已存在')
    else:
        class_name = calc_class_name(student_id)
        users[student_id] = {
            'password': hash_password(password),
            'role': 'student',
            'name': name,
            'student_id': student_id,
            'class': class_name
        }
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        write_log(session['user'], '新增學生', student_id)
        flash(f'已新增學生 {student_id}')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/bulk_add', methods=['POST'])
def admin_bulk_add_users():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    file = request.files.get('csvfile')
    if not file:
        flash('未選擇檔案')
        return redirect(url_for('admin_users'))
    users = load_users()
    reader = csv.DictReader((line.decode('utf-8') for line in file), fieldnames=['student_id', 'name', 'password', 'class'])
    count = 0
    for row in reader:
        student_id = row['student_id']
        if student_id in users:
            continue
        class_name = calc_class_name(student_id)
        users[student_id] = {
            'password': hash_password(row['password']),
            'role': 'student',
            'name': row['name'],
            'student_id': student_id,
            'class': class_name
        }
        count += 1
        write_log(session['user'], '批量新增學生', student_id)
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    flash(f'已批量新增 {count} 位學生')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/auto_bulk_add', methods=['POST'])
def admin_auto_bulk_add_users():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    year = request.form['year']
    dept = request.form['dept']
    start = int(request.form['start'])
    end = int(request.form['end'])
    password = request.form['password']
    users = load_users()
    count = 0
    for i in range(start, end + 1):
        sid = f"{year}{dept}{str(i).zfill(3)}"
        if sid in users:
            continue
        class_name = calc_class_name(sid)
        users[sid] = {
            'password': hash_password(password),
            'role': 'student',
            'name': sid,
            'student_id': sid,
            'class': class_name
        }
        count += 1
        write_log(session['user'], '自動批量新增學生', sid)
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    flash(f'已自動批量新增 {count} 位學生')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/batch_add', methods=['POST'])
def admin_batch_add_users():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    users = load_users()
    count_auto = 0
    count_csv = 0
    # 處理自動產生
    year = request.form.get('year')
    dept = request.form.get('dept_code')
    start = request.form.get('start_no')
    end = request.form.get('end_no')
    password = '12345678'  # 預設密碼
    if year and dept and start and end:
        try:
            start_num = int(start)
            end_num = int(end)
            for i in range(start_num, end_num + 1):
                sid = f"{year}{dept}{str(i).zfill(3)}"
                if sid in users:
                    continue
                class_name = calc_class_name(sid)
                users[sid] = {
                    'password': hash_password(password),
                    'role': 'student',
                    'name': sid,
                    'student_id': sid,
                    'class': class_name
                }
                count_auto += 1
                write_log(session['user'], '自動批量新增學生', sid)
        except Exception as e:
            flash(f'自動產生學號時發生錯誤: {e}')
    # 處理CSV匯入
    file = request.files.get('csvfile')
    if file and file.filename:
        try:
            reader = csv.DictReader((line.decode('utf-8') for line in file), fieldnames=['student_id', 'name', 'password', 'class'])
            for row in reader:
                student_id = row['student_id']
                if student_id in users:
                    continue
                class_name = calc_class_name(student_id)
                users[student_id] = {
                    'password': hash_password(row['password']),
                    'role': 'student',
                    'name': row['name'],
                    'student_id': student_id,
                    'class': class_name
                }
                count_csv += 1
                write_log(session['user'], '批量匯入學生', student_id)
        except Exception as e:
            flash(f'CSV匯入時發生錯誤: {e}')
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    msg = []
    if count_auto:
        msg.append(f'自動批量新增 {count_auto} 位學生')
    if count_csv:
        msg.append(f'CSV匯入 {count_csv} 位學生')
    if not msg:
        msg = ['未新增任何學生，請檢查輸入']
    flash('、'.join(msg))
    return redirect(url_for('admin_users'))

@app.route('/admin/users/batch_delete', methods=['POST'])
def admin_batch_delete_users():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    ids = request.form.getlist('student_ids')
    users = load_users()
    count = 0
    for sid in ids:
        if sid in users and users[sid].get('role') == 'student':
            del users[sid]
            count += 1
            write_log(session['user'], '批量刪除學生', sid)
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    flash(f'已刪除 {count} 位學生')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/edit/<student_id>', methods=['POST'])
def admin_edit_user(student_id):
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    users = load_users()
    if student_id not in users or users[student_id].get('role') != 'student':
        flash('找不到該學生')
        return redirect(url_for('admin_users'))
    name = request.form['name']
    password = request.form['password']
    users[student_id]['name'] = name
    if password:
        users[student_id]['password'] = hash_password(password)
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    write_log(session['user'], '編輯學生', student_id)
    flash(f'已更新學生 {student_id} 資料')
    return redirect(url_for('admin_users'))

# 註冊 Jinja2 過濾器
def time_to_readable(time_dict):
    """
    將課程時間 dict 轉換為易讀格式。
    例如：{"fri": ["5", "6"]} 轉為 "星期五第5、6節"。
    若無上課時間則回傳 "無上課時間"。
    """
    day_map = {
        "sun": "星期日",
        "mon": "星期一",
        "tue": "星期二",
        "wed": "星期三",
        "thu": "星期四",
        "fri": "星期五",
        "sat": "星期六"
    }
    result = []
    for day, sections in time_dict.items():
        if sections:
            section_str = "、".join(sections)
            result.append(f"{day_map[day]}第{section_str}節")
    if not result:
        return "無上課時間"
    return "；".join(result)

def classify_course_type(course):
    """
    根據課程代碼或名稱分類課程類型
    """
    code = course.get('code', '').upper()
    name_zh = course.get('name', {}).get('zh', '')
    if (
        'GE' in code or
        '通識' in name_zh or
        '學院指定向度' in name_zh
    ):
        return '通識'
    elif 'PE' in code or '體育' in name_zh:
        return '體育'
    elif 'EN' in code or '英文' in name_zh or '語言' in name_zh:
        return '語言'
    else:
        return '選修'

@app.template_filter('to_readable_time')
def to_readable_time_filter(time_dict):
    return time_to_readable(time_dict)

ENROLL_PERIOD_FILE = os.path.join(os.path.dirname(__file__), 'data', 'enroll_period.json')
def load_enroll_period():
    if not os.path.exists(ENROLL_PERIOD_FILE):
        return []
    try:
        with open(ENROLL_PERIOD_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_enroll_period(data):
    with open(ENROLL_PERIOD_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 取得指定學年學期、班級的所有時段（多時段多班級）
def get_current_periods(year, sem, class_name):
    periods = load_enroll_period()
    now = datetime.now().strftime('%Y-%m-%dT%H:%M')
    result = []
    for p in periods:
        if p['year'] == str(year) and p['sem'] == str(sem) and class_name in p.get('target', []):
            result.append(p)
    # 依開始時間排序
    result.sort(key=lambda x: x['start'])
    return result

@app.route('/admin/enroll_period', methods=['GET', 'POST'])
def admin_enroll_period():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    years = sorted([d for d in os.listdir(os.path.join(os.path.dirname(__file__), 'data')) if d.isdigit()], reverse=True)
    sems = ['1', '2']
    periods = load_enroll_period()
    msg = None
    if request.method == 'POST':
        if 'batch_delete' in request.form:
            idxs = [int(i) for i in request.form['batch_delete'].split(',') if i.isdigit()]
            deleted_periods = [periods[i] for i in idxs if 0 <= i < len(periods)]
            for idx in sorted(idxs, reverse=True):
                if 0 <= idx < len(periods):
                    periods.pop(idx)
            save_enroll_period(periods)
            for p in deleted_periods:
                write_log(session['user'], '批次刪除選課時段', f"{p['year']}-{p['sem']} {p['name']}")
            msg = '已批次刪除選取時段'
        elif 'delete_idx' in request.form:
            idx = int(request.form['delete_idx'])
            if 0 <= idx < len(periods):
                p = periods[idx]
                periods.pop(idx)
                save_enroll_period(periods)
                write_log(session['user'], '刪除選課時段', f"{p['year']}-{p['sem']} {p['name']}")
                msg = '已刪除指定時段'
        elif 'edit_idx' in request.form:
            idx = int(request.form['edit_idx'])
            if 0 <= idx < len(periods):
                year = request.form['year']
                sem = request.form['sem']
                name = request.form['name']
                start = request.form['start']
                end = request.form['end']
                old = periods[idx]
                periods[idx] = {
                    'year': year,
                    'sem': sem,
                    'name': name,
                    'start': start,
                    'end': end
                }
                save_enroll_period(periods)
                write_log(session['user'], '編輯選課時段', f"{old['year']}-{old['sem']} {old['name']} -> {year}-{sem} {name}")
                msg = '已編輯時段'
        else:
            year = request.form['year']
            sem = request.form['sem']
            name = request.form['name']
            start = request.form['start']
            end = request.form['end']
            periods.append({
                'year': year,
                'sem': sem,
                'name': name,
                'start': start,
                'end': end
            })
            save_enroll_period(periods)
            write_log(session['user'], '新增選課時段', f"{year}-{sem} {name}")
            msg = f"已新增 {name} 時段（{year}-{sem}）"
    # 新增分類：進行中與歷史時段
    from datetime import datetime
    def date_to_number(date_str):
        try:
            if '/' in date_str:
                month, day = map(int, date_str.split('/'))
            else:
                date_int = int(date_str)
                if date_int < 32:
                    month = datetime.now().month
                    day = date_int
                elif date_int < 1000:
                    month = date_int // 100
                    day = date_int % 100
                else:
                    month = date_int // 100
                    day = date_int % 100
            if 1 <= month <= 12 and 1 <= day <= 31:
                return month * 100 + day
            else:
                return 0
        except:
            return 0
    today_num = date_to_number(datetime.now().strftime('%m/%d'))
    current_periods = []
    past_periods = []
    for p in periods:
        start_num = date_to_number(p['start'])
        end_num = date_to_number(p['end'])
        if start_num <= today_num <= end_num:
            current_periods.append(p)
        elif end_num < today_num:
            past_periods.append(p)
    return render_template('enroll_period.html', user=user, years=years, sems=sems, periods=periods, msg=msg)

ENROLL_RECORD_FILE = os.path.join(os.path.dirname(__file__), 'data', 'enroll_record.json')
def load_enroll_record():
    if not os.path.exists(ENROLL_RECORD_FILE):
        return {}
    try:
        with open(ENROLL_RECORD_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_enroll_record(data):
    with open(ENROLL_RECORD_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route('/admin/enroll_record', methods=['GET'])
def admin_enroll_record():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    years = sorted([d for d in os.listdir(os.path.join(os.path.dirname(__file__), 'data')) if d.isdigit()], reverse=True)
    sems = ['1', '2']
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    class_name = request.args.get('class_name', '')
    student_id = request.args.get('student_id', '')
    record = load_enroll_record()
    key = f"{year}-{sem}"
    enrolls = record.get(key, {})
    users = load_users()
    # 過濾學生
    filtered = []
    for sid, course_ids in enrolls.items():
        user_info = users.get(sid)
        if not user_info:
            continue
        if class_name and user_info.get('class') != class_name:
            continue
        if student_id and sid != student_id:
            continue
        filtered.append({
            'student_id': sid,
            'name': user_info.get('name', ''),
            'class': user_info.get('class', ''),
            'courses': course_ids
        })
    # 取得課程明細
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    course_map = {str(c['id']): c for c in courses}
    # 統計課程選課人數
    course_count = {}
    for stu in filtered:
        for cid in stu['courses']:
            course_count[cid] = course_count.get(cid, 0) + 1
    # 熱門課程前10
    hot_courses = sorted(course_count.items(), key=lambda x: x[1], reverse=True)[:10]
    hot_course_labels = [course_map[cid]['name']['zh'] if cid in course_map else cid for cid, _ in hot_courses]
    hot_course_data = [count for _, count in hot_courses]
    # 學分分布統計
    credit_stat = {}
    for stu in filtered:
        total = sum(float(course_map[cid]['credit']) if cid in course_map and 'credit' in course_map[cid] else 0 for cid in stu['courses'])
        total = int(total)
        credit_stat[total] = credit_stat.get(total, 0) + 1
    credit_labels = [f'{k}學分' for k in sorted(credit_stat.keys())]
    credit_data = [credit_stat[k] for k in sorted(credit_stat.keys())]
    # 匯出CSV
    if request.args.get('export') == '1':
        def generate():
            yield '\ufeff'  # 加上 UTF-8 BOM
            header = ['學號', '姓名', '班級', '課程ID', '課程名稱']
            yield ','.join(header) + '\n'
            for stu in filtered:
                for cid in stu['courses']:
                    cname = course_map[cid]['name']['zh'] if cid in course_map else cid
                    row = [stu['student_id'], stu['name'], stu['class'], cid, cname]
                    yield ','.join(row) + '\n'
        filename = f"enroll_record_{year}_{sem}.csv"
        return Response(generate(), mimetype='text/csv', headers={"Content-Disposition": f"attachment;filename={filename}"})
    return render_template('enroll_record.html', user=user, years=years, sems=sems, year=year, sem=sem, class_name=class_name, student_id=student_id, students=filtered, course_map=course_map, course_count=course_count, hot_course_labels=hot_course_labels, hot_course_data=hot_course_data, credit_labels=credit_labels, credit_data=credit_data)

@app.route('/student/enroll', methods=['GET', 'POST'])
def student_enroll():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] == 'admin':
        return redirect(url_for('admin_dashboard'))
    class_name = session.get('class_name', '未知班級')
    class_name_ch = arabic_to_chinese_number(class_name)
    years = sorted([d for d in os.listdir(os.path.join(os.path.dirname(__file__), 'data')) if d.isdigit()], reverse=True)
    sems = ['1', '2']
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    tab = request.args.get('tab', '必修')
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    key = f"{year}-{sem}"
    # 取得學生已選課
    record = load_enroll_record()
    enrolls = record.get(key, {})
    my_courses = enrolls.get(session['user'], [])
    # 取得本班所有必修課
    required_courses = [c for c in courses if c.get('courseType', '') in ['▲', '△'] and any(cls['name'] == class_name_ch for cls in c.get('class', []))]
    required_ids = [str(c['id']) for c in required_courses]
    all_course_ids = set(my_courses) | set(required_ids)
    my_credit = sum(float(c.get('credit', 0)) for c in courses if str(c.get('id')) in all_course_ids)
    # 衝堂檢查（已選課+必修課）
    def is_conflict(new_course):
        new_time = new_course.get('time', {})
        # 檢查已選課+必修課
        for cid in set(my_courses + required_ids):
            if str(cid) == str(new_course.get('id')):
                continue
            c = next((x for x in courses if str(x.get('id')) == str(cid)), None)
            if not c:
                continue
            for day, sections in new_time.items():
                if not sections:
                    continue
                if day in c['time'] and set(sections) & set(c['time'][day]):
                    return True
        return False
    # 分類課程，並過濾掉會衝堂的課
    tab_courses = {k: [] for k in ['通識','體育','語言','選修']}
    for c in courses:
        ctype = classify_course_type(c)
        if ctype:
            # 已選課或必修課不再顯示於可選課清單
            if str(c.get('id')) in my_courses or str(c.get('id')) in required_ids:
                continue
            # 只顯示不衝堂的課
            if not is_conflict(c):
                tab_courses[ctype].append(c)
    # 取得已選課紀錄
    def nowstr():
        return datetime.now().strftime('%m/%d')
    # 檢查選課時段
    periods = load_enroll_period()
    key = f"{year}-{sem}"
    # 找到對應學年學期的時段
    period = None
    for p in periods:
        if p['year'] == str(year) and p['sem'] == str(sem):
            period = p
            break
    now = nowstr()
    can_enroll = False
    msg = None
    if period:
        # 將月/日格式轉換為可比較的數值
        def date_to_number(date_str):
            try:
                # 處理 MM/DD 格式（如 1/25、12/5）
                if '/' in date_str:
                    month, day = map(int, date_str.split('/'))
                # 處理 MMDD 格式（如 125、1225、71）
                else:
                    date_int = int(date_str)
                    if date_int < 32:  # 如 25，假設是當月
                        month = datetime.now().month
                        day = date_int
                    elif date_int < 1000:  # 如 125、71，表示 1月25日、7月1日
                        month = date_int // 100
                        day = date_int % 100
                    else:  # 如 1225，表示 12月25日
                        month = date_int // 100
                        day = date_int % 100
                
                # 驗證月日範圍
                if 1 <= month <= 12 and 1 <= day <= 31:
                    return month * 100 + day
                else:
                    return 0
            except:
                return 0
        start_num = date_to_number(period['start'])
        end_num = date_to_number(period['end'])
        now_num = date_to_number(now)
        can_enroll = start_num <= now_num <= end_num
    else:
        msg = '尚未設定選課時段'
    # 學分上限
    MAX_CREDIT = 25
    # 選課處理
    if request.method == 'POST' and can_enroll:
        add_id = request.form.get('add_id')
        drop_id = request.form.get('drop_id')
        if add_id:
            if add_id in my_courses:
                msg = '已選過此課'
            else:
                course = next((c for c in courses if str(c.get('id')) == add_id), None)
                if not course:
                    msg = '課程不存在'
                elif is_conflict(course):
                    msg = '課程時間衝堂'
                elif my_credit + float(course.get('credit', 0)) > MAX_CREDIT:
                    msg = '超過學分上限'
                else:
                    my_courses.append(add_id)
                    enrolls[session['user']] = my_courses
                    record[key] = enrolls
                    save_enroll_record(record)
                    write_log(session['user'], '選課', add_id)
                    msg = '選課成功'
        elif drop_id:
            if drop_id in my_courses:
                my_courses.remove(drop_id)
                enrolls[session['user']] = my_courses
                record[key] = enrolls
                save_enroll_record(record)
                write_log(session['user'], '退選', drop_id)
                msg = '退選成功'
        return redirect(url_for('student_enroll', year=year, sem=sem, tab=tab, msg=msg))
    # 匯出課表
    if request.args.get('export') == '1':
        def generate():
            yield '\ufeff'  # 加上 UTF-8 BOM
            header = ['課程名稱', '老師', '時間', '學分', '類型']
            yield ','.join(header) + '\n'
            for cid in my_courses:
                course = next((c for c in courses if str(c.get('id')) == str(cid)), None)
                if not course:
                    continue
                name = course['name']['zh']
                teachers = '、'.join([t['name'] for t in course.get('teacher', [])])
                time_str = time_to_readable(course['time'])
                credit = course.get('credit', '')
                ctype = course.get('courseType', '')
                row = [name, teachers, time_str, str(credit), ctype]
                yield ','.join(row) + '\n'
        filename = f"{session['user']}_{year}_{sem}_課表.csv"
        response = Response(generate(), mimetype='text/csv; charset=utf-8')
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
        return response
    msg = request.args.get('msg', msg)
    # 熱門課程與學分分布統計（全校）
    record_all = load_enroll_record()
    users_all = load_users()
    enrolls_all = record_all.get(key, {})
    course_map = {str(c['id']): c for c in courses}
    # 熱門課程
    course_count = {}
    for course_ids in enrolls_all.values():
        for cid in course_ids:
            course_count[cid] = course_count.get(cid, 0) + 1
    hot_courses = sorted(course_count.items(), key=lambda x: x[1], reverse=True)[:50]
    hot_course_labels = [course_map[cid]['name']['zh'] if cid in course_map else cid for cid, _ in hot_courses]
    hot_course_data = [count for _, count in hot_courses]
    # 學分分布
    credit_stat = {}
    for course_ids in enrolls_all.values():
        total = sum(float(course_map[cid]['credit']) if cid in course_map and 'credit' in course_map[cid] else 0 for cid in course_ids)
        total = int(total)
        credit_stat[total] = credit_stat.get(total, 0) + 1
    credit_labels = [f'{k}學分' for k in sorted(credit_stat.keys())]
    credit_data = [credit_stat[k] for k in sorted(credit_stat.keys())]
    return render_template('student_enroll.html', user=user, class_name=class_name_ch, years=years, sems=sems, year=year, sem=sem, tab=tab, tab_courses=tab_courses, my_courses=my_courses, courses=courses, can_enroll=can_enroll, msg=msg, my_credit=my_credit, MAX_CREDIT=MAX_CREDIT, hot_course_labels=hot_course_labels, hot_course_data=hot_course_data, credit_labels=credit_labels, credit_data=credit_data)

@app.route('/student/schedule')
def student_schedule():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] == 'admin':
        return redirect(url_for('admin_dashboard'))
    class_name = session.get('class_name', '未知班級')
    class_name_ch = arabic_to_chinese_number(class_name)
    years = sorted([d for d in os.listdir(os.path.join(os.path.dirname(__file__), 'data')) if d.isdigit()], reverse=True)
    sems = ['1', '2']
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    key = f"{year}-{sem}"
    record = load_enroll_record()
    enrolls = record.get(key, {})
    my_courses = enrolls.get(session['user'], [])
    # 取得班級所有必修課
    class_courses = [c for c in courses if any(cls['name'] == class_name_ch for cls in c.get('class', []))]
    required_courses = [c for c in class_courses if c.get('courseType', '') in ['▲', '△']]
    # 已選課程
    my_course_objs = [c for c in courses if str(c.get('id')) in my_courses]
    # 合併（避免重複）
    all_courses = {}
    for c in required_courses:
        all_courses[str(c['id'])] = {'obj': c, 'type': '必修'}
    for c in my_course_objs:
        cid = str(c['id'])
        if cid in all_courses:
            all_courses[cid]['type'] = '必修+選課'
        else:
            all_courses[cid] = {'obj': c, 'type': '選課'}
    # 準備課表格線資料
    days = ['mon','tue','wed','thu','fri','sat','sun']
    sections = [str(i) for i in range(1,15)] + ['A','B','C','D']
    table = {d: {s: [] for s in sections} for d in days}
    for info in all_courses.values():
        c = info['obj']
        ctype = info['type']
        for d, secs in c.get('time', {}).items():
            for s in secs:
                table[d][s].append({'name': c['name']['zh'], 'type': ctype})
    return render_template('student_schedule.html', user=user, class_name=class_name_ch, years=years, sems=sems, year=year, sem=sem, table=table, days=days, sections=sections, my_course_objs=my_course_objs)

@app.route('/student/export')
def student_export():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    class_name = session.get('class_name', '未知班級')
    class_name_ch = arabic_to_chinese_number(class_name)
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    record = load_enroll_record()
    key = f"{year}-{sem}"
    enrolls = record.get(key, {})
    my_courses = enrolls.get(session['user'], [])
    # 取得本班所有必修課
    required_courses = [c for c in courses if c.get('courseType', '') in ['▲', '△'] and any(cls['name'] == class_name_ch for cls in c.get('class', []))]
    required_ids = [str(c['id']) for c in required_courses]
    # 匯出『已選課＋必修課』（去重）
    all_course_ids = set(my_courses) | set(required_ids)
    def generate():
        yield '\ufeff'  # 加上 UTF-8 BOM
        header = ['課程名稱', '老師', '時間', '學分', '類型']
        yield ','.join(header) + '\n'
        for cid in all_course_ids:
            course = next((c for c in courses if str(c.get('id')) == str(cid)), None)
            if not course:
                continue
            name = course['name']['zh']
            teachers = '、'.join([t['name'] for t in course.get('teacher', [])])
            time_str = time_to_readable(course['time'])
            credit = course.get('credit', '')
            ctype = course.get('courseType', '')
            row = [name, teachers, time_str, str(credit), ctype]
            yield ','.join(row) + '\n'
    filename = f"{session['user']}_{year}_{sem}_課表.csv"
    response = Response(generate(), mimetype='text/csv; charset=utf-8')
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return response

def hash_password(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
def check_password(password, hashed):
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False

LOG_FILE = os.path.join(os.path.dirname(__file__), 'operation.log')
def write_log(user, action, target):
    import json
    log = {
        "username": user,
        "action": action,
        "target": target,
        "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log, ensure_ascii=False) + '\n')

def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'success': False, 'msg': '未登入'}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    user = get_user(username)
    if user and check_password(password, user['password']):
        session['user'] = username
        if user['role'] != 'admin':
            session['class_name'] = calc_class_name(username)
        return jsonify({'success': True, 'role': user['role'], 'name': user['name']})
    else:
        return jsonify({'success': False, 'msg': '帳號或密碼錯誤'}), 401

@app.route('/api/courses', methods=['GET'])
@api_login_required
def api_courses():
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    class_name = request.args.get('class_name')
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    if class_name:
        courses = [c for c in courses if any(cls['name'] == class_name for cls in c.get('class', []))]
    return jsonify({'success': True, 'courses': courses})

@app.route('/api/enroll', methods=['POST'])
@api_login_required
def api_enroll():
    data = request.get_json()
    year = data.get('year', get_current_school_year())
    sem = data.get('sem', '1')
    add_id = data.get('add_id')
    drop_id = data.get('drop_id')
    user_id = session['user']
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    key = f"{year}-{sem}"
    record = load_enroll_record()
    enrolls = record.get(key, {})
    my_courses = enrolls.get(user_id, [])
    msg = None
    # 學分上限
    MAX_CREDIT = 25
    # 取得學生班級
    class_name = session.get('class_name', '未知班級')
    class_name_ch = arabic_to_chinese_number(class_name)
    # 取得本班所有必修課
    required_courses = [c for c in courses if c.get('courseType', '') in ['▲', '△'] and any(cls['name'] == class_name_ch for cls in c.get('class', []))]
    required_ids = [str(c['id']) for c in required_courses]
    all_course_ids = set(my_courses) | set(required_ids)
    my_credit = sum(float(c.get('credit', 0)) for c in courses if str(c.get('id')) in all_course_ids)
    # 衝堂檢查（已選課+必修課）
    def is_conflict(new_course):
        new_time = new_course.get('time', {})
        # 檢查已選課+必修課
        for cid in set(my_courses + required_ids):
            if str(cid) == str(new_course.get('id')):
                continue
            c = next((x for x in courses if str(x.get('id')) == str(cid)), None)
            if not c:
                continue
            for day, sections in new_time.items():
                if not sections:
                    continue
                if day in c['time'] and set(sections) & set(c['time'][day]):
                    return True
        return False
    if add_id:
        if add_id in my_courses:
            msg = '已選過此課'
        else:
            course = next((c for c in courses if str(c.get('id')) == add_id), None)
            if not course:
                msg = '課程不存在'
            elif is_conflict(course):
                msg = '課程時間衝堂'
            elif my_credit + float(course.get('credit', 0)) > MAX_CREDIT:
                msg = '超過學分上限'
            else:
                my_courses.append(add_id)
                enrolls[user_id] = my_courses
                record[key] = enrolls
                save_enroll_record(record)
                write_log(user_id, 'API選課', add_id)
                return jsonify({'success': True, 'msg': '選課成功'})
        return jsonify({'success': False, 'msg': msg})
    elif drop_id:
        if drop_id in my_courses:
            my_courses.remove(drop_id)
            enrolls[user_id] = my_courses
            record[key] = enrolls
            save_enroll_record(record)
            write_log(user_id, 'API退選', drop_id)
            return jsonify({'success': True, 'msg': '退選成功'})
        else:
            return jsonify({'success': False, 'msg': '未選此課'})
    return jsonify({'success': False, 'msg': '未指定操作'})

@app.route('/api/record', methods=['GET'])
@api_login_required
def api_record():
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    user_id = session['user']
    key = f"{year}-{sem}"
    record = load_enroll_record()
    my_courses = record.get(key, {}).get(user_id, [])
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    my_course_objs = [c for c in courses if str(c.get('id')) in my_courses]
    return jsonify({'success': True, 'courses': my_course_objs})

@app.route('/api/stat', methods=['GET'])
@api_login_required
def api_stat():
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    key = f"{year}-{sem}"
    record = load_enroll_record()
    users = load_users()
    enrolls = record.get(key, {})
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    courses = load_courses(data_dir, year, sem)
    course_map = {str(c['id']): c for c in courses}
    # 統計課程選課人數
    course_count = {}
    for sid, course_ids in enrolls.items():
        for cid in course_ids:
            course_count[cid] = course_count.get(cid, 0) + 1
    hot_courses = sorted(course_count.items(), key=lambda x: x[1], reverse=True)[:10]
    hot_course_labels = [course_map[cid]['name']['zh'] if cid in course_map else cid for cid, _ in hot_courses]
    hot_course_data = [count for _, count in hot_courses]
    # 學分分布
    credit_stat = {}
    for sid, course_ids in enrolls.items():
        total = sum(float(course_map[cid]['credit']) if cid in course_map and 'credit' in course_map[cid] else 0 for cid in course_ids)
        total = int(total)
        credit_stat[total] = credit_stat.get(total, 0) + 1
    credit_labels = [f'{k}學分' for k in sorted(credit_stat.keys())]
    credit_data = [credit_stat[k] for k in sorted(credit_stat.keys())]
    return jsonify({'success': True, 'hot_course_labels': hot_course_labels, 'hot_course_data': hot_course_data, 'credit_labels': credit_labels, 'credit_data': credit_data})

@app.route('/swagger.json')
def swagger_json():
    return jsonify({
        "swagger": "2.0",
        "info": {"title": "課程選課系統 API", "version": "1.0"},
        "basePath": "/",
        "schemes": ["http"],
        "paths": {
            "/api/login": {
                "post": {
                    "summary": "登入",
                    "parameters": [{"in": "body", "name": "body", "schema": {"type": "object", "properties": {"username": {"type": "string"}, "password": {"type": "string"}}}}],
                    "responses": {"200": {"description": "登入成功"}, "401": {"description": "登入失敗"}}
                }
            },
            "/api/courses": {
                "get": {
                    "summary": "查詢課程",
                    "parameters": [
                        {"name": "year", "in": "query", "type": "string"},
                        {"name": "sem", "in": "query", "type": "string"},
                        {"name": "class_name", "in": "query", "type": "string"}
                    ],
                    "responses": {"200": {"description": "課程列表"}}
                }
            },
            "/api/enroll": {
                "post": {
                    "summary": "選課/退選",
                    "parameters": [{"in": "body", "name": "body", "schema": {"type": "object", "properties": {"year": {"type": "string"}, "sem": {"type": "string"}, "add_id": {"type": "string"}, "drop_id": {"type": "string"}}}}],
                    "responses": {"200": {"description": "操作結果"}}
                }
            },
            "/api/record": {
                "get": {
                    "summary": "查詢個人已選課程",
                    "parameters": [
                        {"name": "year", "in": "query", "type": "string"},
                        {"name": "sem", "in": "query", "type": "string"}
                    ],
                    "responses": {"200": {"description": "課程列表"}}
                }
            },
            "/api/stat": {
                "get": {
                    "summary": "查詢統計資料",
                    "parameters": [
                        {"name": "year", "in": "query", "type": "string"},
                        {"name": "sem", "in": "query", "type": "string"}
                    ],
                    "responses": {"200": {"description": "統計資料"}}
                }
            }
        }
    })

SWAGGER_URL = '/api/docs'
API_URL = '/swagger.json'
swaggerui_blueprint = get_swaggerui_blueprint(SWAGGER_URL, API_URL, config={'app_name': "課程選課系統 API"})
app.register_blueprint(swaggerui_blueprint, url_prefix=SWAGGER_URL)

# 取得目前學年度（暑假8月起自動進位）
def get_current_school_year():
    today = datetime.now()
    if today.month >= 8:
        return str(today.year - 1911 + 1)
    else:
        return str(today.year - 1911)

# 根據學號自動計算班級（科系名+年級）
def calc_class_name(student_id):
    year, dept_code, dept_name = parse_student_id(student_id)
    current_year = int(get_current_school_year())
    try:
        grade = current_year - int(year) + 1
    except Exception:
        grade = 1
    grade = max(1, min(grade, 6))
    return f"{dept_name}{grade}" if dept_name != "未知" else "未知班級"

# 阿拉伯數字轉國字
CHINESE_NUM = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六"}
def arabic_to_chinese_number(class_name):
    def repl(m):
        return CHINESE_NUM.get(m.group(0), m.group(0))
    # 將班級名稱結尾的數字轉國字
    return re.sub(r'(\d+)$', lambda m: ''.join(CHINESE_NUM.get(d, d) for d in m.group(0)), class_name)

@app.route('/admin/schedule_visual', methods=['GET'])
def admin_schedule_visual():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    years = sorted([d for d in os.listdir(os.path.join(os.path.dirname(__file__), 'data')) if d.isdigit()], reverse=True)
    sems = ['1', '2']
    year = request.args.get('year', get_current_school_year())
    sem = request.args.get('sem', '1')
    student_id = request.args.get('student_id', '')
    table = None
    days = ['一','二','三','四','五','六','日']
    sections = [str(i) for i in range(1,15)] + ['A','B','C','D']
    if student_id:
        data_dir = os.path.join(os.path.dirname(__file__), 'data')
        courses = load_courses(data_dir, year, sem)
        record = load_enroll_record()
        key = f"{year}-{sem}"
        enrolls = record.get(key, {})
        my_courses = enrolls.get(student_id, [])
        # 取得該生班級
        users = load_users()
        stu = users.get(student_id)
        class_name = stu['class'] if stu and 'class' in stu else None
        class_name_ch = arabic_to_chinese_number(class_name) if class_name else None
        class_courses = [c for c in courses if class_name_ch and any(cls['name'] == class_name_ch for cls in c.get('class', []))]
        required_courses = [c for c in class_courses if c.get('courseType', '') in ['▲', '△']]
        my_course_objs = [c for c in courses if str(c.get('id')) in my_courses]
        # 合併（避免重複）
        all_courses = {}
        for c in required_courses:
            all_courses[str(c['id'])] = {'obj': c, 'type': '必修'}
        for c in my_course_objs:
            cid = str(c['id'])
            if cid in all_courses:
                all_courses[cid]['type'] = '必修+選課'
            else:
                all_courses[cid] = {'obj': c, 'type': '選課'}
        # 準備課表格線資料
        table = {d: {s: [] for s in sections} for d in days}
        day_map = {'mon':'一','tue':'二','wed':'三','thu':'四','fri':'五','sat':'六','sun':'日'}
        for info in all_courses.values():
            c = info['obj']
            ctype = info['type']
            for d, secs in c.get('time', {}).items():
                d_zh = day_map.get(d, d)
                for s in secs:
                    table[d_zh][s].append({'name': c['name']['zh'], 'type': ctype})
    return render_template('schedule_visual.html', user=user, years=years, year=year, sem=sem, days=days, sections=sections, table=table, student_id=student_id)

@app.route('/admin/login_log')
def admin_login_log():
    # 檢查檔案是否存在，若無則自動建立
    if not os.path.exists('login_log.json'):
        with open('login_log.json', 'w', encoding='utf-8') as f:
            f.write('[]')
    with open('login_log.json', encoding='utf-8') as f:
        log = json.load(f)
    # 載入操作日誌，遇到格式錯誤自動跳過
    if not os.path.exists('operation.log'):
        with open('operation.log', 'w', encoding='utf-8') as f:
            pass
    op_log = []
    try:
        with open('operation.log', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    op_log.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        op_log = []
    if request.args.get('ajax') == '1':
        return jsonify(log)
    return render_template('login_log.html', log=log, op_log=op_log)

@app.route('/admin/operation_log')
def admin_operation_log():
    if not os.path.exists('operation.log'):
        with open('operation.log', 'w', encoding='utf-8') as f:
            pass
    op_log = []
    try:
        with open('operation.log', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    op_log.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        op_log = []
    if request.args.get('ajax') == '1':
        return jsonify(op_log)
    return render_template('operation_log.html', op_log=op_log)

# 登入日誌寫入
def write_login_log(username, ip):
    log = []
    try:
        with open(LOGIN_LOG_FILE, 'r', encoding='utf-8') as f:
            log = json.load(f)
    except Exception:
        pass
    # 查詢 IP 地理位置
    location = "查無地點"
    if ip != '127.0.0.1' and ip != '::1':
        try:
            res = requests.get(f'http://ip-api.com/json/{ip}?lang=zh-TW', timeout=2)
            data = res.json()
            if data.get('status') == 'success':
                location = f"{data.get('country','')}{data.get('regionName','')}{data.get('city','')}"
        except Exception:
            location = "查詢失敗"
    log.append({
        'username': username,
        'ip': ip,
        'location': location,
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })
    with open(LOGIN_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

# 學生管理API
@app.route('/api/users', methods=['GET'])
def api_users():
    if 'user' not in session:
        return jsonify({'success': False, 'msg': '未登入'}), 401
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return jsonify({'success': False, 'msg': '無權限'}), 403
    users = load_users()
    students = [dict(student_id=k, **v) for k, v in users.items() if v.get('role') == 'student']
    return jsonify({'success': True, 'users': students})

@app.route('/api/user/<student_id>', methods=['GET', 'PUT', 'DELETE'])
def api_user_detail(student_id):
    if 'user' not in session:
        return jsonify({'success': False, 'msg': '未登入'}), 401
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return jsonify({'success': False, 'msg': '無權限'}), 403
    users = load_users()
    if request.method == 'GET':
        stu = users.get(student_id)
        if not stu or stu.get('role') != 'student':
            return jsonify({'success': False, 'msg': '查無學生'}), 404
        return jsonify({'success': True, 'user': dict(student_id=student_id, **stu)})
    elif request.method == 'PUT':
        data = request.get_json()
        if student_id not in users or users[student_id].get('role') != 'student':
            return jsonify({'success': False, 'msg': '查無學生'}), 404
        for k in ['name', 'password', 'class']:
            if k in data:
                users[student_id][k] = data[k]
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        return jsonify({'success': True, 'msg': '修改成功'})
    elif request.method == 'DELETE':
        if student_id not in users or users[student_id].get('role') != 'student':
            return jsonify({'success': False, 'msg': '查無學生'}), 404
        del users[student_id]
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        return jsonify({'success': True, 'msg': '刪除成功'})

@app.route('/api/user', methods=['POST'])
def api_user_create():
    if 'user' not in session:
        return jsonify({'success': False, 'msg': '未登入'}), 401
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return jsonify({'success': False, 'msg': '無權限'}), 403
    data = request.get_json()
    student_id = data.get('student_id')
    name = data.get('name')
    password = data.get('password')
    class_name = data.get('class')
    if not all([student_id, name, password, class_name]):
        return jsonify({'success': False, 'msg': '資料不完整'}), 400
    users = load_users()
    if student_id in users:
        return jsonify({'success': False, 'msg': '學號已存在'}), 400
    users[student_id] = {
        'name': name,
        'password': hash_password(password),
        'role': 'student',
        'class': class_name
    }
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    return jsonify({'success': True, 'msg': '新增成功'})

@app.route('/admin/course_search', methods=['GET'])
def admin_course_search():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = get_user(session['user'])
    if user['role'] != 'admin':
        return redirect(url_for('student_schedule'))
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    years = sorted([d for d in os.listdir(data_dir) if d.isdigit()], reverse=True)
    sems = ['1', '2']
    year = request.args.get('year', years[0] if years else '')
    sem = request.args.get('sem', sems[0])
    class_name = request.args.get('class_name', '')
    keyword = request.args.get('keyword', '').strip()
    courses = load_courses(data_dir, year, sem) if year and sem else []
    # 取得所有班級
    classes = extract_classes(courses) if courses else []
    # 過濾
    filtered = []
    for c in courses:
        if class_name and not any(cls['name'] == class_name for cls in c.get('class', [])):
            continue
        if keyword and keyword not in c['name']['zh'] and keyword not in c.get('code', ''):
            continue
        filtered.append(c)
    return render_template('course_search.html', user=user, years=years, sems=sems, year=year, sem=sem, class_name=class_name, classes=classes, keyword=keyword, courses=filtered)

def write_query_log(user, source, keyword, result_count):
    log_dir = '課程網站'
    log_path = os.path.join(log_dir, 'query_log.txt')
    os.makedirs(log_dir, exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now()} | {user} | {source} | {keyword} | {result_count}\n")

@app.route('/admin/query_log')
def admin_query_log():
    if 'user' not in session or get_user(session['user'])['role'] != 'admin':
        return redirect(url_for('login'))
    logs = []
    try:
        with open('課程網站/query_log.txt', encoding='utf-8') as f:
            logs = [line.strip().split(' | ') for line in f.readlines()]
    except FileNotFoundError:
        pass
    return render_template('query_log.html', logs=logs)

@app.route('/api/query_log', methods=['POST'])
def api_query_log():
    if 'user' not in session:
        return jsonify({'status': 'error', 'msg': '未登入'}), 401
    data = request.get_json()
    source = data.get('source', '前端')
    keyword = data.get('keyword', '')
    result_count = data.get('result_count', 0)
    write_query_log(session['user'], source, keyword, result_count)
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(debug=True) 