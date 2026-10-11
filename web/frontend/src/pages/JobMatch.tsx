import { useState, type ReactNode } from "react";
import { PageHeader, Tabs, Icons } from "../components/ui";
import JDAnalysis from "./JDAnalysis";
import MatchAnalysis from "./MatchAnalysis";

type JobTab = "parse" | "match";

const TABS: { key: JobTab; label: string; icon: ReactNode }[] = [
  { key: "parse", label: "解析职位", icon: Icons.sparkle },
  { key: "match", label: "匹配度", icon: Icons.match },
];

export default function JobMatch() {
  const [tab, setTab] = useState<JobTab>("parse");

  return (
    <div>
      <PageHeader
        title="岗位分析"
        subtitle="解析岗位要求，再算简历匹配度"
        icon={Icons.sparkle}
      />

      <Tabs tabs={TABS} active={tab} onChange={(k) => setTab(k as JobTab)} />

      {tab === "parse" && <JDAnalysis />}
      {tab === "match" && <MatchAnalysis />}
    </div>
  );
}
