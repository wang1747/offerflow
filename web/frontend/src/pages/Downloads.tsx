import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import QRCode from "qrcode";
import { useAuthStore } from "../store/authStore";

/** 一个可下载客户端的展示描述（版本、大小等运行时从服务端清单补齐） */
interface ClientInfo {
  key: "desktop" | "android";
  name: string;
  subtitle: string;
  url: string;
  manifestUrl: string;
  minOs: string;
  accent: string;
  glyph: string;
  fallbackNotes: string;
  steps: string[];
  version?: string;
  notes?: string;
  size?: number | null;
}

const CLIENTS: ClientInfo[] = [
  {
    key: "desktop",
    name: "Windows 桌面版",
    subtitle: "Win10 / 11 64 位，正式安装程序",
    url: "/downloads/OfferFlow-Setup.exe",
    manifestUrl: "/downloads/version.json",
    minOs: "Windows 10 / 11（64 位）",
    accent: "from-sky-400 to-blue-600",
    glyph: "🖥️",
    fallbackNotes: "正式安装程序，双击安装向导即可完成安装；功能与网页版一致，数据实时同步。",
    steps: [
      // 体积不写死：上方标签会用 HEAD 取 Content-Length 动态显示，写死会与标签数字打架
      "下载安装程序 OfferFlow-Setup.exe",
      "双击运行，按中文向导选择安装目录",
      "完成后从桌面或开始菜单启动，可在系统设置中卸载",
    ],
  },
  {
    key: "android",
    name: "Android 手机版",
    subtitle: "Android 7.0 以上，扫码或直接下载",
    url: "/downloads/OfferFlow-android.apk",
    manifestUrl: "/downloads/android.json",
    minOs: "Android 7.0（Nougat）及以上",
    accent: "from-emerald-400 to-teal-600",
    glyph: "📱",
    fallbackNotes: "手机端随时改简历、查投递进度，与网页版数据实时同步。",
    steps: [
      "点击「下载安装包」保存 APK",
      "若提示「未知来源」，允许当前浏览器安装应用",
      "安装完成后登录，数据与网页版完全同步",
    ],
  },
];

