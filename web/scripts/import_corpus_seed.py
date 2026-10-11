"""种子语料导入：把 resume_samples 静态范文灌入语料库（corpus_items + Chroma）。

让「生成简历时语义检索语料参考」真正有数据可用。
用法：python scripts/import_corpus_seed.py
"""
import os
import sys

# 把 backend 加入 sys.path，便于 import app 包
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import sqlite3  # noqa: E402


def _read_resume_samples(settings) -> list:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return []
    conn = sqlite3.connect(url[len("sqlite:///"):])
    try:
        rows = conn.execute(
            "SELECT raw_text FROM resume_samples WHERE raw_text IS NOT NULL AND length(raw_text) >= 100"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0] and r[0].strip()]


def main() -> None:
    from app.config import get_settings
    from app.core.database import init_db, SessionLocal
    from app.modules.corpus.services import add_corpus_item, count_corpus

    settings = get_settings()
    init_db()

    samples = _read_resume_samples(settings)
    print(f"resume_samples 共 {len(samples)} 条可作种子")

    db = SessionLocal()
    try:
        imported = 0
        for raw in samples:
            try:
                add_corpus_item(
                    item_type="resume",
                    raw_text=raw,
                    db=db,
                    source="import",
                    auto_tag=True,
                )
                imported += 1
            except Exception as e:  # noqa: BLE001
                print(f"  [跳过] {e}")
        print(f"已导入 {imported} 条种子语料")
        print("语料库统计:", count_corpus(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
