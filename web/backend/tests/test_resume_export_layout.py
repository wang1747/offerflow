"""导出多栏版式（单栏 / 双栏）的接口与渲染契约。

这组用例守的是一条新链路：**版式参数 → 渲染分栏 → 接口产物**。
判据刻意落在「几何事实」与「反向对照」上，不落在「文本里有某个词」上：

- 双栏容器按**列宽**识别（主栏+栏间距 / 副栏），不按「单元格是不是 list」
  —— ``Table.wrap()`` 会把单元格里的 flowable 列表换成 ``_ExpandedCellTuple``，
  用类型判会被静默判成单栏（这个假 FAIL 已经实测踩过）。
- ``sidebar_sections`` 指向本文档里不存在的分区时，必须退回单栏，
  且**逐字节等于**单栏产物 —— 不能凭空造一个空副栏、也不能把主栏压窄。
- 双栏整行塞不下时（reportlab 表格行不可跨页拆）必须降档重试 / 最终退回单栏，
  绝不允许返回 0 字节空文件。
"""

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Table
from reportlab.platypus.doctemplate import LayoutError

from app.modules.resume import export_service as E

MARGIN = 20
AVAIL_W = A4[0] - 2 * MARGIN * mm
AVAIL_H = A4[1] - (E._TOP_MARGIN_MM + E._BOTTOM_MARGIN_MM) * mm
SECTIONS = ("技能", "证书")


def _payload(*, skills=("Python", "FastAPI"), certs=("英语六级",), bullets=6):
    return {
        "name": "张三",
        "position": "Python 后端实习",
        "contact": {"phone": "13800138000", "email": "zhangsan@example.com"},
        "summary": "计算机科学与技术专业，具备 Python 后端开发能力。",
        "education": [{"school": "示例大学", "major": "计算机科学与技术",
                       "degree": "本科", "start": "2023-09", "end": "2027-06"}],
        "experiences": [
            {"type": "project", "name": "OfferFlow", "role": "后端开发",
             "start": "2026-03", "end": "2026-08",
             "bullets": ["负责订单结算的后端接口开发与联调，补齐异常处理，保证服务稳定运行"
                         for _ in range(bullets)]},
            {"type": "internship", "name": "某公司", "role": "开发实习生",
             "start": "2026-06", "end": "2026-08",
             "bullets": ["负责数据同步脚本编写，梳理调用链路，支撑每日增量更新"
                         for _ in range(bullets)]},
        ],
        "skills": list(skills),
        "certifications": list(certs),
    }


class _Resume:
    """只被导出层读两个字段的最小替身（导出是只读动作，不需要真 ORM 对象）。"""

    def __init__(self, generation: dict):
        self.parsed_json = {"generation": generation}
        self.raw_text = ""


def _story(payload, sidebar, tier=(11.0, 17.0, 13.0, 10.0, 21.0, MARGIN)):
    return E._pdf_story_structured(
        "Helvetica", payload, tier[0], tier[1], tier[2], tier[3], tier[4], tier[5],
        0.0, 1.0, None, tuple(sidebar), E._DEFAULT_SIDEBAR_RATIO)


def _two_col(story, margin):
    """按几何识别双栏容器：顶层表格，两列，列宽等于算出来的主栏/副栏。"""
    content_w = (210 - 2 * margin) * mm
    main_w, side_w = E._column_widths(content_w, SECTIONS, E._DEFAULT_SIDEBAR_RATIO)
    for f in story:
        if not isinstance(f, Table):
            continue
        widths = getattr(f, "_colWidths", None)
        if not widths or len(widths) != 2:
            continue
        if (abs(float(widths[0]) - (main_w + E._COLUMN_GUTTER_PT)) < 1.0
                and abs(float(widths[1]) - side_w) < 1.0):
            return f
    return None


def _texts(flowables):
    out = []

    def walk(items):
        for item in items:
            if isinstance(item, (list, tuple)):
                walk(item)
            elif isinstance(item, Table):
                for row in item._cellvalues:
                    for cell in row:
                        walk([cell])
            elif isinstance(getattr(item, "text", None), str):
                out.append(item.text)

    walk(flowables)
    return out


# ---------------- 渲染层：分栏几何 ----------------


def test_split_moves_only_declared_sections_to_sidebar():
    story, blocks = _story(_payload(), SECTIONS)
    table = _two_col(story, MARGIN)
    assert table is not None, "声明了副栏分区却没生成双栏容器"

    side_text = " | ".join(_texts([table._cellvalues[0][1]]))
    main_text = " | ".join(_texts([table._cellvalues[0][0]]))
    assert "技能" in side_text and "证书" in side_text
    assert "项目经历" in main_text
    assert "技能" not in main_text, "技能分区必须整块进副栏，不能两栏都出现"
    assert "项目经历" not in side_text, "经历类内容必须留在主栏"
    # 两列宽 + 栏间距 = 版心宽（否则页面右侧会多出或吞掉一条空白）
    assert abs(sum(float(w) for w in table._colWidths) - AVAIL_W) < 0.5
    # 副栏不额外占模块编号（自适应按模块数分配留白，不能虚增）
    single_story, single_blocks = _story(_payload(), ())
    assert blocks == single_blocks == 6


