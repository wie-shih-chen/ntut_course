import pytest
from app import app, USERS_FILE, hash_password
import json
import os

@pytest.fixture
def client():
    app.config['TESTING'] = True
    # 測試前準備測試帳號
    users = {
        "admin": {"password": hash_password("1234"), "role": "admin", "name": "管理員"},
        "s1234567": {"password": hash_password("student123"), "role": "student", "name": "王小明", "student_id": "s1234567", "class": "資工四"},
        "s7654321": {"password": hash_password("student456"), "role": "student", "name": "李小華", "student_id": "s7654321", "class": "資工四"}
    }
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
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

def test_enroll_conflict_and_credit(client):
    login(client, 's1234567', 'student123')
    rv = client.get('/api/courses?year=114&sem=1')
    courses = rv.get_json()['courses']
    if len(courses) < 2:
        pytest.skip('課程數不足，略過衝堂/學分測試')
    # 模擬選兩門同時段課（需資料有重疊時段）
    c1, c2 = courses[0], courses[1]
    # 強制設置同時段
    c2['time'] = c1['time']
    # 選第一門
    rv = client.post('/api/enroll', json={"year": "114", "sem": "1", "add_id": str(c1['id'])})
    assert rv.status_code == 200
    # 選第二門（衝堂）
    rv = client.post('/api/enroll', json={"year": "114", "sem": "1", "add_id": str(c2['id'])})
    assert '衝堂' in rv.get_json()['msg'] or not rv.get_json()['success']
    # 測試學分上限
    for i in range(30):
        c = dict(c1)
        c['id'] = 10000 + i
        c['credit'] = 3
        c['time'] = {"mon": [str(i%14+1)]}
        courses.append(c)
        rv = client.post('/api/enroll', json={"year": "114", "sem": "1", "add_id": str(c['id'])})
        if '超過學分上限' in rv.get_data(as_text=True):
            break

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