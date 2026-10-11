# -*- coding: utf-8 -*-
"""有界撤销栈（2026-10-07）。

## 设计取舍：存快照，不存差异

这个界面里的"可撤销状态"是**一整个表单**（十来个输入框 + 可增删的经历卡片）。
存差异要维护每类操作的逆操作，改一个字段就得同步改逆操作；而表单快照是
一个纯 dict，序列化即可存盘 —— 顺手就把"关掉应用还能恢复"也做了。
代价是内存：一份快照几 KB，200 步上限下最坏几 MB，可以接受。

## 为什么去重是必须的

输入框的每一次按键都会产生一次"变更"，若不去重，200 步会被一次连续输入
吃光 —— 用户按一次 Ctrl+Z 只退掉一个字符。所以 ``push`` 对"和当前相同"
的快照直接丢弃（调用方另外做时间防抖，见页面里的 debounce 定时器）。

## 语义（这三条是判据）

  1. ``undo()`` 回退**一步**并返回回退后的状态；栈空返回 ``None``。
  2. ``redo()`` 是 ``undo()`` 的逆操作：连续 undo 再连续 redo 必须回到原状态。
  3. 任何一次新的 ``push`` 都会**清空重做栈** —— 这是撤销的通用语义，
     否则会出现"重做到一条已经不存在的时间线"。
"""

from __future__ import annotations

from typing import Any, Generic, List, Optional, TypeVar

T = TypeVar("T")

DEFAULT_LIMIT = 200


class UndoStack(Generic[T]):
    """有界快照撤销栈。

    ``limit`` 是**历史上限**（不含当前状态）。超出后从最旧的一端丢弃：
    用户更可能想撤销刚刚那几步，而不是十几次编辑以前的。
    """

    def __init__(self, limit: int = DEFAULT_LIMIT):
        self.limit = max(1, int(limit or DEFAULT_LIMIT))
        self._past: List[T] = []
        self._future: List[T] = []
        self._current: Optional[T] = None

    # ---------- 写入 ----------

    def push(self, snapshot: T) -> bool:
        """记一个新状态。与当前相同则忽略（返回 False）。"""
        if self._current is not None and snapshot == self._current:
            return False
        if self._current is not None:
            self._past.append(self._current)
            if len(self._past) > self.limit:
                # 从最旧的一端丢：保留"最近 N 步"
                del self._past[:len(self._past) - self.limit]
        self._current = snapshot
        self._future.clear()          # 新分支产生，旧的重做线作废
        return True

    def reset(self, snapshot: Optional[T] = None) -> None:
        """清空栈并把 ``snapshot`` 作为新的起点（切换简历/账号时用）。"""
        self._past.clear()
        self._future.clear()
        self._current = snapshot

    # ---------- 读取 ----------

    def undo(self) -> Optional[T]:
        if not self._past:
            return None
        if self._current is not None:
            self._future.append(self._current)
        self._current = self._past.pop()
        return self._current

    def redo(self) -> Optional[T]:
        if not self._future:
            return None
        if self._current is not None:
            self._past.append(self._current)
        self._current = self._future.pop()
        return self._current

    @property
    def current(self) -> Optional[T]:
        return self._current

    @property
    def can_undo(self) -> bool:
        return bool(self._past)

    @property
    def can_redo(self) -> bool:
        return bool(self._future)

    @property
    def undo_depth(self) -> int:
        """还能撤销多少步（用于界面上显示"可撤销 N 步"）。"""
        return len(self._past)

    @property
    def redo_depth(self) -> int:
        return len(self._future)