def test_split_is_shorter_than_single():
    """并排生效的硬证据：同一份内容双栏的自然高度必须比单栏小。"""
    single, _ = _story(_payload(), ())
    split, _ = _story(_payload(), SECTIONS)
    h_single = E._measure_story(single, AVAIL_W, AVAIL_H)
    h_split = E._measure_story(split, AVAIL_W, AVAIL_H)
    assert h_split < h_single, f"双栏没变矮：split={h_split} single={h_single}"


@pytest.mark.parametrize("sidebar", [(), SECTIONS])
def test_both_layouts_render_to_one_page(sidebar):
    story, _ = _story(_payload(), sidebar)
    data, pages = E._render(story, MARGIN)
    assert pages == 1, f"版式 sidebar={sidebar} 溢出到 {pages} 页"
    assert data.startswith(b"%PDF") and len(data) > 1000


@pytest.mark.parametrize("layout", ["single", "split"])
def test_export_to_pdf_smoke(layout):
    """端到端入口对两种版式都必须给出可解析的 PDF（split 走新增的分栏链路）。"""
    data = E.export_to_pdf(_Resume(_payload()), None, None, layout=layout)
    assert data.startswith(b"%PDF") and len(data) > 1000
    assert data.rstrip().endswith(b"%%EOF")


def test_render_does_not_consume_story():
    """``_render`` 必须是只读的。

    反向对照：reportlab 的 ``doc.build()`` 会把传入列表逐个 pop 掉，
    历史上 ``_adaptive_pdf`` 有一条「超页 → 回退重渲 base_story」的分支，
    一旦原 story 被吃空，那条分支会渲出 **0 页空白 PDF** 且被当作成功返回。
    """
    story, _ = _story(_payload(), ())
    before = len(story)
    data, pages = E._render(story, MARGIN)
    assert len(story) == before, "渲染吃掉了 story —— 回退分支会渲出空白页"
    assert pages == 1
    again, pages_again = E._render(story, MARGIN)
    assert pages_again == 1, "重渲同一 story 不再是 1 页"
    assert abs(len(again) - len(data)) <= 16


# ---------------- 反向对照 ----------------


def test_sidebar_labels_not_present_falls_back_to_single():
    """声明的副栏分区在本文档里不存在 → 不许造空副栏，也不许压窄主栏。"""
    story, _ = _story(_payload(), ("语言",))
    assert _two_col(story, MARGIN) is None
    assert len(story) == len(_story(_payload(), ())[0])


def test_layout_without_sidebar_content_equals_single_end_to_end():
    """整份导出层面的同一件事：没有可进副栏的分区 → 产物与单栏等长。"""
    payload = _payload(skills=(), certs=())
    single = E.export_to_pdf(_Resume(payload), None, None, layout="single")
    split = E.export_to_pdf(_Resume(payload), None, None, layout="split")
    assert len(split) == len(single)


def test_unknown_layout_falls_back_to_single():
    assert E.resolve_layout("不存在") == E.resolve_layout(None) == ("single", ())
    payload = _payload()
    single = E.export_to_pdf(_Resume(payload), None, None, layout="single")
    bogus = E.export_to_pdf(_Resume(payload), None, None, layout="不存在的版式")
    assert len(bogus) == len(single)


# ---------------- 超长内容：降档与兜底 ----------------


def test_two_column_row_cannot_split():
    """证明「兜底」不是死代码：双栏整行超过一页时确实会抛 LayoutError。"""
    payload = _payload(bullets=60)
    story, _ = _story(payload, SECTIONS)
    with pytest.raises(LayoutError):
        E._render(story, E._SIZE_TIERS[-1][5])


def test_long_content_still_returns_pdf_not_empty():
    """所有档位都排不下时退回单栏，绝不允许返回 0 字节。"""
    data = E.export_to_pdf(_Resume(_payload(bullets=60)), None, None, layout="split")
    assert data.startswith(b"%PDF")
    assert len(data) > 1000, "退化成空文件 —— 前端会下载到一个打不开的 PDF"


def test_tier_downgrade_keeps_two_columns():
    """存在「首档排不下、低档能压回一页」的长度，且降档后**仍是双栏**。

    若这里找不到这样的长度，说明「单档失败就降档」的分支从未被真正执行过。
    """
    hit = None
    for per in range(6, 30):
        payload = _payload(bullets=per)
        first, _ = _story(payload, SECTIONS, E._SIZE_TIERS[0])
        if E._render_tier(first, E._SIZE_TIERS[0][5]) is not None:
            continue
        for tier in E._SIZE_TIERS[1:]:
            story, _ = _story(payload, SECTIONS, tier)
            rendered = E._render_tier(story, tier[5])
            # 不抛异常 ≠ 塞进一页：reportlab 会把整行挪到第 2 页，所以要求 pages == 1
            if rendered is not None and rendered[1] == 1:
                hit = (tier, story)
                break
        if hit:
            break
    assert hit is not None, "6..29 条要点里没有任何长度能触发降档重试"
    tier, story = hit
    assert _two_col(story, tier[5]) is not None, "降档把双栏降成了单栏"