function formatSize(bytes?: number | null) {
  if (!bytes || bytes <= 0) return null;
  const mb = bytes / 1048576;
  if (mb < 1) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${mb.toFixed(1)} MB`;
}

export default function Downloads() {
  const { isAuthenticated, user } = useAuthStore();
  const [infos, setInfos] = useState<ClientInfo[]>(CLIENTS);
  const [qr, setQr] = useState<string>("");
  const [isMobile, setIsMobile] = useState(false);

  const backTo = isAuthenticated ? (user?.role === "admin" ? "/admin" : "/") : "/login";

  useEffect(() => {
    let alive = true;

    // 1) 版本 / 更新说明：读服务端清单，读不到就退回内置文案，不影响下载
    (async () => {
      const next = await Promise.all(
        CLIENTS.map(async (c) => {
          try {
            const res = await fetch(c.manifestUrl, { cache: "no-store" });
            if (!res.ok) return c;
            const data = await res.json();
            return {
              ...c,
              version: typeof data.version === "string" ? data.version : data.versionName,
              notes: typeof data.notes === "string" ? data.notes : undefined,
            };
          } catch {
            return c;
          }
        }),
      );
      // 注意：这里必须「按 key 合并」而不是 setInfos(next) 整体替换。
      // 体积 effect 实测总是先落地（HEAD 比清单 JSON 先回），整体替换会把
      // 刚补上的 size 抹掉，导致体积徽章永远不显示。
      if (alive)
        setInfos((prev) =>
          next.map((c) => {
            const old = prev.find((p) => p.key === c.key);
            return { ...old, ...c };
          }),
        );
    })();

    // 2) 安装包体积：用 HEAD 取 Content-Length，避免在页面里写死数字
    (async () => {
      const sizes = await Promise.all(
        CLIENTS.map(async (c) => {
          try {
            const res = await fetch(c.url, { method: "HEAD", cache: "no-store" });
            const len = Number(res.headers.get("content-length") || 0);
            return len > 0 ? len : null;
          } catch {
            return null;
          }
        }),
      );
      if (!alive) return;
      setInfos((prev) =>
        prev.map((c, i) => ({ ...c, size: sizes[i] ?? c.size ?? null })),
      );
    })();

    // 3) 二维码：指向本页，扫码方在手机上打开后一键下载
    (async () => {
      try {
        const url = `${window.location.origin}/download`;
        const dataUrl = await QRCode.toDataURL(url, {
          width: 320,
          margin: 1,
          color: { dark: "#0f172a", light: "#ffffff" },
        });
        if (alive) setQr(dataUrl);
      } catch {
        /* 二维码生成失败不影响下载 */
      }
    })();

    const ua = navigator.userAgent || "";
    setIsMobile(/Android|iPhone|iPad|iPod|Mobile/i.test(ua));

    return () => {
      alive = false;
    };
  }, []);

  const ordered = useMemo(
    () => (isMobile ? [...infos].reverse() : infos),
    [infos, isMobile],
  );

  return (
    <div className="min-h-screen bg-slate-50">
      {/* 顶栏 */}
      <header className="border-b border-slate-200 bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-5 py-4">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-brand-400 to-brand-600 text-sm font-black text-white shadow-sm shadow-brand-600/30">
            OF
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-bold text-slate-900">OfferFlow</p>
            <p className="text-[11px] text-slate-500">求职智能助手 · 客户端下载</p>
          </div>
          <Link
            to={backTo}
            className="rounded-lg px-3 py-2 text-xs font-medium text-slate-600 transition hover:bg-slate-100 hover:text-slate-900"
          >
            {isAuthenticated ? "返回应用" : "登录 / 注册"}
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-5 pb-16 pt-10">
        <h1 className="text-2xl font-bold text-slate-900 sm:text-3xl">下载 OfferFlow</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-slate-600">
          电脑端与手机端都是网页版的「外壳」，功能与网页版完全一致、数据实时同步。
          网页版一发版，客户端打开就是最新的，无需重复下载。
        </p>

        <div className="mt-8 grid gap-5 lg:grid-cols-2">
          {ordered.map((c) => (
            <section
              key={c.key}
              className={`relative flex flex-col rounded-2xl border bg-white p-6 shadow-sm transition ${
                isMobile && c.key === "android"
                  ? "border-brand-300 ring-2 ring-brand-100"
                  : "border-slate-200"
              }`}
            >
              {isMobile && c.key === "android" && (
                <span className="absolute right-5 top-5 rounded-full bg-brand-50 px-2.5 py-1 text-[10px] font-bold text-brand-600">
                  推荐
                </span>
              )}

              <div className="flex items-start gap-4">
                <div
                  className={`flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-xl bg-gradient-to-br text-xl ${c.accent}`}
                >
                  <span>{c.glyph}</span>
                </div>
                <div className="min-w-0 flex-1">
                  <h2 className="text-base font-bold text-slate-900">{c.name}</h2>
                  <p className="mt-0.5 text-xs text-slate-500">{c.subtitle}</p>
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
                    {c.version && (
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 font-semibold text-slate-600">
                        v{c.version}
                      </span>
                    )}
                    {formatSize(c.size) && (
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">
                        {formatSize(c.size)}
                      </span>
                    )}
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">
                      {c.minOs}
                    </span>
                  </div>
                </div>
              </div>

              <p className="mt-4 text-xs leading-relaxed text-slate-600">
                {c.notes || c.fallbackNotes}
              </p>

              <div className="mt-5 flex flex-wrap items-center gap-3">
                <a
                  href={c.url}
                  download
                  className="inline-flex items-center gap-2 rounded-xl bg-brand-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-brand-700 active:scale-[0.98]"
                >
                  <svg
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <path d="M7 10l5 5 5-5" />
                    <path d="M12 15V3" />
                  </svg>
                  {isMobile && c.key === "android" ? "下载安装包" : "立即下载"}
                </a>
                {c.key === "android" && (
                  <span className="text-[11px] text-slate-400">
                    微信内请点右上角「…」→ 用浏览器打开
                  </span>
                )}
              </div>

              <div className="mt-6 flex-1 rounded-xl bg-slate-50 p-4">
                <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400">
                  安装步骤
                </p>
                <ol className="mt-2 space-y-1.5">
                  {c.steps.map((s, i) => (
                    <li key={i} className="flex gap-2 text-xs leading-relaxed text-slate-600">
                      <span className="mt-px flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full bg-white text-[10px] font-bold text-brand-600 shadow-sm">
                        {i + 1}
                      </span>
                      <span>{s}</span>
                    </li>
                  ))}
                </ol>
              </div>
            </section>
          ))}
        </div>

        {/* 扫码区 + 网页版说明 */}
        <div className="mt-6 grid gap-5 lg:grid-cols-3">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 lg:col-span-2">
            <h3 className="text-sm font-bold text-slate-900">不装客户端也能用</h3>
            <p className="mt-2 text-xs leading-relaxed text-slate-600">
              直接用浏览器访问即可，功能没有任何删减，登录后数据与客户端完全一致。
              手机浏览器还可以把页面「添加到主屏幕」，得到接近 App 的体验。
            </p>
            <div className="mt-4 flex flex-wrap gap-3">
              <a
                href="/"
                className="rounded-xl border border-slate-200 px-4 py-2 text-xs font-semibold text-slate-700 transition hover:border-brand-300 hover:text-brand-600"
              >
                打开网页版
              </a>
              <span className="rounded-xl bg-slate-50 px-4 py-2 text-xs text-slate-500">
                iPhone / iPad 用户同样使用网页版，在 Safari 中「添加到主屏幕」
              </span>
            </div>
          </div>

          <div className="flex flex-col items-center rounded-2xl border border-slate-200 bg-white p-6">
            <p className="text-sm font-bold text-slate-900">扫码在手机上下载</p>
            <p className="mt-1 text-center text-[11px] text-slate-500">
              手机扫码打开本页，即可一键获取安装包
            </p>
            <div className="mt-4 flex h-[168px] w-[168px] items-center justify-center rounded-xl border border-slate-100 bg-slate-50">
              {qr ? (
                <img src={qr} alt="扫码打开下载页" className="h-40 w-40 rounded-lg" />
              ) : (
                <span className="text-[11px] text-slate-400">二维码生成中…</span>
              )}
            </div>
          </div>
        </div>

        <p className="mt-10 text-center text-[11px] leading-relaxed text-slate-400">
          下载内容均来自本站服务器 · 桌面版 zip 与手机版 APK 的版本、体积信息实时读取自服务端清单
          <br />
          OfferFlow · 求职智能助手
        </p>
      </main>
    </div>
  );
}
