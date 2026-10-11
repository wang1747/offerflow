"""对比度口径回归测试：锁死「WCAG 亮度必须反伽马」这件事。

背景：resume 模板的标题/姓名色要用 ``darken_for_text`` 判断是否需要压深。
曾经这里的亮度函数直接拿 0..255 归一化后加权（少了 sRGB 反伽马），把中深色的
对比度算低了一大截（#808080 被算成 1.9，真实是 3.95），于是把本来已经满足
WCAG AA 的强调色（#4F46E5 真实 6.29:1）又白白压深了一遍，配色变得发闷。

这个测试用国际标准里的锚点值把它钉住：
  - #808080 on #FFFFFF → 3.95
  - #000000 on #FFFFFF → 21.0
  - #767676 on #FFFFFF → 4.54（WCAG 4.5 线的经典边界灰）
并验证：现有 6 套模板的主色本就达标，不得被压深；而真正的浅色要被压到达标。

运行：python tests/test_contrast_metric.py
"""

import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from offerflow_desktop.services.templates import (  # noqa: E402
    TEMPLATES, darken_for_text, _contrast,
)

# 标准锚点：(前景, 背景, 期望对比度, 容差)
ANCHORS = [
    ("808080", "FFFFFF", 3.95, 0.02),
    ("000000", "FFFFFF", 21.0, 0.05),
    ("767676", "FFFFFF", 4.54, 0.03),
    ("FFFFFF", "FFFFFF", 1.00, 0.01),
]


def _rgb(h):
    v = h.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def main() -> int:
    fails = []

    print("A. WCAG 亮度锚点")
    for fg, bg, want, tol in ANCHORS:
        got = _contrast(_rgb(fg), _rgb(bg))
        ok = abs(got - want) <= tol
        print("   #%s on #%s → %.2f（期望 %.2f %s）%s" % (
            fg, bg, got, want, "±%.2f" % tol, "OK" if ok else "XX"))
        if not ok:
            fails.append("锚点 #%s/#%s 偏差 %.2f" % (fg, bg, got - want))

    print()
    print("B. 现有模板主色本就达标 → 不得被压深")
    for t in TEMPLATES:
        ratio = _contrast(_rgb(t.accent_hex), (255, 255, 255))
        same = t.text_accent_hex.upper() == t.accent_hex.upper()
        ok = ratio >= 4.5 and same
        print("   %-9s #%s 对比度 %.2f  %s  %s" % (
            t.key, t.accent_hex, ratio,
            "未压深" if same else "被压深(XX)", "OK" if ok else "XX"))
        if not ok:
            fails.append("%s 被误压深" % t.key)

    print()
    print("C. 真正的浅色必须被压到达标（且不过度压深）")
    for hx in ("FBBF24", "FB923C", "22D3EE"):
        out = darken_for_text(hx)
        before = _contrast(_rgb(hx), (255, 255, 255))
        after = _contrast(_rgb(out), (255, 255, 255))
        # 达标 4.5，且不该冲到 6 以上（说明二分没有过冲）
        ok = after >= 4.4 and after <= 6.0
        print("   #%s %.2f → #%s %.2f  %s" % (
            hx, before, out.upper(), after, "OK" if ok else "XX"))
        if not ok:
            fails.append("#%s 压深结果 %.2f 不合理" % (hx, after))

    print()
    if fails:
        print("失败 %d 项：%s" % (len(fails), "；".join(fails)))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
