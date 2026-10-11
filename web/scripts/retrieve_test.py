from app.core.database import SessionLocal
from app.modules.corpus.services import retrieve_similar

db = SessionLocal()
try:
    for q in ["Python 后端开发工程师，Django FastAPI", "Java 开发工程师 微服务"]:
        rs = retrieve_similar(q, top_k=3, item_type="resume", db=db)
        print("QUERY:", q)
        for r in rs:
            print("  score=%.3f dir=%s type=%s len=%d" % (r["score"], r.get("direction"), r.get("item_type"), len(r.get("raw_text",""))))
        print("  -> total", len(rs))
finally:
    db.close()
