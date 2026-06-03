import type { Reference, ReferenceKind } from "../types.js";
import { isTestPath } from "../test-discovery.js";

export function mergeReferences(references: Reference[], limit: number): Reference[] {
  const merged: Reference[] = [];
  const seen = new Set<string>();
  const prioritized = [...references].sort((left, right) => referencePriority(left) - referencePriority(right));
  for (const reference of prioritized) {
    addReference(merged, seen, reference, limit);
    if (merged.length >= limit) break;
  }
  return merged;
}

export function addReference(refs: Reference[], seen: Set<string>, reference: Reference, limit: number): void {
  if (refs.length >= limit) return;
  const key = referenceIdentity(reference);
  if (seen.has(key)) return;
  seen.add(key);
  refs.push(reference);
}

function referenceIdentity(reference: Reference): string {
  return `${reference.file}:${reference.line}:${reference.kind}:${reference.text}`;
}

function referencePriority(reference: Reference): number {
  const testPenalty = isTestPath(reference.file.toLowerCase()) ? 2 : 0;
  return referenceKindPriority(reference.kind) + testPenalty;
}

function referenceKindPriority(kind: ReferenceKind): number {
  if (kind === "call") return 0;
  if (kind === "callee") return 0;
  if (kind === "contract") return 0;
  if (kind === "metadata") return 0;
  if (kind === "write" || kind === "read") return 1;
  if (kind === "type") return 2;
  if (kind === "import") return 5;
  return 3;
}
