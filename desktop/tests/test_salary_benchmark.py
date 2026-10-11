#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""薪资基准「刷新 / 导入」的判据（G8）。

三层：
  1. **纯函数层**：导入 JSON 的解析与校验口径（坏 JSON / 顶层不是对象 /
     缺字段 / 字段为空）；
  2. **客户端层**：基准数据的**主存已换成本机**（`core.salary_benchmark_store`）——
     三个封装一个网络请求都不发；「刷新」固定拒绝且说明指向「导入」；
     导入的校验失败不能吞（吞掉 = 用户以为行情换掉了）；
  3. **控件层**：信息条文案、刷新/导入后的按钮复位、以及「换了基准版本就把
     旧的市场价结果清掉」（那是上一版行情算的，留着会误导）。

⚠️ 桌面版与网页版**各管一份**基准数据（服务端不给数据表通道，反过来自动上行又会
   改动所有账号的算价）。所以这里的用例一律把 `local_store` 切到一个**测试专用账号**
   再操作，既不碰用户真实数据，也不真调任何云端 benchmark 接口。

反向对照见 `_mutate_p1_check.py`。

运行：python tests/test_salary_benchmark.py [TestCase.test_method]
"""

import json
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtGui import QShowEvent                                # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QPushButton,  # noqa: E402
                               QWidget)

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop import api_client, local_store             # noqa: E402
from offerflow_desktop.api_client import ApiError                   # noqa: E402
from offerflow_desktop.core import salary_benchmark as bench        # noqa: E402
from offerflow_desktop.core import salary_benchmark_store          # noqa: E402
from offerflow_desktop.ui.pages import salary                       # noqa: E402

VALID = {
    "degree_base": {"bachelor": 9000},
    "direction_coef": {"ai": 1.2},
    "city_tiers": {"t1": [1.3, "一线"]},
}

#: 一份**结构合法且 key 全部命中白名单**的完整基准数据。
#: 注意 `VALID` 的 key（bachelor/t1）不在白名单里，会被 `sanitize_data`
#: 静默换成默认值 —— 想验证「导入后数据真的变了」必须用标准 key。
FULL_VALID = {
    "degree_base": {k: [v[0] * 2, v[1] * 2] for k, v in bench.DEGREE_BASE.items()},
    "direction_coef": {k: [v[0], v[1]] for k, v in bench.DIRECTION_COEF.items()},
    "city_tiers": {k: [v[0], v[1]] for k, v in bench.CITY_TIERS.items()},
}

#: `_on_reference` 需要的字段（形状对齐服务端 get_salary_reference）
REF = {
    "degree": "本科", "direction": "AI 应用", "city_tier": "一线",
    "salary_range": {"low": 9000, "high": 15000},
    "suggest_target": 13000, "suggest_bottom": 10000,
    "market_basis": "2026 届行情", "highlights": [],
    "analysis": "", "sources": [], "caveats": [],
}


class _Sig:
    """极简信号替身（只支持 connect / disconnect / emit）。"""

    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def disconnect(self):
        self.slots = []

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class _SyncWorker:
    """同步假 worker：立即执行任务，按真实 worker 的信号语义回传。

    为什么不直接用 `LLMWorker`：它会起后台线程去调**真实接口**；而基准导入是
    全局写操作，测试里一次都不许发生。
    """

    def __init__(self, fn):
        self._fn = fn
        self.success = _Sig()
        self.failed = _Sig()
        self.started = False

    def start(self):
        self.started = True
        try:
            self.success.emit(self._fn())
        except Exception as exc:          # noqa: BLE001 —— 对齐 LLMWorker.run
            self.failed.emit(str(exc))


class _FakeMW(QWidget):
    def __init__(self):
        super().__init__()
        self.navigated = []

    def navigate(self, *args, **kwargs):
        self.navigated.append(args[0] if args else None)

    def refresh_status(self):
        pass

    def open_agent(self, *args, **kwargs):
        pass


# ======================== 1. 纯函数层 ========================

class TestParsePayload(unittest.TestCase):
    """导入 JSON 的解析与校验（不弹窗、不联网）。"""

    def test_accepts_complete_payload(self):
        data, err = salary.parse_benchmark_payload(json.dumps(VALID))
        self.assertEqual(err, "")
        self.assertEqual(data, VALID)

    def test_blank_input_is_rejected(self):
        data, err = salary.parse_benchmark_payload("")
        self.assertIsNone(data)
        self.assertTrue(err)

    def test_broken_json_is_rejected(self):
        data, err = salary.parse_benchmark_payload('{"degree_base": ')
        self.assertIsNone(data)
        self.assertIn("JSON", err)

    def test_non_object_top_level_is_rejected(self):
        data, err = salary.parse_benchmark_payload(json.dumps([1, 2, 3]))
        self.assertIsNone(data)
        self.assertIn("对象", err)

    def test_every_required_field_is_checked(self):
        # 逐个摘掉必需字段，报错必须点名缺的是哪个（只说"缺少字段"用户无从下手）
        for field in salary.BENCHMARK_REQUIRED_FIELDS:
            payload = json.loads(json.dumps(VALID))
            payload.pop(field)
            data, err = salary.parse_benchmark_payload(json.dumps(payload))
            self.assertIsNone(data, "%s 缺失时不该通过" % field)
            self.assertIn(field, err)

    def test_empty_required_field_counts_as_missing(self):
        # 空 dict 也是"没给"：只判 `key in payload` 会把它放进去，
        # 后端 sanitize_data 再抛 400，用户白跑一趟网络
        payload = json.loads(json.dumps(VALID))
        payload["degree_base"] = {}
        data, err = salary.parse_benchmark_payload(json.dumps(payload))
        self.assertIsNone(data)
        self.assertIn("degree_base", err)


class TestResolveDirection(unittest.TestCase):
    """方向识别：ASCII 关键词按**词边界**（不得被子串误伤），CJK 仍子串。"""

    def test_ascii_substring_does_not_hijack_ai_or_chip(self):
        # email / training / campaign 含 "ai"，curriculum 含 "ic" —— 都不得误判
        self.assertNotEqual(bench.resolve_direction("Backend Engineer, email service"), "ai")
        self.assertNotEqual(bench.resolve_direction("Maintain and explain the domain"), "ai")
        self.assertNotEqual(bench.resolve_direction("Marketing campaign specialist"), "ai")
        self.assertNotEqual(bench.resolve_direction("Curriculum Vitae screening"), "chip")

    def test_real_ai_keywords_still_hit(self):
        self.assertEqual(bench.resolve_direction("AI 算法工程师"), "ai")
        self.assertEqual(bench.resolve_direction("LLM RAG 应用开发"), "ai")

    def test_cjk_and_core_roles_still_resolve(self):
        self.assertEqual(bench.resolve_direction("java 后端开发"), "tech")
        self.assertEqual(bench.resolve_direction("前端开发"), "frontend")
        self.assertEqual(bench.resolve_direction("产品经理"), "product")
        self.assertEqual(bench.resolve_direction(""), "general")

    def test_smart_prefix_follows_domain_not_ai(self):
        """A：「智能X」按 X 的领域归类，不得被宽词「智能」一律抢成 ai。

        修前 ai 关键词含宽词「智能」，``智能制造工程师``/``智能硬件`` 会被判成 ai
        （白套 1.45 高系数）；改为只保留「智能驾驶」等明确复合词后，
        前者归机械（「制造」）、后者归芯片（「硬件」）。反向对照：
        真正的智能复合词仍须落到 ai。
        """
        self.assertEqual(bench.resolve_direction("智能制造工程师"), "mechanical")
        self.assertEqual(bench.resolve_direction("智能硬件工程师"), "chip")
        # 反向对照：ai 的明确复合词没被这次收窄误伤
        self.assertEqual(bench.resolve_direction("智能驾驶算法工程师"), "ai")
        self.assertEqual(bench.resolve_direction("智能语音工程师"), "ai")

    def test_english_role_titles_resolve(self):
        """B：英文岗位名不再一律落到 general（漏判会算出错误区间）。"""
        self.assertEqual(bench.resolve_direction("Frontend Engineer"), "frontend")
        self.assertEqual(bench.resolve_direction("Backend Engineer"), "tech")
        self.assertEqual(bench.resolve_direction("Data Analyst"), "tech")
        self.assertEqual(bench.resolve_direction("Data Scientist"), "ai")
        self.assertEqual(bench.resolve_direction("Machine Learning Engineer"), "ai")
        self.assertEqual(bench.resolve_direction("Mechanical Engineer"), "mechanical")
        # 反向对照：ascii 词边界仍生效，email/training 不得把后端岗拽成 ai
        self.assertEqual(bench.resolve_direction("Backend Engineer, email service"), "tech")

    def test_resume_side_key_alias_bridges(self):
        """C：简历侧 key ``environmental`` 与薪资侧 ``environment`` 同义异名。

        直接喂简历侧 key 时，此前会因不在 ``DIRECTION_COEF`` 里而**静默回退
        general**（丢系数）；桥接后应与 ``environment`` 完全等价。
        """
        self.assertEqual(bench.resolve_direction("environmental"), "environment")
        self.assertEqual(bench.resolve_direction("environment"), "environment")
        self.assertEqual(
            bench.estimate_salary_range("bachelor_normal", "environmental", "tier1"),
            bench.estimate_salary_range("bachelor_normal", "environment", "tier1"))
        self.assertEqual(bench.direction_label("environmental"), "环境/环保")
        # 反向对照：简历侧也认薪资侧写法（skill_keywords 的反向桥接）
        from offerflow_desktop.core import skill_keywords as SK
        self.assertTrue(SK.top_keywords("environment"))
        self.assertEqual(SK.top_keywords("environment"), SK.top_keywords("environmental"))

    def test_resume_direction_keys_all_valid_in_salary(self):
        """防漂移护栏：简历侧每个方向 key（经桥接）都要能在薪资侧查到系数。

        两端方向集合是「薪资 ⊇ 简历」的超集（薪资另有 frontend/chip/admin/logistics），
        但**简历侧不得存在薪资侧没有、又没进 `_DIRECTION_KEY_ALIASES` 的 key** ——
        否则该方向算薪时会静默回退 general。这是 C 类漂移的长期防线。
        """
        from offerflow_desktop.core import skill_keywords as SK
        for key in SK.directions():
            norm = bench.normalize_direction_key(key)
            self.assertIn(norm, bench.DIRECTION_COEF,
                          "简历侧方向 %r 在薪资侧无对应（需补 %s）" % (key, "_DIRECTION_KEY_ALIASES"))


# ======================== 2. 客户端层 ========================

_TEST_ACCOUNT = "bench-test@local"


class TestApiClientLocalSemantics(unittest.TestCase):
    """三个封装**本地化之后**的契约：一个网络请求都不发、刷新固定拒绝、导入落本机。

    ⚠️ 这里测的是「基准数据的主存换成了本机」这条改造本身，不是 HTTP 形状。
    旧版本断言的是「打了 GET/POST 到哪个云端路径」—— 本地化后那些断言全部失效，
    但它们**想保护的东西**没变：① 刷新不能假装成功；② 导入不能吞掉校验失败；
    ③ 读接口必须优雅降级而不是抛错。这里逐条换成新形态。
    """

    def setUp(self):
        self._prev = local_store.get_account()
        local_store.set_account(_TEST_ACCOUNT)
        local_store.clear(_TEST_ACCOUNT)

        def _restore():
            local_store.clear(_TEST_ACCOUNT)
            local_store.set_account(self._prev)

        self.addCleanup(_restore)

    def test_no_network_request_at_all(self):
        """核心不变量：三个封装**一个 `_request` 都不发**（这是「本机化」的定义）。"""
        with mock.patch.object(api_client, "_request") as req:
            api_client.salary_benchmark_info()
            api_client.salary_benchmark_import(FULL_VALID, "2027", "某机构 2027 届薪酬报告", "备注")
            with self.assertRaises(ApiError):
                api_client.salary_benchmark_refresh()
            req.assert_not_called()

    def test_info_degrades_to_builtin_baseline(self):
        """从没导入过 → 回退内置基线（version=0 / source="内置基线"），**不抛错**。

        薪资参考在没导入任何权威数据时也必须可用，否则「第一次打开就报错」。
        """
        info = api_client.salary_benchmark_info()
        self.assertEqual(info["version"], 0)
        self.assertEqual(info["source"], "内置基线")
        self.assertIn("degree_base", salary_benchmark_store.active_data())

    def test_refresh_is_hard_rejected(self):
        """刷新固定拒绝：模型估算会编造数字污染真实数据，且说明要指向「导入」。"""
        with self.assertRaises(ApiError) as ctx:
            api_client.salary_benchmark_refresh()
        self.assertIn("导入", str(ctx.exception))

    def test_import_writes_local_version_and_is_readable_back(self):
        """导入整份数据落到本机：版本自增、数据可回读、元信息带上了来源与年份。

        只断言"没抛错"是空靶 —— `refresh` 完全可能返回成功却什么都没写。
        这里断言**回读出来的数值确实变了**（degree_base 整体翻倍）。
        """
        info = api_client.salary_benchmark_import(
            FULL_VALID, "2027", "某机构 2027 届薪酬报告", "备注")
        self.assertEqual(info["version"], 1)
        self.assertEqual(info["data_year"], "2027")
        self.assertEqual(info["source"], "某机构 2027 届薪酬报告")

        active = salary_benchmark_store.active_data()
        for key, (low, high) in bench.DEGREE_BASE.items():
            self.assertEqual(active["degree_base"][key], [low * 2, high * 2],
                             "导入的数据没有真正落进生效基准表：%s" % key)

        # 第二次导入版本再自增（界面上的「第 N 版」必须单调，不能回退）
        info2 = api_client.salary_benchmark_import(FULL_VALID, "2028", "另一家", "")
        self.assertEqual(info2["version"], 2)

    def test_import_rejects_incomplete_data_without_writing(self):
        """三个必需字段缺一 → 拒绝且**什么都不写**（脏数据落盘会污染之后每一次算价）。"""
        before = salary_benchmark_store.active_data()["version"]
        with self.assertRaises(ApiError) as ctx:
            api_client.salary_benchmark_import({"degree_base": {"bachelor_normal": [1, 2]}}, "", "", "")
        self.assertIn("不合法", str(ctx.exception))
        self.assertEqual(salary_benchmark_store.active_data()["version"], before)

    def test_import_requires_login(self):
        """未登录时导入必须**明确报错**，不能静默成功（用户会以为行情换掉了）。"""
        local_store.set_account(None)
        with self.assertRaises(ApiError) as ctx:
            api_client.salary_benchmark_import(FULL_VALID, "2027", "某机构", "")
        self.assertIn("登录", str(ctx.exception))

    def test_import_reports_silently_replaced_fields(self):
        """#9：端到端 —— 被静默兜底的字段必须出现在返回体的 `data_report` 里。

        旧版只报「字段名不认识」，取值非法（越界/形状不对）同样被静默换成内置默认值，
        用户看到「已导入到 v2」却发现数字一个都没变。
        """
        dirty = {
            "degree_base": {k: [v[0] * 2, v[1] * 2] for k, v in bench.DEGREE_BASE.items()},
            "direction_coef": {k: [v[0], v[1]] for k, v in bench.DIRECTION_COEF.items()},
            "city_tiers": {k: [v[0], v[1]] for k, v in bench.CITY_TIERS.items()},
        }
        dirty["degree_base"]["bachelor_normal"] = {}      # 形状不对 → 兜底
        dirty["city_tiers"]["tier1"] = [0.1, "一线"]      # 系数越界 → 兜底

        info = api_client.salary_benchmark_import(dirty, "2027", "某机构 2027 届薪酬报告", "")
        rep = info.get("data_report") or {}
        self.assertIn("bachelor_normal",
                      (rep.get("replaced_keys") or {}).get("degree_base", []),
                      "形状不对的字段被兜底却没报告")
        self.assertIn("tier1",
                      (rep.get("replaced_keys") or {}).get("city_tiers", []),
                      "系数越界的字段被兜底却没报告")
        # 反向对照：三张表都按白名单给全的数据**不该**报 missing（否则提示变成噪音）
        clean = api_client.salary_benchmark_import(FULL_VALID, "2027", "某机构", "")
        clean_rep = clean.get("data_report") or {}
        self.assertNotIn("missing_keys", clean_rep, "给全了的字段不该报 missing")


# ======================== 3. 控件层 ========================

class _Base(unittest.TestCase):
    def _build_page(self):
        mw = _FakeMW()
        p1 = mock.patch.object(salary.db, "list_resumes", return_value=[])
        p2 = mock.patch.object(salary, "LLMWorker", _SyncWorker)
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)
        page = salary.SalaryPage(mw)
        self.addCleanup(page.deleteLater)
        return page


class TestSalaryPageBenchmarkBar(_Base):
    """信息条 + 刷新 / 导入的状态机。"""

    def test_label_shows_version_and_year(self):
        page = self._build_page()
        page._on_benchmark_info({"version": 3, "data_year": "2027"})
        text = page.bench_label.text()
        self.assertIn("v3", text)
        self.assertIn("2027", text)

    def test_label_degrades_when_info_unavailable(self):
        page = self._build_page()
        page._on_benchmark_info({})
        self.assertIn("不可用", page.bench_label.text())

    def test_ctor_does_not_call_benchmark_api(self):
        # 构造期只搭界面：信息条在 showEvent 里拉
        with mock.patch.object(salary.db, "list_resumes", return_value=[]), \
             mock.patch.object(salary, "LLMWorker", _SyncWorker), \
             mock.patch.object(salary.db, "salary_benchmark_info") as info:
            salary.SalaryPage(_FakeMW())
        info.assert_not_called()

    def test_show_event_loads_benchmark_info(self):
        page = self._build_page()
        with mock.patch.object(salary.db, "salary_benchmark_info",
                               return_value={"version": 9, "data_year": "2027"}) as info:
            page.showEvent(QShowEvent())
        info.assert_called_once()
        self.assertIn("v9", page.bench_label.text())

    def test_refresh_button_is_gone(self):
        """#5：**死按钮必须真的从界面消失**。

        「刷新市场数据」在两版都是硬拒绝（`api_client.salary_benchmark_refresh` 固定抛错，
        模型估算会编造数字污染真实行情），留一个点了必然报错的按钮只会让用户以为坏了。
        这里锁死它不再存在；反向对照：把按钮加回去，本判据立刻变红。
        """
        page = self._build_page()
        self.assertFalse(hasattr(page, "bench_refresh_btn"),
                         "「刷新市场数据」死按钮又回来了")
        self.assertNotIn("刷新市场数据",
                         [b.text() for b in page.findChildren(QPushButton)])
        self.assertFalse(hasattr(page, "_refresh_benchmark"),
                         "按钮没了，刷新方法也该一起删掉（否则是留着手动调用的暗门）")

    def test_import_surfaces_replaced_and_missing_fields(self):
        """#9：被静默兜底的字段必须出现在导入提示里。

        旧版只报「字段名不认识」，取值非法（`replaced_keys`）与缺字段（`missing_keys`）
        同样被静默换成内置默认值 —— 用户看到「导入成功」却数字一个都没变。
        """
        page = self._build_page()
        info = {
            "version": 7, "data_year": "2027",
            "data_report": {
                "unknown_keys": {"degree_base": ["bachelor"]},
                "replaced_keys": {"city_tiers": ["t1"]},
                "missing_keys": {"direction_coef": ["ai"]},
            },
        }
        page._on_benchmark_imported(info)
        text = page.bench_msg.text()
        self.assertIn("本机", text)
        self.assertIn("bachelor", text, "不认识的字段名必须点名")
        self.assertIn("t1", text, "被换成默认值的字段必须点名")
        self.assertIn("ai", text, "没提供的字段必须点名")
        self.assertNotIn("所有账号", text)

    def test_import_success_uses_dialog_payload_and_says_local_only(self):
        """导入走对话框 payload，且文案必须说清**只在本机生效**。

        桌面版的基准数据存本机（网页版那份是全局的、两端不共享），
        所以不能说「对所有账号生效」—— 用户会以为网页版也跟着换了行情。
        """
        page = self._build_page()
        page._on_reference(dict(REF))
        seen = {}

        class _Dlg:
            def __init__(self, parent=None):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def payload(self):
                return (dict(VALID), "2027", "某机构 2027 届薪酬报告")

        def fake_import(data, year, source):
            seen["args"] = (data, year, source)
            return {"version": 6, "data_year": "2027"}

        with mock.patch.object(salary, "_BenchmarkImportDialog", _Dlg), \
             mock.patch.object(salary.db, "salary_benchmark_import", fake_import):
            page._open_benchmark_import()

        self.assertEqual(seen["args"][1], "2027")
        self.assertEqual(seen["args"][2], "某机构 2027 届薪酬报告")
        self.assertEqual(seen["args"][0], VALID)
        self.assertIn("v6", page.bench_label.text())
        # 基准数据在**本机**：不能出现「对所有账号生效」这类说法
        self.assertIn("本机", page.bench_msg.text())
        self.assertNotIn("所有账号", page.bench_msg.text())
        self.assertTrue(page.bench_import_btn.isEnabled())
        self.assertEqual(page.ref_result_layout.count(), 1)

    def test_import_cancel_does_not_call_api(self):
        page = self._build_page()

        class _Dlg:
            def __init__(self, parent=None):
                pass

            def exec(self):
                return QDialog.DialogCode.Rejected

        with mock.patch.object(salary, "_BenchmarkImportDialog", _Dlg), \
             mock.patch.object(salary.db, "salary_benchmark_import") as imp:
            page._open_benchmark_import()
        imp.assert_not_called()

    def test_import_failure_reports_and_restores_button(self):
        page = self._build_page()

        class _Dlg:
            def __init__(self, parent=None):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def payload(self):
                return (dict(VALID), "2027", "")

        def boom(data, year, source):
            raise ApiError("导入的数据缺少 city_tiers 结构")

        with mock.patch.object(salary, "_BenchmarkImportDialog", _Dlg), \
             mock.patch.object(salary.db, "salary_benchmark_import", boom):
            page._open_benchmark_import()
        self.assertIn("city_tiers", page.bench_msg.text())
        self.assertTrue(page.bench_import_btn.isEnabled())


class TestSalaryReferenceAskWiring(_Base):
    """#1：开口价必须用 `suggest_ask`，不能拿 `suggest_target` 冒充。

    数据层返回**三档** `(ask, target, bottom)`，`ask = 区间上限 × 1.15`；旧版 UI 三处
    都读 `suggest_target`，导致「开口价」显示/带入的其实是目标价，15% 锚定空间白算，
    还与 AI 解读里「第一次开口要报开口价 {ask}、不是目标价 {target}」自相矛盾。
    """

    def _labels(self, page):
        texts = []
        for i in range(page.ref_result_layout.count()):
            item = page.ref_result_layout.itemAt(i)
            w = item.widget() if item else None
            if w is not None:
                texts.extend(lb.text() for lb in w.findChildren(QLabel))
        return "\n".join(texts)

    def test_card_displays_ask_not_target(self):
        page = self._build_page()
        page._on_reference(dict(REF, suggest_ask=15000))   # ask(15000) > target(13000)
        text = self._labels(page)
        self.assertIn("开口价", text)
        # 卡片金额按千分位格式化，所以断言要用 f"{n:,}"
        self.assertIn(f"{15000:,}", text, "开口价一栏必须显示 suggest_ask")
        self.assertIn(f"{13000:,}", text, "目标价仍需展示（三档都给出）")

    def test_card_falls_back_to_target_when_ask_missing(self):
        """反向对照：老数据/旧返回体没有 suggest_ask 时不能空白或崩。"""
        page = self._build_page()
        ref = {k: v for k, v in REF.items() if k != "suggest_ask"}
        page._on_reference(ref)
        self.assertIn(f"{REF['suggest_target']:,}", self._labels(page))

    def test_jump_fills_ask_into_opening_field(self):
        page = self._build_page()
        page._jump_to_negotiation(dict(REF, suggest_ask=15000))
        self.assertEqual(page.tabs.currentIndex(), 1)
        self.assertEqual(page.neg_target.text(), "15000", "带入谈判的必须是开口价")
        self.assertEqual(page.neg_bottom.text(), str(REF["suggest_bottom"]))

    def test_opening_field_is_labelled_ask(self):
        """输入框标签必须是「开口报价」（谈判契约把该值当开口价）。"""
        page = self._build_page()
        self.assertIn("开口报价", [lb.text() for lb in page.findChildren(QLabel)])


class TestImportDialogValidation(unittest.TestCase):
    """弹窗自己的本地校验（明显写错就别走一轮网络）。"""

    def _dlg(self):
        d = salary._BenchmarkImportDialog()
        self.addCleanup(d.deleteLater)
        return d

    def test_valid_payload_is_accepted(self):
        d = self._dlg()
        d.json_edit.setPlainText(json.dumps(VALID))
        d.year_edit.setText("2027")
        d.source_edit.setText("某机构报告")
        d._on_ok()
        self.assertEqual(d.result(), int(QDialog.DialogCode.Accepted))
        data, year, source = d.payload()
        self.assertEqual(data, VALID)
        self.assertEqual(year, "2027")
        self.assertEqual(source, "某机构报告")

    def test_bad_json_stays_open_and_shows_reason(self):
        d = self._dlg()
        d.json_edit.setPlainText("{oops")
        d._on_ok()
        self.assertNotEqual(d.result(), int(QDialog.DialogCode.Accepted))
        self.assertFalse(d.msg.isHidden())
        self.assertIn("JSON", d.msg.text())
        self.assertIsNone(d.payload()[0])

    def test_missing_field_is_named(self):
        d = self._dlg()
        d.json_edit.setPlainText(json.dumps({"degree_base": {"b": 1}}))
        d._on_ok()
        self.assertNotEqual(d.result(), int(QDialog.DialogCode.Accepted))
        self.assertIn("direction_coef", d.msg.text())


class TestSanitizeReportSource(unittest.TestCase):
    """#9：`sanitize_data` 的 report 出参必须真的记录三桶（渲染层是另一套判据）。"""

    def test_reports_replaced_unknown_and_missing(self):
        dirty = {
            "degree_base": {"bachelor_normal": [0, 0]},      # 取值非法 → replaced
            "direction_coef": {k: [v[0], v[1]] for k, v in bench.DIRECTION_COEF.items()},
            "city_tiers": {"t1": [1.3, "一线"]},              # t1 不在白名单 → unknown
        }
        rep = {}
        cleaned = bench.sanitize_data(dirty, report=rep)
        self.assertIsNotNone(cleaned)
        self.assertEqual(rep["replaced_keys"]["degree_base"], ["bachelor_normal"])
        self.assertEqual(rep["unknown_keys"]["city_tiers"], ["t1"])
        # 没给的白名单 key → missing（city_tiers 只给了 t1，tier2/tier3 都缺）
        self.assertIn("tier2", rep["missing_keys"]["city_tiers"])
        # 反向对照：给全了的表不该报 missing
        self.assertNotIn("direction_coef", rep["missing_keys"])

    def test_report_is_optional_and_does_not_change_clean_output(self):
        """不传 report ≡ 老行为；传了也不改变清洗结果。"""
        dirty = {"degree_base": {"bachelor_normal": [0, 0]},
                 "direction_coef": {k: [v[0], v[1]] for k, v in bench.DIRECTION_COEF.items()},
                 "city_tiers": {k: [v[0], v[1]] for k, v in bench.CITY_TIERS.items()}}
        rep = {}
        self.assertEqual(bench.sanitize_data(dirty), bench.sanitize_data(dirty, report=rep))
        self.assertTrue(rep, "传了 report 却什么都没记")


