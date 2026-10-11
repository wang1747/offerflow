from app.models.corpus import CorpusItem
from app.modules.corpus.services import delete_corpus, list_corpus


def test_high_cost_endpoints_require_auth(auth_client):
    assert auth_client.post(
        "/api/v1/parse/document",
        files={"file": ("a.txt", b"hello", "text/plain")},
    ).status_code == 401
    assert auth_client.post(
        "/api/v1/parse/graph/query",
        json={"query_text": "python"},
    ).status_code == 401
    assert auth_client.get("/api/v1/integrations/status").status_code == 401
    assert auth_client.post(
        "/api/v1/integrations/memory/add",
        json={"text": "private"},
    ).status_code == 401
    assert auth_client.post(
        "/api/v1/corpus/search",
        json={"query_text": "python"},
    ).status_code == 401


def test_corpus_visibility_and_delete_are_scoped(db_session):
    from app.models.user import User

    db_session.add(User(id="other", name="Other"))
    db_session.add_all([
        CorpusItem(
            id="public",
            item_type="resume",
            raw_text="public corpus",
            user_id="other",
            is_public=True,
        ),
        CorpusItem(
            id="mine",
            item_type="resume",
            raw_text="my private corpus",
            user_id="default",
            is_public=False,
        ),
        CorpusItem(
            id="theirs",
            item_type="resume",
            raw_text="other private corpus",
            user_id="other",
            is_public=False,
        ),
        CorpusItem(
            id="anonymous",
            item_type="interview",
            raw_text="anonymously imported public dataset",
            user_id=None,
            is_public=False,
        ),
    ])
    db_session.commit()

    visible = list_corpus(
        db_session,
        limit=None,
        viewer_user_id="default",
        include_private=True,
    )
    # 公开 + 自己私有 + 匿名导入（无主）可见；他人私有不可见
    assert {item.id for item in visible} == {"public", "mine", "anonymous"}
    assert not delete_corpus("theirs", db_session, user_id="default")
    assert delete_corpus("mine", db_session, user_id="default")


def test_anonymous_import_corpus_stays_shared(db_session):
    """匿名导入语料（user_id IS NULL）不得因 is_public=false 被挡在非管理员之外。

    回归背景：加固把可见性收紧成「is_public + 归属」后，346 条 source='import'
    的匿名公开语料（面经题库 / 简历模板）成了管理员专属，普通用户可见量
    从 356 条掉到 10 条；桌面版本地索引靠 /corpus/export 全量重建 → 随之塌掉。
    这批语料没有归属人，不存在隐私问题，必须始终视为共享。
    """
    from app.models.user import User

    db_session.add(User(id="other", name="Other"))
    db_session.add_all([
        CorpusItem(id="anon_pub", item_type="interview", raw_text="anon public",
                   user_id=None, is_public=False),
        CorpusItem(id="anon_seed", item_type="resume", raw_text="anon seed",
                   user_id=None, is_public=True),
        CorpusItem(id="theirs_priv", item_type="resume", raw_text="other private",
                   user_id="other", is_public=False),
        CorpusItem(id="mine_priv", item_type="resume", raw_text="my private",
                   user_id="default", is_public=False),
    ])
    db_session.commit()

    # 已登录普通用户：可见 匿名(两条) + 自己的私有，不含他人私有
    logged_in = list_corpus(db_session, limit=None,
                            viewer_user_id="default", include_private=True)
    assert {i.id for i in logged_in} == {"anon_pub", "anon_seed", "mine_priv"}

    # 未登录 / 只看共享：匿名语料仍应可见
    shared_only = list_corpus(db_session, limit=None, include_private=False)
    assert {i.id for i in shared_only} == {"anon_pub", "anon_seed"}

    # 管理员：全部可见
    admin_view = list_corpus(db_session, limit=None, is_admin=True)
    assert {i.id for i in admin_view} == {"anon_pub", "anon_seed", "theirs_priv", "mine_priv"}

