# -*- coding: utf-8 -*-
"""导出结果反馈：把「文件写成功了」与「写出来的东西是不是全」分开报（2026-10-07）。

## 为什么要单独一个模块

导出这件事有两个**独立**的结论，混在一起报最容易让人误判：

  * **写文件成功了吗** —— 失败就抛异常，是硬错误；
  * **写出去的内容全不全** —— 文件好好的，但可能**静默少了字**
    （中文字体子集化漏字形 / 一页调优裁掉尾段）。

后者是"成功"里藏着的失败。所以自检不通过时**不能**只弹一个绿框，
但也不该把已经写好的文件说成"导出失败"—— 文件是好的，用户可能还要用。

## 判据与文案分离

``export_message()`` 是纯函数（不碰 Qt），``notify_export_result()`` 只是
把它贴到对话框上。这样"该报什么"可以直接断言，不必去截获对话框。

## 结果对象是「鸭子类型」，不是某一个类

``result`` 只要满足几个成员即可（``ok`` / ``degraded`` / ``checked`` / ``note`` /
``summary()`` / ``details()``）：

* PDF 走 `core.pdf_verify.PdfVerifyResult`（把文件读回来）；
* JSON 走 `core.json_resume.JsonVerifyResult`（查对象结构）。

两种格式的失败模式不同，所以**修法建议的文案要分开**（见 ``_ADVICE``）——
对 PDF 说"进招聘系统可能读不到"，对 JSON 说"别的平台可能打不开"，
把 PDF 的话原样套到 JSON 上是错的。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from PySide6.QtWidgets import QMessageBox

# 自检没过时给用户的"下一步怎么办"。按导出格式分开：同一个红色框，
# 用户要采取的行动完全不同。
_ADVICE = {
    "pdf": ("文件已生成，可直接使用；但这份 PDF 进招聘系统后，"
            "被标出的部分也可能读不到，建议先按明细改一遍再投。"),
    "json": ("文件已生成，但它的结构不符合 JSON Resume 标准，"
             "拿到 jsonresume.org 或别的平台可能打不开。建议按明细核对。"),
}


def export_message(path: str, extra: str, result: Optional[Any],
                   kind: str = "pdf") -> Tuple[str, str, str]:
    """把一次导出折算成 ``(level, title, body)``。

    level 为 ``"warn"`` 表示"文件在，但内容可能不全"；``"info"`` 表示一切正常。
    没做校验（``result is None``）时**不编造结论**，只说没校验。

    ``kind`` 决定自检没过时给哪一段建议；未知值退到 pdf（保持向后兼容）。
    """
    body = f"已导出：\n{path}{extra or ''}"
    if result is None:
        return "info", "导出成功", body

    if result.degraded:
        return "info", "导出成功", f"{body}\n\n未做回读校验：{result.note}"

    if result.checked and not result.ok:
        details = "\n".join("· " + d for d in result.details())
        return ("warn", "导出完成，但内容可能不完整",
                f"{body}\n\n回读自检未通过：{result.summary()}\n"
                + (details + "\n" if details else "")
                + "\n" + _ADVICE.get(kind, _ADVICE["pdf"]))

    return "info", "导出成功", f"{body}\n\n回读自检：{result.summary()}"


def notify_export_result(parent: Any, path: str, extra: str,
                         result: Optional[Any], kind: str = "pdf") -> str:
    """按 ``export_message`` 的结论弹框，返回用掉的 level。"""
    level, title, body = export_message(path, extra, result, kind)
    if level == "warn":
        QMessageBox.warning(parent, title, body)
    else:
        QMessageBox.information(parent, title, body)
    return level
