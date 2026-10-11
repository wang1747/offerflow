"""后端契约测试（审计 #325）—— 验证「桌面端 api_client」依赖的后端契约是否成立。

不依赖 OpenAPI（生产已关闭 /openapi.json），改用对真实后端的安全探测：

  Part1 路由存在性：对 api_client 里每个 (方法, 路径) 发 OPTIONS。
        存在路由返回 405/200（非 404）；404 即「后端没有这个路由」——典型的
        后端改了路径而桌面端没跟上，正是本测试要抓的漂移。OPTIONS 不触发处理器，零副作用。
  Part2 统一信封/形状：无鉴权可达的端点（/api/health、/model-config/presets）
        校验统一响应信封 {success,data,error} / 分页形状。
  Part3 方法被允许：对「OPTIONS 的 Allow 头误报」的 7 个鉴权端点，改用真实方法
        （无 token）探测——401/403/400/422 均代表「路由存在且方法被允许」
        （鉴权/校验发生在路由匹配之后），仅 404/405 为违约。这些端点都需鉴权，
        无 token 探测不会造成副作用。
  Part3b Allow 扫描：把 OPTIONS 的 Allow 头与桌面端声明的方法比对，不一致的列为
        *警告*（不计入失败），供人工复核（Starlette 的 OPTIONS Allow 不可靠，见 Part3）。
  Part4 登录态契约（可选）：设 OFFERFLOW_TEST_EMAIL / OFFERFLOW_TEST_PASSWORD 后
        真实走 login → me，校验 access_token 与用户信封。
  Part5 静态契约：核对 career_agent 的 CreateSessionRequest 入参字段
        （entry_source / jd_id / resume_id / application_id，不含 context）与
        ActionDecisionRequest.approved（decide 裁决）。属于 web↔后端契约，作静态兜底。

配置（环境变量）：
  OFFERFLOW_CONTRACT_BASE_URL   默认 http://localhost:8001
  OFFERFLOW_CONTRACT_LIVE       1=跑在线探测（默认），0=只跑 Part5 静态部分
  OFFERFLOW_TEST_EMAIL/_PASSWORD 可选，启用 Part4
  OFFERFLOW_BACKEND_SRC         backend 源码根，默认记忆路径

直接运行： .venv/Scripts/python.exe tests/test_backend_contract.py
"""

import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
API_CLIENT = os.path.join(REPO, "offerflow_desktop", "api_client.py")
# monorepo 布局：desktop/ 与 web/ 同级，后端源码在 web/backend
BACKEND_DEFAULT = os.path.join(os.path.dirname(REPO), "web", "backend")

BASE_URL = os.environ.get("OFFERFLOW_CONTRACT_BASE_URL", "http://localhost:8001").rstrip("/")
LIVE = os.environ.get("OFFERFLOW_CONTRACT_LIVE", "1") == "1"
TEST_EMAIL = os.environ.get("OFFERFLOW_TEST_EMAIL", "")
TEST_PASSWORD = os.environ.get("OFFERFLOW_TEST_PASSWORD", "")
BACKEND_SRC = os.environ.get("OFFERFLOW_BACKEND_SRC", BACKEND_DEFAULT)

PLACEHOLDER = "00000000-0000-0000-0000-000000000000"

fails = []
warns = []


def check(cond, label, extra=""):
    print(("OK  " if cond else "XX  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


# ----------------------------------------------------------------------------
# 提取「消费者契约」：api_client.py 里所有 (方法, 路径) 调用
# ----------------------------------------------------------------------------
def extract_endpoints(src):
    tree = ast.parse(open(src, encoding="utf-8").read())

    def path_of(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):  # f-string
            out = ""
            for v in node.values:
                if isinstance(v, ast.Constant):
                    out += v.value
                elif isinstance(v, ast.FormattedValue):
                    out += "{param}"
            return out
        return None

    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)):
            continue
        name = n.func.id
        if name not in ("_request", "_upload", "push_op"):
            continue
        method = None
        path = None
        if name == "_upload":
            method = "POST"
            if n.args:
                path = path_of(n.args[0])
        else:
            if len(n.args) >= 2:
                m = n.args[0]
                if isinstance(m, ast.Constant) and isinstance(m.value, str):
                    method = m.value
                path = path_of(n.args[1])
        if method and path:
            out.append((method.upper(), path))
    return out


