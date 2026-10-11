# -*- coding: utf-8 -*-
"""命令行导入权威市场薪资基准数据（覆盖模型估算）。

用法：
    # 导入数据（JSON 文件里可含 data_year/source/note，命令行参数优先）
    python scripts/import_salary_benchmark.py 2027基准.json --year 2027 --source "某机构 2027 届薪酬报告"

    # 查看当前版本历史
    python scripts/import_salary_benchmark.py --list

JSON 文件格式（纯数据，元信息可命令行传入）：
    {
        "degree_base": {"associate": [4000, 6000], ...},
        "direction_coef": {"ai": [1.70, "AI/算法/大模型"], ...},
        "city_tiers": {"tier1": [1.00, "一线城市（北上广深）"], ...}
    }
也可在 JSON 里附带元信息：data_year / source / note（命令行参数优先）。
"""

import argparse
import json
import sys
from pathlib import Path

# 把 backend 目录加入 sys.path，确保能 import app.*
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core import salary_benchmark_store as store  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models.salary_benchmark import SalaryBenchmark  # noqa: E402


def list_versions(db):
    rows = db.query(SalaryBenchmark).order_by(SalaryBenchmark.version).all()
    if not rows:
        print("暂无基准数据，首次启动会自动 seed 内置基线。")
        return
    print(f"{'版本':<6}{'年份':<8}{'生效':<6}来源")
    print("-" * 60)
    for r in rows:
        flag = "✓" if r.is_active else " "
        print(f"v{r.version:<5}{r.data_year or '-':<8}{flag:<6}{(r.source or '')[:40]}")
    print("-" * 60)
    print(f"共 {len(rows)} 个版本，当前生效 v{max(r.version for r in rows)}（若仅一条则即 v1）")


def import_data(db, path, data_year, source, note):
    p = Path(path)
    if not p.exists():
        print(f"错误：文件不存在 {p}")
        sys.exit(1)

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"错误：JSON 解析失败 {e}")
        sys.exit(1)

    if not isinstance(raw, dict):
        print("错误：JSON 顶层必须是对象（dict）")
        sys.exit(1)

    # 元信息：命令行参数优先，其次 JSON 文件里的字段
    final_year = data_year or str(raw.get("data_year") or "")
    final_source = source or str(raw.get("source") or "")
    final_note = note or str(raw.get("note") or "")

    # 纯数据部分（去掉元信息字段，避免混入）
    data = {k: v for k, v in raw.items() if k not in ("data_year", "source", "note")}

    try:
        row = store.refresh(db, data, final_year or "最新", final_source or "手动导入", final_note or "命令行导入")
    except ValueError as e:
        print(f"错误：数据不合法，已拒绝写入 —— {e}")
        sys.exit(1)

    print(f"✓ 导入成功：v{row.version}（{row.data_year}）")
    print(f"  来源：{row.source}")
    if row.note:
        print(f"  说明：{row.note}")


def main():
    parser = argparse.ArgumentParser(description="导入权威市场薪资基准数据")
    parser.add_argument("file", nargs="?", help="JSON 数据文件路径")
    parser.add_argument("--year", help="数据年份，如 2027")
    parser.add_argument("--source", help="数据来源说明")
    parser.add_argument("--note", help="更新说明")
    parser.add_argument("--list", action="store_true", help="查看版本历史")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.list or not args.file:
            list_versions(db)
            return
        import_data(db, args.file, args.year, args.source, args.note)
    finally:
        db.close()


if __name__ == "__main__":
    main()
