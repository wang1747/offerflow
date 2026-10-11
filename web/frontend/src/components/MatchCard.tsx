import { type FC } from "react";
import type { MatchResult } from "../types";

interface MatchCardProps {
  result: MatchResult;
}

const MatchCard: FC<MatchCardProps> = ({ result }) => {
  const isHigh = result.score >= 80;
  const isMid = result.score >= 60;
  const color = isHigh ? "emerald" : isMid ? "amber" : "rose";
  const ringColor = isHigh ? "from-emerald-400 to-emerald-600" : isMid ? "from-amber-400 to-amber-500" : "from-rose-400 to-rose-500";
  const label = isHigh ? "匹配度较高" : isMid ? "有待提升" : "差距较大";

  return (
    <div className="of-card-hover flex items-center gap-4 p-4">
      <div className={`flex h-14 w-14 flex-shrink-0 items-center justify-center rounded-full bg-gradient-to-br ${ringColor} text-lg font-bold text-white shadow-soft`}>
        {result.score}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="truncate text-sm font-semibold text-slate-900">
            {result.company || `岗位 ${result.jd_id.slice(0, 8)}`}
          </p>
          <span className={`of-badge bg-${color}-50 text-${color}-600`}>{label}</span>
        </div>
        {result.position && (
          <p className="mt-0.5 truncate text-xs text-slate-500">{result.position}</p>
        )}
        {result.suggestion && (
          <p className="mt-1 text-xs leading-relaxed text-slate-400">{result.suggestion}</p>
        )}
      </div>
    </div>
  );
};

export default MatchCard;
