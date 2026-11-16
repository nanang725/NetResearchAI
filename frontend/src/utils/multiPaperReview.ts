import type {
  ReviewComparisonFinding,
  ReviewDimensionComparison,
} from "@/types/citation";


export type ReviewClaimKind = ReviewComparisonFinding["claim_kind"];


export function claimKindLabel(kind: ReviewClaimKind): string {
  return {
    evidence: "原文证据支持",
    inference: "模型推断",
    insufficient_evidence: "证据不足",
  }[kind];
}


export function claimKindType(
  kind: ReviewClaimKind,
): "success" | "warning" | "info" {
  const types = {
    evidence: "success",
    inference: "warning",
    insufficient_evidence: "info",
  } as const;
  return types[kind];
}


export function comparisonCitationCount(
  dimension: ReviewDimensionComparison,
): number {
  return dimension.items.reduce(
    (total, item) => total + item.citations.length,
    0,
  );
}
