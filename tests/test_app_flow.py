"""Web 全流程测试：上传 -> 预览 -> 核验 -> 确认发布 -> 查看 -> 重启持久化 -> 拒绝场景。"""

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

WARDS = ["内科", "外科", "儿科"]
WEEKS = ["2026-W44", "2026-W45", "2026-W46"]
# 经典可发布矩阵：一个小格 -> 连带隐藏 2x2 块
COUNTS_OK = [[1, 4, 5], [3, 4, 5], [6, 7, 8]]


def make_csv(counts, wards=WARDS, weeks=WEEKS, extra=""):
    lines = ["检验号,病区,周次,是否阳性"]
    n = 0
    first_positive = None
    for i, row in enumerate(counts):
        for j, c in enumerate(row):
            for _ in range(c):
                n += 1
                line = f"T{n:04d},{wards[i]},{weeks[j]},是"
                if first_positive is None:
                    first_positive = line
                lines.append(line)
    lines.append(f"N0001,{wards[0]},{weeks[0]},否")     # 阴性记录
    lines.append(f"N0002,{wards[-1]},{weeks[-1]},否")
    if first_positive is not None:
        lines.append(first_positive)                     # 同号同内容重复：只计一次
    if extra:
        lines.append(extra)
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "app.db")


@pytest.fixture()
def client(db_path):
    return TestClient(create_app(db_path))


def upload(client, data: bytes, follow=False):
    return client.post(
        "/upload",
        files={"file": ("tests.csv", data, "text/csv")},
        follow_redirects=follow,
    )


def verify(client, original: bytes, ledger: bytes):
    return client.post(
        "/verify",
        files={
            "original_file": ("original.csv", original, "text/csv"),
            "ledger_file": ("ledger.csv", ledger, "text/csv"),
        },
        follow_redirects=False,
    )


def verify_ok(client, data: bytes):
    """用同一份 CSV 作为原件与台账：指纹匹配且逐号一致 -> 通过。"""
    r = verify(client, data, data)
    assert r.status_code == 200
    assert "核验通过" in r.text
    return r


# ------------------------------------------------------------ 可发布流程


def test_full_publish_flow(client):
    data = make_csv(COUNTS_OK)
    # 上传 -> 303 到预览页
    r = upload(client, data)
    assert r.status_code == 303 and r.headers["location"] == "/preview"

    # 预览页：安全表，4 个隐藏格，小格计数 1 不泄露；留存原 CSV 指纹
    r = client.get("/preview")
    assert r.status_code == 200
    html = r.text
    assert html.count(">—</td>") == 4          # 1 个小格 + 3 个连带隐藏
    assert "<td>1</td>" not in html             # 小格计数 1 不可见
    for total in ("10", "12", "21", "15", "18", "43"):
        assert f"<td class=\"total\">{total}</td>" in html or \
               f"<td class=\"total grand\">{total}</td>" in html
    assert hashlib.sha256(data).hexdigest() in html   # 指纹留存并展示
    assert "尚未核验" in html

    # 未核验时确认发布被拒绝
    r = client.post("/confirm", follow_redirects=False)
    assert r.status_code == 409 and "未通过核验" in r.text

    # 核验通过 -> 确认发布 -> 303 到已发布页
    verify_ok(client, data)
    r = client.post("/confirm", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/published"

    r = client.get("/published")
    assert r.status_code == 200
    assert r.text.count(">—</td>") == 4
    assert "已发布" in r.text
    assert hashlib.sha256(data).hexdigest() in r.text  # 发布表留存原件指纹

    # 确认后不接受替换文件
    r = upload(client, data)
    assert r.status_code == 409
    assert "不接受替换文件" in r.text
    r = client.get("/upload", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/published"


def test_restart_persistence(db_path):
    c1 = TestClient(create_app(db_path))
    data = make_csv(COUNTS_OK)
    assert upload(c1, data).status_code == 303
    verify_ok(c1, data)
    assert c1.post("/confirm", follow_redirects=False).status_code == 303

    # 模拟重启：同一数据库文件、新的应用实例
    c2 = TestClient(create_app(db_path))
    r = c2.get("/published")
    assert r.status_code == 200
    assert r.text.count(">—</td>") == 4
    assert "2026-W44" in r.text
    # 重启后已发布状态下上传仍被拒绝
    assert upload(c2, data).status_code == 409


def test_restart_keeps_verified_pending(db_path):
    """核验通过后重启：仍只能发布当前核对通过的预览。"""
    data = make_csv(COUNTS_OK)
    c1 = TestClient(create_app(db_path))
    upload(c1, data)
    verify_ok(c1, data)

    c2 = TestClient(create_app(db_path))  # 重启
    r = c2.get("/preview")
    assert r.status_code == 200 and "已通过" in r.text
    assert c2.post("/confirm", follow_redirects=False).status_code == 303
    assert c2.get("/published").status_code == 200


def test_restart_keeps_failed_pending_unpublishable(db_path):
    """核验失败后重启：仍不能发布。"""
    data = make_csv(COUNTS_OK)
    c1 = TestClient(create_app(db_path))
    upload(c1, data)
    bad_ledger = data + "L9001,内科,2026-W44,是\n".encode()   # 台账多出一号
    r = verify(c1, data, bad_ledger)
    assert "核验未通过" in r.text

    c2 = TestClient(create_app(db_path))  # 重启
    r = c2.post("/confirm", follow_redirects=False)
    assert r.status_code == 409 and "未通过核验" in r.text
    assert c2.get("/published", follow_redirects=False).status_code == 303


def test_reupload_replaces_pending_before_confirm(client):
    upload(client, make_csv(COUNTS_OK))
    # 发布前重新上传：替换待确认预览
    data2 = make_csv([[0, 4, 5], [3, 4, 5], [6, 7, 8]])
    r = upload(client, data2)
    assert r.status_code == 303
    r = client.get("/preview")
    assert r.status_code == 200
    assert r.text.count(">—</td>") == 0         # 无小格，全部公开
    verify_ok(client, data2)
    assert client.post("/confirm", follow_redirects=False).status_code == 303
    assert client.get("/published").status_code == 200


# ------------------------------------------------------------ 拒绝场景


def test_conflict_csv_rejected_no_partial_state(client):
    data = make_csv(COUNTS_OK, extra="T0001,外科,2026-W45,是")  # 同号冲突
    r = upload(client, data)
    assert r.status_code == 400
    assert "冲突" in r.text
    # 不生成待确认预览
    r = client.get("/preview", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/upload"
    # 发布页不可用
    r = client.get("/published", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/upload"


def test_bad_csv_rejected(client):
    r = upload(client, "检验号,病区\nA,内\n".encode("utf-8"))
    assert r.status_code == 400
    assert "缺少必需列" in r.text


def test_unpublishable_no_partial_results(client):
    # 2x2 且含小格：无法发布
    data = make_csv([[1, 5], [5, 5]], wards=["内科", "外科"],
                    weeks=["2026-W44", "2026-W45"])
    r = upload(client, data)
    assert r.status_code == 422
    assert "无法发布" in r.text
    assert "<table" not in r.text               # 不得出现部分公开结果
    assert "crosstab" not in r.text
    # 不产生待确认预览
    r = client.get("/preview", follow_redirects=False)
    assert r.status_code == 303


def test_all_small_3x3_unpublishable(client):
    data = make_csv([[1, 1, 1], [1, 1, 1], [1, 1, 1]])
    r = upload(client, data)
    assert r.status_code == 422
    assert "<table" not in r.text


def test_confirm_without_pending_redirects(client):
    r = client.post("/confirm", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/upload"


def test_index_redirects(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/upload"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
