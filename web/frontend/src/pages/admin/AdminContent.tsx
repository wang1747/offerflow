import { useState } from "react";
import { PageHeader, Icons } from "../../components/ui";
import FeedbackManage from "./FeedbackManage";
import CommunityManage from "./CommunityManage";

export default function AdminContent() {
  const [tab, setTab] = useState<"community" | "feedback">("community");

  return (
    <div>
      <PageHeader title="内容治理" subtitle="社区内容与建议反馈" icon={Icons.interview} />

      <div className="mb-4 flex gap-1 rounded-xl bg-slate-100 p-1 sm:w-fit">
        {(
          [
            { key: "community", label: "社区内容" },
            { key: "feedback", label: "建议反馈" },
          ] as const
        ).map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex-shrink-0 rounded-lg px-4 py-1.5 text-sm font-medium transition ${
              tab === t.key
                ? "bg-white text-brand-600 shadow-sm"
                : "text-slate-500 hover:text-slate-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "community" ? <CommunityManage /> : <FeedbackManage />}
    </div>
  );
}
