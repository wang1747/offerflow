import { type ReactNode, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Icons } from "./ui";

type GateState = "loading" | "missing" | "ready";

export default function ModelConfigGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>("loading");

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(async () => {
      try {
        const res = await api.modelConfig.get();
        if (!active) return;
        setState(res.success && (res.data?.has_config || res.data?.using_default) ? "ready" : "missing");
      } catch {
        if (active) setState("missing");
      }
    }, 0);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, []);

  if (state === "loading") {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="flex items-center gap-3 text-slate-400">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-slate-200 border-t-brand-500" />
          <span className="text-sm">检查模型配置...</span>
        </div>
      </div>
    );
  }

  if (state === "missing") {
    return (
      <div className="mx-auto max-w-lg py-12">
        <div className="of-card p-8 text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-50 text-amber-500 [&>svg]:h-full [&>svg]:w-full">
            {Icons.alert}
          </div>
          <h2 className="text-lg font-bold text-slate-900">请先完成模型设置</h2>
          <p className="mt-2 text-sm text-slate-500">
            该功能需要先配置你自己的 AI 密钥（安全加密存储，不会泄露）。
          </p>
          <Link to="/preferences?tab=model" className="of-btn-primary mt-6 inline-flex">
            前往设置
          </Link>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
