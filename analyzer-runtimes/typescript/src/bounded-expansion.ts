const DEFAULT_EXPANSION_RESULT_LIMIT = 512;
const DEFAULT_EXPANSION_RESULT_BYTE_LIMIT = 64 * 1024;
const DEFAULT_EXPANSION_TOTAL_BYTE_LIMIT = 4 * 1024 * 1024;
const DEFAULT_EXPANSION_TRAVERSAL_LIMIT = 4_096;

export interface BoundedExpansionBudget {
  maxResults: number;
  maxResultBytes: number;
  maxTotalBytes: number;
  maxTraversalEntries: number;
  retainedResults: number;
  retainedBytes: number;
  traversedEntries: number;
  limited: boolean;
}

export interface BoundedExpansionLimits {
  maxResults?: number;
  maxResultBytes?: number;
  maxTotalBytes?: number;
  maxTraversalEntries?: number;
}

export function createBoundedExpansionBudget(
  limits: BoundedExpansionLimits = {},
): BoundedExpansionBudget {
  return {
    maxResults:
      limits.maxResults ?? DEFAULT_EXPANSION_RESULT_LIMIT,
    maxResultBytes:
      limits.maxResultBytes ??
      DEFAULT_EXPANSION_RESULT_BYTE_LIMIT,
    maxTotalBytes:
      limits.maxTotalBytes ??
      DEFAULT_EXPANSION_TOTAL_BYTE_LIMIT,
    maxTraversalEntries:
      limits.maxTraversalEntries ??
      DEFAULT_EXPANSION_TRAVERSAL_LIMIT,
    retainedResults: 0,
    retainedBytes: 0,
    traversedEntries: 0,
    limited: false,
  };
}

export function reserveBoundedTraversal(
  budget: BoundedExpansionBudget,
  count = 1,
): boolean {
  if (
    count < 0 ||
    !Number.isSafeInteger(count) ||
    count >
      budget.maxTraversalEntries - budget.traversedEntries
  ) {
    budget.limited = true;
    return false;
  }
  budget.traversedEntries += count;
  return true;
}

export function appendBoundedExpansionResult(
  results: string[],
  value: string,
  budget: BoundedExpansionBudget,
): boolean {
  const retained = retainBoundedExpansionValue(value, budget);
  if (retained === null) return false;
  results.push(retained);
  return true;
}

export function retainBoundedExpansionValue(
  value: string,
  budget: BoundedExpansionBudget,
): string | null {
  const valueBytes = Buffer.byteLength(value);
  return reserveExpansionResult(budget, valueBytes)
    ? value
    : null;
}

export function boundedWildcardSubstitution(
  target: string,
  wildcard: string,
  budget: BoundedExpansionBudget,
  isDuplicate?: (candidate: string) => boolean,
): string | null {
  const targetBytes = Buffer.byteLength(target);
  const wildcardCount = countWildcardCharacters(target);
  const staticBytes = targetBytes - wildcardCount;
  if (staticBytes > budget.maxResultBytes) {
    budget.limited = true;
    return null;
  }

  const wildcardBytes = Buffer.byteLength(wildcard);
  if (
    wildcardCount > 0 &&
    wildcardBytes >
      Math.floor(
        (budget.maxResultBytes - staticBytes) / wildcardCount,
      )
  ) {
    budget.limited = true;
    return null;
  }
  const resultBytes =
    staticBytes + wildcardCount * wildcardBytes;
  let result: string | null = null;
  if (isDuplicate) {
    result =
      wildcardCount === 0
        ? target
        : target.replaceAll("*", wildcard);
    if (isDuplicate(result)) return null;
  }
  if (!reserveExpansionResult(budget, resultBytes)) return null;

  return (
    result ??
    (wildcardCount === 0
      ? target
      : target.replaceAll("*", wildcard))
  );
}

function reserveExpansionResult(
  budget: BoundedExpansionBudget,
  resultBytes: number,
): boolean {
  if (
    resultBytes < 0 ||
    !Number.isSafeInteger(resultBytes) ||
    budget.retainedResults >= budget.maxResults ||
    resultBytes > budget.maxResultBytes ||
    resultBytes > budget.maxTotalBytes - budget.retainedBytes
  ) {
    budget.limited = true;
    return false;
  }
  budget.retainedResults += 1;
  budget.retainedBytes += resultBytes;
  return true;
}

function countWildcardCharacters(value: string): number {
  let count = 0;
  let offset = value.indexOf("*");
  while (offset !== -1) {
    count += 1;
    offset = value.indexOf("*", offset + 1);
  }
  return count;
}