# ---------------- 产物级：从 PDF 字节里读回真实落笔坐标 ----------------

_MID = A4[0] / 2


def _painted(pdf: bytes):
    """从导出产物读回 [(绝对 x, 绝对 y, 文本)]。

    ⚠️ 必须把 `cm` 与 `tm` 复合成绝对坐标：PyPDF2 visitor 给的 `tm` 只是当前
    文本矩阵，reportlab 大量段落以 ``tm[4] == 0`` 落笔，直接取 tm 会得到一片
    0.0，把「双栏已生效」误判成没生效。
    ⚠️ 也别想着在 canvas 子类里挂 ``beginText``/``drawString`` 钩子：reportlab 4.x
    画段落文字不走这两个方法，钩子会拿到 0 条记录（同样会误判）。
    """
    import io

    from PyPDF2 import PdfReader

    out = []

    def visitor(text, cm, tm, font_dict, font_size):
        stripped = (text or "").strip()
        if not stripped:
            return
        ax = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
        ay = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
        out.append((round(float(ax), 1), round(float(ay), 1), stripped))

    for page in PdfReader(io.BytesIO(pdf)).pages:
        page.extract_text(visitor_text=visitor)
    return out


def _x_of(painted, needle):
    for x, _y, text in painted:
        if needle in text:
            return x
    return None


def test_exported_pdf_paints_sidebar_on_the_right():
    """产物级判据：不看 story，只看最终 PDF 里文字的真实落笔 x。

    反向对照在下一个用例（单栏必须全部贴左）。
    """
    pdf = E.export_to_pdf(_Resume(_payload()), None, None, layout="split")
    painted = _painted(pdf)
    assert len(painted) >= 10, f"PDF 里读不回文字，判据失效：{painted[:3]}"

    for needle in ("技能", "证书", "FastAPI"):
        x = _x_of(painted, needle)
        assert x is not None, f"产物里找不到「{needle}」"
        assert x > _MID, f"「{needle}」画在 x={x}，没有落在右半页（中线 {_MID:.0f}）"

    for needle in ("OfferFlow", "负责订单结算"):
        x = _x_of(painted, needle)
        assert x is not None, f"产物里找不到「{needle}」"
        assert x < _MID, f"「{needle}」画在 x={x}，经历类内容必须留在主栏"


def test_exported_single_column_paints_everything_on_the_left():
    """反向对照：单栏产物里同一批文字必须全部贴左（否则上一条判据不算数）。"""
    pdf = E.export_to_pdf(_Resume(_payload()), None, None, layout="single")
    painted = _painted(pdf)
    for needle in ("技能", "证书", "FastAPI", "OfferFlow"):
        x = _x_of(painted, needle)
        assert x is not None and x < _MID, f"单栏里「{needle}」跑到了 x={x}"


# ---------------- 接口层 ----------------


def _make_resume(client, raw_text="张三\n技能：Python、FastAPI\n证书：英语六级\n"):
    r = client.post("/api/v1/resume/upload", json={"raw_text": raw_text})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def test_layouts_endpoint(client):
    r = client.get("/api/v1/resume/layouts")
    assert r.status_code == 200
    keys = {item["key"]: item for item in r.json()["data"]}
    assert set(keys) == {"single", "split"}
    assert keys["single"]["columns"] == 1 and keys["split"]["columns"] == 2
    assert keys["split"]["sidebar_sections"] == ["技能", "证书"]
    assert all(item["label"] and item["desc"] for item in keys.values())


def test_export_pdf_split_differs_from_single(client):
    resume_id = _make_resume(client)
    single = client.get(f"/api/v1/resume/{resume_id}/export",
                        params={"format": "pdf", "layout": "single"})
    split = client.get(f"/api/v1/resume/{resume_id}/export",
                       params={"format": "pdf", "layout": "split"})
    assert single.status_code == 200 and split.status_code == 200
    assert single.content.startswith(b"%PDF") and split.content.startswith(b"%PDF")
    assert len(split.content) != len(single.content), "版式参数没传到渲染层"


def test_word_rejects_split_layout_loudly(client):
    """Word 暂不支持多栏：必须明确报错，不能静默给一份单栏。"""
    resume_id = _make_resume(client)
    r = client.get(f"/api/v1/resume/{resume_id}/export",
                   params={"format": "word", "layout": "split"})
    assert r.status_code == 400
    assert "多栏" in r.json()["detail"]
    ok = client.get(f"/api/v1/resume/{resume_id}/export",
                    params={"format": "word", "layout": "single"})
    assert ok.status_code == 200


def test_export_unknown_layout_is_single(client):
    resume_id = _make_resume(client)
    single = client.get(f"/api/v1/resume/{resume_id}/export", params={"format": "pdf"})
    bogus = client.get(f"/api/v1/resume/{resume_id}/export",
                       params={"format": "pdf", "layout": "whatever"})
    assert bogus.status_code == 200
    assert len(bogus.content) == len(single.content)
