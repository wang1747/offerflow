import { type FC, useEffect, useState } from "react";
import { flushQueue, onSynced, pendingLabels } from "../offline";
import { useOffline } from "../offline/useOffline";
import { Icons } from "./ui";

function fmtTime(ts: number | null): string {
  if (!ts) return "刚刚";
  const d = new Date(ts);
  const p = (n: number): string => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * 离线状态条。
 *
 * 只在"离线中 / 有积压 / 同步出错 / 刚同步完成"这几种情况下出现，其余时候完全不占位，
 * 免得平时也顶一条横幅影响观感。
 */
const OfflineBanner: FC = () => {
  const { online, pending, syncing, lastError, staleAt } = useOffline();
  const [labels, setLabels] = useState<string[]>([]);
  const [justSynced, setJustSynced] = useState(false);

  useEffect(() => {
    let alive = true;
    if (pending > 0) {
      void pendingLabels().then((list) => {
        if (alive) setLabels(Array.from(new Set(list)));
      });
    } else {
      setLabels([]);
    }
    return () => {
      alive = false;
    };
  }, [pending]);

  useEffect(() => {
    return onSynced(() => {
      setJustSynced(true);
    });
  }, []);

  useEffect(() => {
    if (!justSynced) return;
    const timer = window.setTimeout(() => setJustSynced(false), 4000);
    return () => window.clearTimeout(timer);
  }, [justSynced]);

  if (online && pending === 0 && !lastError && !justSynced) return null;

  const syncButton = pending > 0 && online && (
    <button
      type="button"
      onClick={() => void flushQueue()}
      disabled={syncing}
      className="ml-auto shrink-0 rounded-lg border border-current px-2.5 py-1 text-xs font-medium disabled:opacity-50"
    >
      {syncing ? "同步中…" : "立即同步"}
    </button>
  );

  let tone = "";
  let text = "";
  let ico = Icons.alert;

  if (!online) {
    tone = "border-amber-200 bg-amber-50 text-amber-800";
    text = `离线中 · 看到的是 ${fmtTime(staleAt)} 的本地副本，仍可查看和修改`;
    ico = Icons.alert;
  } else if (pending > 0) {
    tone = "border-sky-200 bg-sky-50 text-sky-800";
    const detail = labels.length > 0 ? `（${labels.join("、")}）` : "";
    text = `待同步 ${pending} 条${detail}`;
    ico = Icons.upload;
  } else if (syncing) {
    tone = "border-sky-200 bg-sky-50 text-sky-800";
    text = "正在同步…";
    ico = Icons.upload;
  } else if (lastError) {
    tone = "border-rose-200 bg-rose-50 text-rose-700";
    text = lastError;
    ico = Icons.alert;
  } else if (justSynced) {
    tone = "border-emerald-200 bg-emerald-50 text-emerald-700";
    text = "已同步完成";
    ico = Icons.check;
  }

  return (
    <div
      className={`mb-4 flex items-center gap-2.5 rounded-xl border px-4 py-2.5 text-sm ${tone}`}
      role="status"
    >
      <span className="shrink-0 opacity-70">{ico}</span>
      <span className="min-w-0 flex-1">{text}</span>
      {syncButton}
    </div>
  );
};

export default OfflineBanner;