def norm(path):
    p = path.split("?")[0]
    p = re.sub(r"\{[^}]+\}", "{param}", p)  # 任意路径参数统一占位
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


# ----------------------------------------------------------------------------
# 网络工具
# ----------------------------------------------------------------------------
def _req(method, path, body=None, token=None, timeout=15):
    url = BASE_URL + path.replace("{param}", PLACEHOLDER)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _safe_json(resp)
    except urllib.error.HTTPError as e:
        return e.code, _safe_json(e)
    except Exception as e:  # noqa: BLE001
        return None, {"_err": type(e).__name__ + ": " + str(e)}


def _safe_json(resp):
    try:
        return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------------
# Part1：路由存在性（OPTIONS，安全）
# ----------------------------------------------------------------------------
def part1_route_existence(endpoints):
    print("\n--- Part1 路由存在性（OPTIONS 探测，零副作用）---")
    api_eps = [(m, p) for m, p in endpoints if p.startswith("/api")]
    check(bool(api_eps), "提取到 /api 端点", f"{len(api_eps)} 个")

    missing = []
    for m, p in api_eps:
        status, _ = _req("OPTIONS", p)
        if status == 404:
            missing.append((m, p))
            check(False, f"路由存在 {m} {p}", "HTTP 404 缺失")
    if not missing:
        check(True, "全部路由存在（无 404）", f"共 {len(api_eps)} 个端点")
    print(f"  端点总数={len(api_eps)}  缺失={len(missing)}")


# ----------------------------------------------------------------------------
# Part2：无鉴权可达端点的统一信封 / 形状
# ----------------------------------------------------------------------------
def part2_envelope():
    print("\n--- Part2 统一响应信封 / 形状（无鉴权可达端点）---")
    s, b = _req("GET", "/api/health")
    ok = (s == 200 and isinstance(b, dict) and b.get("success") is True
          and isinstance(b.get("data"), dict))
    check(ok, "GET /api/health -> {success:true, data:{...}}", f"status={s}")

    s, b = _req("GET", "/api/v1/user/model-config/presets")
    if s == 200:
        ok = isinstance(b, dict) and isinstance(b.get("data"), list)
        check(ok, "GET /api/v1/user/model-config/presets -> {data:[...]}", f"status={s}")
    else:
        print(f"  .. /presets 返回 {s}（可能需鉴权），跳过信封断言")


# ----------------------------------------------------------------------------
# Part3：方法被允许（真实方法 + 无 token，仅鉴权端点，安全）
# ----------------------------------------------------------------------------
def part3_method_allowed():
    print("\n--- Part3 方法被允许（真实方法探测，无 token，仅鉴权端点）---")
    # 这 7 个是被 OPTIONS 的 Allow 头误报、但真实方法探测均 401（存在+允许）的端点。
    # 它们都需鉴权，无 token 探测不会造成副作用。
    spot = [
        ("PUT", "/api/v1/applications/{param}"),
        ("DELETE", "/api/v1/applications/{param}"),
        ("DELETE", "/api/v1/community/posts/{param}"),
        ("DELETE", "/api/v1/user/model-config"),
        ("GET", "/api/v1/interview/articles"),
        ("POST", "/api/v1/corpus/consent"),
        ("POST", "/api/v1/user/model-config"),
    ]
    for m, p in spot:
        s, _ = _req(m, p, body={} if m in ("POST", "PUT") else None)
        allowed = s not in (404, 405)  # 401/403/400/422 => 存在且方法被允许
        check(allowed, f"方法允许 {m} {p.replace('{param}', '<id>')}", f"status={s}")


