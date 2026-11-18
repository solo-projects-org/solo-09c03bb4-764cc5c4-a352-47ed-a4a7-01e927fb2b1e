"""逐号核验（原件 × 登记台账）与发布门禁的测试。

覆盖：
- 通过条件：原件指纹匹配当前预览 + 两侧逐号一致；
- 差异展示：仅一侧存在的检验号、同号字段差异（只在核验页）；
- 同版再次核验失败撤销旧通过结论；
- 换稿/核验交错时迟到的旧结论不能放行新稿、不能覆盖较新核验；
- 失败/重启后仍只能发布当前核对通过的预览。
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.verify import diff_records, records_match
from app.csv_import import parse_tests

from tests.test_app_flow import COUNTS_OK, make_csv, upload, verify, verify_ok


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "app.db")


@pytest.fixture()
def client(db_path):
    return TestClient(create_app(db_path))


@pytest.fixture()
def uploaded(client):
    """已上传并生成预览；返回原 CSV 字节。"""
    data = make_csv(COUNTS_OK)
    assert upload(client, data).status_code == 303
    return data


# ------------------------------------------------------------ 通过条件


def test_verify_pass_enables_confirm(client, uploaded):
    verify_ok(client, uploaded)
    r = client.get("/preview")
    assert "已通过" in r.text
    assert client.post("/confirm", follow_redirects=False).status_code == 303


def test_fingerprint_mismatch_fails(client, uploaded):
    # 内容等价（同号同内容重复行会被去重）但字节不同 -> 指纹不匹配
    tampered = uploaded + uploaded.splitlines(keepends=True)[1]
    r = verify(client, tampered, uploaded)
    assert r.status_code == 200
    assert "核验未通过" in r.text
    assert "不匹配" in r.text
    r = client.post("/confirm", follow_redirects=False)
    assert r.status_code == 409 and "未通过核验" in r.text


def test_ledger_conflict_rejected(client, uploaded):
    # 台账内部同号冲突 -> 台账整份拒绝 -> 核验失败
    ledger = uploaded + "T0001,外科,2026-W45,是\n".encode()
    r = verify(client, uploaded, ledger)
    assert "核验未通过" in r.text
    assert "台账 CSV 被拒绝" in r.text and "冲突" in r.text
    assert client.post("/confirm", follow_redirects=False).status_code == 409


def test_original_conflict_rejected(client, uploaded):
    original = uploaded + "T0002,外科,2026-W45,是\n".encode()
    r = verify(client, original, uploaded)
    assert "核验未通过" in r.text
    assert "原件 CSV 被拒绝" in r.text and "冲突" in r.text


def test_ledger_missing_column_rejected(client, uploaded):
    r = verify(client, uploaded, "检验号,病区\nA,内\n".encode("utf-8"))
    assert "核验未通过" in r.text
    assert "缺少必需列" in r.text


# ------------------------------------------------------------ 差异明细


def test_only_on_one_side_listed(client, uploaded):
    ledger = uploaded + "L9001,内科,2026-W44,是\nL9002,外科,2026-W45,否\n".encode()
    r = verify(client, uploaded, ledger)
    assert "核验未通过" in r.text
    assert "仅台账存在的检验号（2 个）" in r.text
    assert "L9001" in r.text and "L9002" in r.text
    assert "仅原件存在的检验号（0 个）" in r.text

    # 反向：原件多出一号（台账少一行；该号在文件中出现两次，须全部去掉）
    dropped = uploaded.replace("T0001,内科,2026-W44,是\n".encode(), b"")
    # 台账须与原件指纹无关：用上传原件 + 去掉一行的台账
    r = verify(client, uploaded, dropped)
    assert "仅原件存在的检验号（1 个）" in r.text
    assert ">T0001</code>" in r.text


def test_field_diff_listed(client, uploaded):
    # 台账中 T0001 的周次被改（两处出现都改，否则台账内部同号冲突）-> 同号字段差异
    ledger = uploaded.replace(
        "T0001,内科,2026-W44,是".encode(), "T0001,内科,2026-W45,是".encode()
    )
    r = verify(client, uploaded, ledger)
    assert "核验未通过" in r.text
    assert "同号字段差异（1 处）" in r.text
    assert "T0001" in r.text and "周次" in r.text
    assert "2026-W44" in r.text and "2026-W45" in r.text
    assert client.post("/confirm", follow_redirects=False).status_code == 409


def test_diff_details_stay_on_verify_page(client, uploaded, db_path):
    # 失败明细留在核验页（GET /verify 可回看），不进入发布表
    ledger = uploaded + "L9001,内科,2026-W44,是\n".encode()
    verify(client, uploaded, ledger)
    r = client.get("/verify")
    assert r.status_code == 200 and "L9001" in r.text

    # 重新核验通过并发布
    verify_ok(client, uploaded)
    assert client.post("/confirm", follow_redirects=False).status_code == 303
    r = client.get("/published")
    assert r.status_code == 200
    assert "L9001" not in r.text          # 差异明细不进入发布表
    assert "差异" not in r.text
    # 发布 payload 不含任何核验明细
    db = Database(db_path)
    pub = db.get_publication()
    assert not any("diff" in k or "verif" in k or "only_in" in k for k in pub)
    db.close()
    # 发布后核验页不再可用
    assert client.get("/verify", follow_redirects=False).status_code == 303


def test_verify_page_redirects_without_result(client, uploaded):
    assert client.get("/verify", follow_redirects=False).status_code == 303


# ------------------------------------------------------------ 结论撤销与交错


def test_reverify_failure_revokes_pass(client, uploaded):
    verify_ok(client, uploaded)                       # 同版第一次：通过
    r = verify(client, uploaded, uploaded + "L9001,内科,2026-W44,是\n".encode())
    assert "核验未通过" in r.text                     # 同版再次核验失败
    r = client.post("/confirm", follow_redirects=False)
    assert r.status_code == 409                       # 旧通过结论已撤销
    r = client.get("/preview")
    assert "未通过" in r.text and 'action="/confirm"' not in r.text


def test_new_upload_invalidates_old_pass(client, uploaded):
    verify_ok(client, uploaded)
    data2 = make_csv([[0, 4, 5], [3, 4, 5], [6, 7, 8]])
    assert upload(client, data2).status_code == 303   # 换稿
    r = client.post("/confirm", follow_redirects=False)
    assert r.status_code == 409                       # 旧通过不能放行新稿
    verify_ok(client, data2)                          # 新稿重新核验
    assert client.post("/confirm", follow_redirects=False).status_code == 303


def test_late_old_conclusion_cannot_release_new_draft(client, uploaded, db_path):
    """核验交错：换稿后，迟到的旧结论不能落到新稿、不能放行新稿。"""
    db = TestClient(create_app(db_path)).app.state.db  # 同一库，模拟另一会话
    v1 = db.get_pending()["version"]
    seq_old = db.begin_verification(v1)               # 旧稿核验进行中
    assert seq_old is not None

    data2 = make_csv([[0, 4, 5], [3, 4, 5], [6, 7, 8]])
    assert upload(client, data2).status_code == 303   # 换稿

    # 旧结论迟到：落库被拒绝
    assert db.finish_verification(v1, seq_old, "passed", {"passed": True}) is False
    assert db.get_verification() is None
    r = client.post("/confirm", follow_redirects=False)
    assert r.status_code == 409                       # 不能放行新稿


def test_late_old_conclusion_cannot_overwrite_newer(client, uploaded, db_path):
    """核验交错：同版两次核验，迟到的旧结论不能覆盖较新结论。"""
    db = TestClient(create_app(db_path)).app.state.db
    v = db.get_pending()["version"]
    seq_old = db.begin_verification(v)                # 旧核验进行中
    verify_ok(client, uploaded)                       # 较新核验已完成：通过
    # 旧核验迟到且结论为失败：落库被拒绝，不得覆盖较新“通过”
    assert db.finish_verification(v, seq_old, "failed", {"passed": False}) is False
    assert db.get_verification()["status"] == "passed"
    assert client.post("/confirm", follow_redirects=False).status_code == 303


def test_begin_revokes_previous_pass_immediately(client, uploaded, db_path):
    """同版再次发起核验的瞬间，旧通过结论即不可用。"""
    verify_ok(client, uploaded)
    db = TestClient(create_app(db_path)).app.state.db
    v = db.get_pending()["version"]
    assert db.begin_verification(v) is not None       # 再次发起 -> 状态 checking
    assert db.get_verification()["status"] == "checking"
    assert client.post("/confirm", follow_redirects=False).status_code == 409


def test_discard_clears_verification(client, uploaded, db_path):
    verify_ok(client, uploaded)
    assert client.post("/discard", follow_redirects=False).status_code == 303
    db = TestClient(create_app(db_path)).app.state.db
    assert db.get_pending() is None and db.get_verification() is None


def test_verify_requires_pending(client):
    r = verify(client, b"a", b"b")
    assert r.status_code == 303 and r.headers["location"] == "/upload"


def test_verify_blocked_after_publish(client, uploaded):
    verify_ok(client, uploaded)
    assert client.post("/confirm", follow_redirects=False).status_code == 303
    r = verify(client, uploaded, uploaded)
    assert r.status_code == 409


# ------------------------------------------------------------ 持久化


def test_verification_result_survives_restart(db_path):
    data = make_csv(COUNTS_OK)
    c1 = TestClient(create_app(db_path))
    upload(c1, data)
    verify(c1, data, data + "L9001,内科,2026-W44,是\n".encode())   # 失败

    c2 = TestClient(create_app(db_path))                   # 重启
    r = c2.get("/verify")                                  # 失败明细仍可回看
    assert r.status_code == 200 and "L9001" in r.text
    assert c2.post("/confirm", follow_redirects=False).status_code == 409


# ------------------------------------------------------------ 单元：比对与门禁


def test_diff_records_unit():
    a = parse_tests("检验号,病区,周次,是否阳性\n"
                    "A1,内科,W1,是\nA2,内科,W2,否\nA3,外科,W1,是\n".encode())
    b = parse_tests("检验号,病区,周次,是否阳性\n"
                    "A1,内科,W1,是\nA2,外科,W2,否\nB9,儿科,W3,是\n".encode())
    d = diff_records(a, b)
    assert d["only_in_original"] == ["A3"]
    assert d["only_in_ledger"] == ["B9"]
    assert d["field_diffs"] == [
        {"test_id": "A2", "field": "病区", "original": "内科", "ledger": "外科"}
    ]
    assert not records_match(d)
    assert records_match(diff_records(a, a))


def test_db_publish_requires_current_version_pass(tmp_path):
    db = Database(str(tmp_path / "app.db"))
    db.set_pending({"x": 1})
    ok, reason = db.publish_verified()
    assert not ok and reason == "not_verified"        # 未核验不能发布

    v = db.get_pending()["version"]
    seq = db.begin_verification(v)
    ok, reason = db.publish_verified()
    assert not ok and reason == "not_verified"        # 核验中不能发布
    assert db.finish_verification(v, seq, "failed", {}) is True
    ok, reason = db.publish_verified()
    assert not ok and reason == "not_verified"        # 核验失败不能发布

    seq = db.begin_verification(v)
    assert db.finish_verification(v, seq, "passed", {}) is True
    ok, _ = db.publish_verified()
    assert ok                                          # 通过后可发布
    assert db.get_pending() is None
    assert db.get_verification() is None               # 发布后核验结论清空
    db.close()


def test_db_old_db_migration(tmp_path):
    """老库（pending 无 version 列）打开时自动迁移，且仍需核验才能发布。"""
    import sqlite3
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE pending (id INTEGER PRIMARY KEY CHECK (id = 1),"
        " payload TEXT NOT NULL, created_at TEXT NOT NULL);"
        "INSERT INTO pending VALUES (1, '{\"wards\": []}', '2026-01-01');"
    )
    conn.close()
    db = Database(path)
    pending = db.get_pending()
    assert pending["version"]                          # 迁移后补了版本号
    ok, reason = db.publish_verified()
    assert not ok and reason == "not_verified"
    db.close()