class TestWorkerLifecycle(_Base):
    """#4：页面销毁必须断开后台回调。

    `LLMWorker` 由模块级 `_ALIVE` 强引用保活，回调**一定会跑**；页面若已被销毁
    （退出登录 → 重建主窗口），回调里访问任何 Qt 属性都会 0xC0000409 —— 无 traceback
    直接崩进程。`destroyed` 是子页面（QStackedWidget 的 widget）的确定性析构钩子。
    """

    def test_destroyed_signal_triggers_cleanup(self):
        page = self._build_page()
        page._load_benchmark_info()
        self.assertTrue(page._workers, "起过 worker 却没被记录，销毁时无从断连")
        page.destroyed.emit()          # 模拟子页面析构
        self.assertEqual(page._workers, [], "destroyed 没有接到清理钩子")

    def test_cleanup_disconnects_both_signals(self):
        page = self._build_page()
        page._load_benchmark_info()
        w = page._workers[0]
        seen = []
        w.success.connect(lambda r: seen.append(("ok", r)))
        w.failed.connect(lambda m: seen.append(("err", m)))
        page._cleanup_workers()
        w.success.emit({"version": 1})
        w.failed.emit("boom")
        self.assertEqual(seen, [], "断连后仍触发了回调")


class TestBenchmarkReportRendering(unittest.TestCase):
    """#9：`format_benchmark_report` 的三桶渲染（纯函数，不弹窗、不起页面）。"""

    def test_clean_import_has_no_warning(self):
        """一切正常时不能凭空多出一段警告（否则用户会习惯性忽略它）。"""
        self.assertEqual(salary.format_benchmark_report({"version": 3, "data_year": "2027"}), "")

    def test_each_bucket_is_named_with_its_nature(self):
        text = salary.format_benchmark_report({"data_report": {
            "unknown_keys": {"degree_base": ["bachelor"]},
            "replaced_keys": {"city_tiers": ["t1"]},
            "missing_keys": {"direction_coef": ["ai"]},
        }})
        self.assertIn("bachelor", text)
        self.assertIn("t1", text)
        self.assertIn("ai", text)
        # 三桶性质不同，说清各自含义用户才知道怎么改
        self.assertIn("识别", text)       # 字段名不认识
        self.assertIn("不合法", text)     # 取值非法
        self.assertIn("未提供", text)     # 没给

    def test_legacy_flat_unknown_keys_still_rendered(self):
        """反向对照：只带平铺 `unknown_keys` 的旧返回体不能无声无息。"""
        text = salary.format_benchmark_report(
            {"unknown_keys": {"degree_base": ["bachelor"]}})
        self.assertIn("bachelor", text)

    def test_non_dict_input_does_not_crash(self):
        self.assertEqual(salary.format_benchmark_report(None), "")
        self.assertEqual(salary.format_benchmark_report({}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