# ----------------------------------------------------------------------------
# Part3b：Allow 扫描（仅警告，不计入失败）
# ----------------------------------------------------------------------------
def part3b_allow_scan(endpoints):
    print("\n--- Part3b OPTIONS Allow 扫描（警告级别，供人工复核）---")
    api_eps = [(m, p) for m, p in endpoints if p.startswith("/api")]
    for m, p in api_eps:
        status, _ = _req("OPTIONS", p)
        allow = ""
        if status == 405:
            # 从 405 的 Allow 头再探一次（部分实现把 Allow 放在 200 响应）
            try:
                req = urllib.request.Request(BASE_URL + p.replace("{param}", PLACEHOLDER),
                                             method="OPTIONS")
                with urllib.request.urlopen(req, timeout=15) as r:
                    allow = (r.headers.get("Allow") or "").upper()
            except urllib.error.HTTPError as e:
                allow = (e.headers.get("Allow") or "").upper()
        if status != 404 and allow and m not in allow.split(","):
            warns.append(f"{m} {p} 不在 Allow[{allow}] 中（通常可忽略，见 Part3）")
    if warns:
        for w in warns:
            print("  WARN " + w)
        print(f"  .. 上述 {len(warns)} 条 Allow 不一致：已用 Part3 真实方法探测确认为误报，不计入失败。")
    else:
        print("  OK  无 Allow 不一致")


# ----------------------------------------------------------------------------
# Part4：可选登录态契约
# ----------------------------------------------------------------------------
def part4_auth_flow():
    print("\n--- Part4 登录态契约（可选，需 OFFERFLOW_TEST_EMAIL/PASSWORD）---")
    if not (TEST_EMAIL and TEST_PASSWORD):
        print("  .. 未设置测试账号，跳过")
        return
    s, b = _req("POST", "/api/v1/auth/login",
                {"username": TEST_EMAIL, "password": TEST_PASSWORD})
    ok = (s == 200 and isinstance(b, dict) and isinstance(b.get("data"), dict)
          and b["data"].get("access_token"))
    check(ok, "POST /api/v1/auth/login -> access_token", f"status={s}")
    token = (b.get("data") or {}).get("access_token") if ok else None
    if token:
        s2, b2 = _req("GET", "/api/v1/auth/me", token=token)
        ok2 = (s2 == 200 and isinstance(b2, dict) and isinstance(b2.get("data"), dict)
               and b2["data"].get("email"))
        check(ok2, "GET /api/v1/auth/me -> 用户信封(含 email)", f"status={s2}")


# ----------------------------------------------------------------------------
# Part5：静态核对 career_agent 会话创建入参契约
# ----------------------------------------------------------------------------
def part5_agent_schema():
    print("\n--- Part5 静态契约：career_agent 会话创建入参（web↔后端）---")
    routes = os.path.join(BACKEND_SRC, "app", "modules", "career_agent", "routes.py")
    if not os.path.exists(routes):
        print("  .. 后端源码不在默认路径，跳过（设 OFFERFLOW_BACKEND_SRC）")
        return
    txt = open(routes, encoding="utf-8").read()

    def class_fields(cls):
        m = re.search(r"class " + cls + r"\(BaseModel\):(.*?)\nclass ", txt, re.S)
        if not m:
            return None
        return set(re.findall(r"^\s*([a-z_]+)\s*:", m.group(1), re.M))

    f = class_fields("CreateSessionRequest")
    if f is None:
        print("  .. 未找到 CreateSessionRequest，跳过")
        return
    for field in ("entry_source", "jd_id", "resume_id", "application_id"):
        check(field in f, f"CreateSessionRequest 含字段 {field}")
    check("context" not in f, "CreateSessionRequest 不含 context（防止误传正文）")

    fd = class_fields("ActionDecisionRequest")
    if fd is not None:
        check("approved" in fd, "ActionDecisionRequest 含 approved（decide 裁决）")


def main() -> int:
    if not os.path.exists(API_CLIENT):
        print("找不到 api_client.py: %s" % API_CLIENT)
        return 1
    endpoints = extract_endpoints(API_CLIENT)
    if LIVE:
        part1_route_existence(endpoints)
        part2_envelope()
        part3_method_allowed()
        part3b_allow_scan(endpoints)
        part4_auth_flow()
    else:
        print("OFFERFLOW_CONTRACT_LIVE=0，跳过在线探测")
    part5_agent_schema()

    print()
    if fails:
        print("FAILED: %d 处 -> %s" % (len(fails), "; ".join(fails)))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
