"""原件与登记台账的逐号核验比对。

输入两侧均已按检验号去重（同号冲突已在解析阶段整份拒绝）。
比对结果（仅一侧存在的检验号、同号字段差异）只用于核验页面展示，
写入 verification 表，绝不进入待发布/已发布的安全表 payload。
"""

from __future__ import annotations

from .csv_import import TestRecord

# 入库与展示的明细条数上限（防止异常大文件撑爆核验页/数据库）
MAX_LISTED = 200


def _pos(v: bool) -> str:
    return "阳性" if v else "阴性"


def diff_records(
    original: list[TestRecord], ledger: list[TestRecord]
) -> dict:
    """逐号比对两份去重后的记录。

    返回：
    - only_in_original / only_in_ledger: 仅一侧存在的检验号（排序后）；
    - field_diffs: 同号但字段不一致的明细
      [{"test_id", "field", "original", "ledger"}]。
    """
    o = {r.test_id: r for r in original}
    g = {r.test_id: r for r in ledger}
    only_in_original = sorted(set(o) - set(g))
    only_in_ledger = sorted(set(g) - set(o))
    field_diffs: list[dict] = []
    for tid in sorted(set(o) & set(g)):
        a, b = o[tid], g[tid]
        pairs = (
            ("病区", a.ward, b.ward),
            ("周次", a.week, b.week),
            ("是否阳性", _pos(a.positive), _pos(b.positive)),
        )
        for field, va, vb in pairs:
            if va != vb:
                field_diffs.append(
                    {"test_id": tid, "field": field,
                     "original": va, "ledger": vb}
                )
    return {
        "only_in_original": only_in_original,
        "only_in_ledger": only_in_ledger,
        "field_diffs": field_diffs,
    }


def records_match(diff: dict) -> bool:
    """两侧逐号一致：无仅单侧存在的检验号，且无同号字段差异。"""
    return (
        not diff["only_in_original"]
        and not diff["only_in_ledger"]
        and not diff["field_diffs"]
    )


def cap_list(items: list, limit: int = MAX_LISTED) -> dict:
    """明细列表截断为 {total, entries, truncated}，便于入库与展示。

    键名避开 dict 方法名（items/count 等），防止模板中取错。
    """
    return {
        "total": len(items),
        "entries": items[:limit],
        "truncated": len(items) > limit,
    }
