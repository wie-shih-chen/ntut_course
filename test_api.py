import pytest
import app as app_module
from app import app, hash_password
import json
import os
import shutil

@pytest.fixture
def client(tmp_path, monkeypatch):
    app.config['TESTING'] = True
    # 所有會被寫入的檔案都導向暫存目錄，避免覆寫正式的 users.json、選課紀錄與日誌
    users_file = tmp_path / 'users.json'
    enroll_record_file = tmp_path / 'enroll_record.json'
    enroll_period_file = tmp_path / 'enroll_period.json'
    monkeypatch.setattr(app_module, 'USERS_FILE', str(users_file))
    monkeypatch.setattr(app_module, 'ENROLL_RECORD_FILE', str(enroll_record_file))
    monkeypatch.setattr(app_module, 'ENROLL_PERIOD_FILE', str(enroll_period_file))
    monkeypatch.setattr(app_module, 'LOG_FILE', str(tmp_path / 'operation.log'))
    monkeypatch.setattr(app_module, 'LOGIN_LOG_FILE', str(tmp_path / 'login_log.json'))
    # 選課時段沿用正式設定的副本（唯讀來源，不會被修改）
    real_period_file = os.path.join(os.path.dirname(app_module.__file__), 'data', 'enroll_period.json')
    if os.path.exists(real_period_file):
        shutil.copy(real_period_file, enroll_period_file)
    # 準備測試帳號
    users = {
        "admin": {"password": hash_password("1234"), "role": "admin", "name": "管理員"},
        "s1234567": {"password": hash_password("student123"), "role": "student", "name": "王小明", "student_id": "s1234567", "class": "資工四"},
        "s7654321": {"password": hash_password("student456"), "role": "student", "name": "李小華", "student_id": "s7654321", "class": "資工四"}
    }
    with open(users_file, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    with app.test_client() as client:
        yield client

def login(client, username, password):
    return client.post('/api/login', json={"username": username, "password": password})

def test_api_unauthorized(client):
    rv = client.get('/api/courses?year=114&sem=1')
    assert rv.status_code == 401

def test_login_success(client):
    rv = login(client, 'admin', '1234')
    assert rv.status_code == 200
    assert rv.get_json()['success']

def test_login_fail(client):
    rv = login(client, 'admin', 'wrongpw')
    assert rv.status_code == 401
    assert not rv.get_json()['success']

def test_courses_query(client):
    login(client, 'admin', '1234')
    rv = client.get('/api/courses?year=114&sem=1')
    assert rv.status_code == 200
    assert rv.get_json()['success']
    assert isinstance(rv.get_json()['courses'], list)

def test_enroll_and_record(client):
    login(client, 's1234567', 'student123')
    rv = client.get('/api/courses?year=114&sem=1')
    courses = rv.get_json()['courses']
    if not courses:
        pytest.skip('無課程資料，略過選課測試')
    cid = str(courses[0]['id'])
    rv = client.post('/api/enroll', json={"year": "114", "sem": "1", "add_id": cid})
    assert rv.status_code == 200
    rv = client.get('/api/record?year=114&sem=1')
    assert rv.status_code == 200
    my_courses = rv.get_json()['courses']
    assert any(str(c['id']) == cid for c in my_courses)
    rv = client.post('/api/enroll', json={"year": "114", "sem": "1", "drop_id": cid})
    assert rv.status_code == 200

def fake_course(cid, time, credit=3):
    """建立測試用課程（非必修，不屬於任何班級，避免被算入學生必修課）"""
    return {"id": cid, "code": f"T{cid}", "name": {"zh": f"測試課程{cid}"},
            "credit": credit, "courseType": "", "class": [], "time": time}

@pytest.fixture
def fake_courses(monkeypatch):
    """以固定的假課程取代 load_courses，讓衝堂/學分測試不依賴真實課程資料"""
    courses = []
    monkeypatch.setattr(app_module, 'load_courses', lambda data_dir, year, sem: courses)
    return courses

def enroll(client, cid):
    return client.post('/api/enroll', json={"year": "114", "sem": "1", "add_id": str(cid)})

def test_enroll_conflict(client, fake_courses):
    fake_courses += [
        fake_course(1, {"mon": ["1", "2"]}),
        fake_course(2, {"mon": ["2", "3"]}),  # 與課程 1 在週一第 2 節重疊
        fake_course(3, {"tue": ["1", "2"]}),  # 不衝堂
    ]
    login(client, 's1234567', 'student123')
    rv = enroll(client, 1)
    assert rv.get_json() == {'success': True, 'msg': '選課成功'}
    rv = enroll(client, 2)
    assert rv.get_json() == {'success': False, 'msg': '課程時間衝堂'}
    rv = enroll(client, 3)
    assert rv.get_json()['success']
    # 衝堂的課不應寫入選課紀錄
    rv = client.get('/api/record?year=114&sem=1')
    assert sorted(str(c['id']) for c in rv.get_json()['courses']) == ['1', '3']

def test_enroll_credit_limit(client, fake_courses):
    # 8 門 3 學分、時段互不重疊的課 = 24 學分；第 9 門會變成 27 學分，超過上限 25
    days = ["mon", "tue", "wed", "thu", "fri"]
    fake_courses += [fake_course(i, {days[i % 5]: [str(i // 5 + 1)]}) for i in range(9)]
    login(client, 's1234567', 'student123')
    for i in range(8):
        rv = enroll(client, i)
        assert rv.get_json()['success'], rv.get_json()
    rv = enroll(client, 8)
    assert rv.get_json() == {'success': False, 'msg': '超過學分上限'}
    # 剛好達到上限仍可選：1 學分的課 → 25 學分
    fake_courses.append(fake_course(99, {"sat": ["1"]}, credit=1))
    rv = enroll(client, 99)
    assert rv.get_json()['success']

def test_drop_not_enrolled(client):
    login(client, 's1234567', 'student123')
    rv = client.post('/api/enroll', json={"year": "114", "sem": "1", "drop_id": "999999"})
    assert rv.status_code == 200
    assert not rv.get_json()['success']

def test_batch_add_and_stat(client):
    login(client, 'admin', '1234')
    # 模擬多學生選同一課
    login(client, 's1234567', 'student123')
    login(client, 's7654321', 'student456')
    rv = client.get('/api/courses?year=114&sem=1')
    courses = rv.get_json()['courses']
    if not courses:
        pytest.skip('無課程資料')
    cid = str(courses[0]['id'])
    # 兩學生都選同一課
    client.post('/api/enroll', json={"year": "114", "sem": "1", "add_id": cid})
    login(client, 's7654321', 'student456')
    client.post('/api/enroll', json={"year": "114", "sem": "1", "add_id": cid})
    # 查統計
    login(client, 'admin', '1234')
    rv = client.get('/api/stat?year=114&sem=1')
    assert rv.status_code == 200
    data = rv.get_json()
    assert cid in [str(x) for x in data['hot_course_labels'] or []] or data['hot_course_data']

def test_stat(client):
    login(client, 'admin', '1234')
    rv = client.get('/api/stat?year=114&sem=1')
    assert rv.status_code == 200
    data = rv.get_json()
    assert 'hot_course_labels' in data
    assert 'credit_labels' in data 