"""LLM 客户端：直连 OpenAI 兼容 API（BYOK），带 QThread 后台工作线程。

桌面版不依赖 LangChain/LangGraph——用 requests 直连 /chat/completions，
把 Web 版的 Agent 调用收敛为「系统提示 + 用户提示 → 文本/JSON」的同步原语，
再由 QThread 包装成异步任务供 UI 调用。
"""

import json
import re
from typing import Any, Callable, Dict, List, Optional

import requests

from . import database as db


class LLMError(Exception):
    """LLM 调用异常（未配置 / 网络失败 / 返回异常）。"""


def get_llm_config() -> Dict[str, str]:
    cfg = db.get_model_config()
    if not cfg["has_config"]:
        raise LLMError(
            "本机还没有可用的模型：在「本地知识库」页点「本地模型…」配置"
            "（可填本机 Ollama）；或在「模型设置」里保存一次，会自动同步到本机"
        )
    return cfg


def chat(system_prompt: str, user_prompt: str, temperature: float = 0.1,
         timeout: int = 180) -> str:
    """调用 OpenAI 兼容 chat/completions，返回助手文本。"""
    cfg = get_llm_config()
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    headers = {"Content-Type": "application/json"}
    # 本机 Ollama / vLLM 这类内网模型不需要 Key；空 Key 时别发 "Bearer "，
    # 部分实现会当成无效凭据直接 401（错题本 E-08 顺带修）。
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        raise LLMError("模型请求超时，请稍后重试")
    except requests.exceptions.ConnectionError:
        raise LLMError("无法连接模型服务，请检查网络或 Base URL")
    except requests.exceptions.HTTPError as exc:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise LLMError(f"模型服务返回错误（{resp.status_code}）{': ' + detail if detail else ''}")
    except json.JSONDecodeError:
        raise LLMError("模型服务返回格式异常")
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError("模型返回内容为空或格式异常")


def test_connection(base_url: str, api_key: str, model: str) -> str:
    """测试连接：发送一条极短消息。成功返回模型回复。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "回复：连接成功"}],
        "max_tokens": 16,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or "连接成功"


def clean_json(raw: str) -> str:
    """清洗 LLM 输出的 JSON（去掉 ``` 围栏 / BOM / 前后杂文）。"""
    text = (raw or "").strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


def parse_json(raw: str) -> Optional[Any]:
    """容错解析 LLM 输出的 JSON（对象或数组都支持）。

    三级尝试：

      1. **整体直接解析** —— 模型老实输出 JSON 时走这条（最常见）；
      2. 从「最先出现的括号类型」截取 —— 前面有解释文字时用；
      3. 换另一种括号再试一次。

    ⚠️ 为什么不能只写「先找 `{` 再找 `}`」（2026-09-30 修）：
        JSON **数组**里含有对象时（`[{"a":1},{"b":2}]`），`find("{")`/`rfind("}")`
        会切出 `{"a":1},{"b":2}` —— 这不是合法 JSON，`json.loads` 必然失败，
        于是**所有数组形态的返回都会被判为"解析失败"**。
        P3c 记忆提炼要求模型返回数组，实测就是被这个坑挡住的。
    """
    text = clean_json(raw)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    candidates = []
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        s, e = text.find(open_ch), text.rfind(close_ch)
        if s != -1 and e > s:
            candidates.append((s, text[s:e + 1]))
    candidates.sort(key=lambda x: x[0])          # 用最先出现的那一段
    for _, chunk in candidates:
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    return None


# ============ QThread 后台任务 ============

from PySide6.QtCore import QThread, Signal  # noqa: E402

# 运行中的 worker 强引用集合（防止线程未结束时被 GC 销毁 → Qt abort）
_ALIVE: set = set()


class LLMWorker(QThread):
    """后台执行一个可调用任务，通过信号回传结果/异常。

    用法：
        worker = LLMWorker(lambda: some_llm_call(...))
        worker.success.connect(on_ok)
        worker.failed.connect(on_err)
        worker.start()

    ⚠️ 生命周期：**运行时由模块级 `_ALIVE` 持有强引用**。
    QThread 是 C++ 对象，Python 引用一旦归零就会被析构；若此时线程还在跑，
    Qt 会直接 abort（`QThread: Destroyed while thread is still running`）——
    表现为进程无异常、无 traceback 地静默崩退。踩坑场景：把 worker 存到
    `self._worker = worker` 这种单槽属性上，连续两次请求时前一个被覆盖即触发。
    这里把「保命引用」收进 LLMWorker 自己，调用方不必再操心。
    """

    success = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent=None):
        super().__init__(parent)
        self._fn = fn
        self.finished.connect(self._release)

    def start(self, *args, **kwargs):  # noqa: D102
        _ALIVE.add(self)
        super().start(*args, **kwargs)

    def _release(self) -> None:
        _ALIVE.discard(self)

    def run(self):
        try:
            result = self._fn()
            self.success.emit(result)
        except LLMError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"执行出错：{exc}")


def _str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _dict_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]
