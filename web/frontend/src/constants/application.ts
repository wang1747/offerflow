import type { ApplicationStatus } from "../types";

export const STATUS_LABELS: Record<string, string> = {
  saved: "已收藏",
  applied: "已投递",
  online_test: "笔试",
  first_interview: "一面",
  second_interview: "二面",
  hr_round: "HR 面",
  offered: "Offer",
  accepted: "已接受",
  rejected: "已拒绝",
};

export const STATUS_ORDER: ApplicationStatus[] = [
  "saved",
  "applied",
  "online_test",
  "first_interview",
  "second_interview",
  "hr_round",
  "offered",
  "accepted",
  "rejected",
];
