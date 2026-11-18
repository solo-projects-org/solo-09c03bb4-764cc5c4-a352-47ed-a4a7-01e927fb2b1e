"""FastAPI 应用：病区×周次阳性检验安全汇总的上传、预览、核验、确认发布与查看。

流程：
    上传 CSV -> 校验/去重/冲突检测 -> 计算交叉表与隐藏方案
    -> 可发布：保存待确认预览（留存原 CSV 指纹与预览版本号）-> 网页预览
    -> 预览页重新上传原件与登记台账 CSV，逐号核验：
       两侧各自去重（同号冲突整份拒绝）、原件指纹须匹配当前预览、
       两侧检验号集合与字段须一致；差异明细只留在核验页面
    -> 核验通过（绑定当前预览版本）-> 确认发布（原子写入）
    -> 不可发布/核验失败：仅提示原因，不展示、不保存任何表格内容
确认发布后不再接受替换文件；发布结果存于 SQLite，重启后可查看。
"""

from __future__ import annotations

import hashlib
import os

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .csv_import import CsvImportError, build_crosstab, parse_tests
from .db import Database
from .suppression import find_suppression
from .verify import cap_list, diff_records, records_match

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(BASE_DIR), "data", "app.db")
)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def build_safe_payload(wards, weeks, counts, hidden, n_records, filename):
    """构造“安全表”payload：隐藏格不含真实计数（服务端也不再保留）。"""
    n_r, n_c = len(wards), len(weeks)
    cells = []
    for i in range(n_r):
        row = []
        for j in range(n_c):
            if (i, j) in hidden:
                row.append({"hidden": True, "count": None})
            else:
                row.append({"hidden": False, "count": counts[i][j]})
        cells.append(row)
    row_totals = [sum(counts[i]) for i in range(n_r)]
    col_totals = [sum(counts[i][j] for i in range(n_r)) for j in range(n_c)]
    return {
        "wards": wards,
        "weeks": weeks,
        "cells": cells,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "grand_total": sum(row_totals),
        "n_records": n_records,
        "n_hidden": len(hidden),
        "source_filename": filename,
    }


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="病区×周次阳性检验安全汇总发布", docs_url=None, redoc_url=None)
    db = Database(db_path or DEFAULT_DB_PATH)
    app.state.db = db

    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
              name="static")

    def render(request: Request, name: str, status: int = 200, **ctx):
        return templates.TemplateResponse(
            request, name, ctx, status_code=status
        )

    def current_verification(pending: dict) -> tuple[dict | None, bool]:
        """当前预览版本对应的核验结论与“是否可确认发布”。"""
        verif = db.get_verification()
        if verif is None or verif["preview_version"] != pending.get("version"):
            return None, False
        return verif, verif["status"] == "passed"

    # ------------------------------------------------------------ 页面

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if db.get_publication() is not None:
            return RedirectResponse("/published", status_code=303)
        if db.get_pending() is not None:
            return RedirectResponse("/preview", status_code=303)
        return RedirectResponse("/upload", status_code=303)

    @app.get("/upload", response_class=HTMLResponse)
    def upload_form(request: Request):
        if db.get_publication() is not None:
            return RedirectResponse("/published", status_code=303)
        return render(request, "upload.html", errors=[], has_pending=db.get_pending() is not None)

    @app.post("/upload", response_class=HTMLResponse)
    async def upload(request: Request, file: UploadFile = File(...)):
        if db.get_publication() is not None:
            return render(request, "message.html", status=409,
                          title="已发布，不接受替换文件",
                          message="汇总表已确认发布。为保证发布结果不可篡改，"
                                  "系统不再接受新的上传或替换。")

        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return render(request, "upload.html", status=400,
                          errors=[f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）"],
                          has_pending=db.get_pending() is not None)

        try:
            records = parse_tests(raw)
        except CsvImportError as exc:
            # 校验失败：整份拒绝，不改变任何已有状态
            return render(request, "upload.html", status=400,
                          errors=exc.errors,
                          has_pending=db.get_pending() is not None)

        wards, weeks, counts = build_crosstab(records)
        hidden, reasons = find_suppression(counts)
        if hidden is None:
            # 无法发布：不展示、不保存任何表格内容（无部分公开结果）
            return render(request, "message.html", status=422,
                          title="无法发布",
                          message="该数据在当前保密规则下无法安全发布。",
                          reasons=reasons)

        payload = build_safe_payload(
            wards, weeks, counts, hidden, len(records), file.filename or "upload.csv"
        )
        # 留存原 CSV 指纹：核验时据此确认“原件匹配当前预览”
        payload["source_sha256"] = hashlib.sha256(raw).hexdigest()
        db.set_pending(payload)  # 换稿：生成新预览版本并作废旧核验结论
        return RedirectResponse("/preview", status_code=303)

    @app.get("/preview", response_class=HTMLResponse)
    def preview(request: Request):
        if db.get_publication() is not None:
            return RedirectResponse("/published", status_code=303)
        pending = db.get_pending()
        if pending is None:
            return RedirectResponse("/upload", status_code=303)
        verif, can_confirm = current_verification(pending)
        return render(request, "preview.html", t=pending,
                      verif=verif, can_confirm=can_confirm)

    # ------------------------------------------------------------ 核验

    @app.get("/verify", response_class=HTMLResponse)
    def verify_page(request: Request):
        """核验页面：展示当前预览版本最近一次核验的结论与差异明细。"""
        if db.get_publication() is not None:
            return RedirectResponse("/published", status_code=303)
        pending = db.get_pending()
        if pending is None:
            return RedirectResponse("/upload", status_code=303)
        verif, _ = current_verification(pending)
        if verif is None or verif["detail"] is None:
            return RedirectResponse("/preview", status_code=303)
        return render(request, "verify.html", t=pending, res=verif["detail"],
                      verif_status=verif["status"], checked_at=verif["created_at"],
                      applied=True)

    @app.post("/verify", response_class=HTMLResponse)
    async def verify(request: Request,
                     original_file: UploadFile = File(...),
                     ledger_file: UploadFile = File(...)):
        """逐号核验：重新上传的原件 + 独立登记台账。

        通过条件（缺一不可）：
        1. 原件指纹与生成当前预览时留存的原 CSV 指纹一致；
        2. 两份文件各自解析通过（按检验号去重，同号冲突整份拒绝）；
        3. 两侧检验号集合一致且同号字段无差异。
        """
        if db.get_publication() is not None:
            return render(request, "message.html", status=409,
                          title="已发布，无需核验",
                          message="汇总表已确认发布，不能再核验或替换。")
        pending = db.get_pending()
        if pending is None:
            return RedirectResponse("/upload", status_code=303)

        original_raw = await original_file.read(MAX_UPLOAD_BYTES + 1)
        ledger_raw = await ledger_file.read(MAX_UPLOAD_BYTES + 1)

        # 发起核验：绑定当前预览版本；同版旧结论立即作废
        seq = db.begin_verification(pending["version"])
        if seq is None:
            return render(request, "message.html", status=409,
                          title="预览已更换",
                          message="待确认的预览已更换，请重新上传原件与台账核验。")

        limit_msg = f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）"
        errors_original: list[str] = []
        errors_ledger: list[str] = []
        original_records = ledger_records = None
        if len(original_raw) > MAX_UPLOAD_BYTES:
            errors_original.append(limit_msg)
        else:
            try:
                original_records = parse_tests(original_raw, check_coverage=False)
            except CsvImportError as exc:
                errors_original = exc.errors
        if len(ledger_raw) > MAX_UPLOAD_BYTES:
            errors_ledger.append(limit_msg)
        else:
            try:
                ledger_records = parse_tests(ledger_raw, check_coverage=False)
            except CsvImportError as exc:
                errors_ledger = exc.errors

        original_sha256 = hashlib.sha256(original_raw).hexdigest()
        fingerprint_match = original_sha256 == pending.get("source_sha256")

        diff = {"only_in_original": [], "only_in_ledger": [], "field_diffs": []}
        if original_records is not None and ledger_records is not None:
            diff = diff_records(original_records, ledger_records)

        passed = (
            fingerprint_match
            and not errors_original
            and not errors_ledger
            and records_match(diff)
        )
        detail = {
            "passed": passed,
            "fingerprint_match": fingerprint_match,
            "source_sha256": pending.get("source_sha256"),
            "original_sha256": original_sha256,
            "original_filename": original_file.filename or "original.csv",
            "ledger_filename": ledger_file.filename or "ledger.csv",
            "n_original": len(original_records) if original_records is not None else None,
            "n_ledger": len(ledger_records) if ledger_records is not None else None,
            "errors_original": errors_original,
            "errors_ledger": errors_ledger,
            "only_in_original": cap_list(diff["only_in_original"]),
            "only_in_ledger": cap_list(diff["only_in_ledger"]),
            "field_diffs": cap_list(diff["field_diffs"]),
        }
        # 迟到结论（换稿或有更新核验）不落库，但本次结果仍展示给操作人
        applied = db.finish_verification(
            pending["version"], seq, "passed" if passed else "failed", detail
        )
        return render(request, "verify.html", t=pending, res=detail,
                      verif_status="passed" if passed else "failed",
                      checked_at=None, applied=applied)

    # ------------------------------------------------------------ 发布

    @app.post("/confirm", response_class=HTMLResponse)
    def confirm(request: Request):
        ok, reason = db.publish_verified()
        if ok:
            return RedirectResponse("/published", status_code=303)
        if reason == "no_pending":
            if db.get_publication() is not None:
                return RedirectResponse("/published", status_code=303)
            return RedirectResponse("/upload", status_code=303)
        if reason == "already_published":
            return render(request, "message.html", status=409,
                          title="已发布，不接受替换文件",
                          message="汇总表已确认发布，本次确认被拒绝。")
        return render(request, "message.html", status=409,
                      title="未通过核验，不能发布",
                      message="发布前须在预览页重新上传原件与登记台账完成逐号核验："
                              "原件指纹须匹配当前预览，且两侧检验记录逐号一致。")

    @app.post("/discard", response_class=HTMLResponse)
    def discard(request: Request):
        if db.get_publication() is None:
            db.delete_pending()
        return RedirectResponse("/upload", status_code=303)

    @app.get("/published", response_class=HTMLResponse)
    def published(request: Request):
        pub = db.get_publication()
        if pub is None:
            return RedirectResponse("/upload", status_code=303)
        return render(request, "published.html", t=pub)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
