import fs from "node:fs";
import path from "node:path";

import type { PackageInfo } from "./types.js";
import { ANALYZER_METADATA_BYTE_LIMIT } from "./constants.js";
import {
  appendBoundedExpansionResult,
  boundedWildcardSubstitution,
  createBoundedExpansionBudget,
  reserveBoundedTraversal,
  type BoundedExpansionBudget,
} from "./bounded-expansion.js";
import {
  isRecord,
  isSameOrInsideRepo,
  readStableFile,
  type StableFilePermission,
} from "./utils.js";

export function readPackageInfo(
  root: string,
  packageJsonPath: string,
  isPermitted?: StableFilePermission,
  onMetadataLimit?: (fileName: string, fileBytes: number) => void,
  shouldReadMetadata?: (fileName: string, fileBytes: number) => boolean,
  shouldRetainPackage?: (fileName: string) => boolean,
): PackageInfo | null {
  try {
    const resolvedRoot = path.resolve(root);
    const realRoot = fs.realpathSync(resolvedRoot);
    const snapshot = readStableFile(
      packageJsonPath,
      isPermitted ??
        ((resolvedPath, realPath) =>
          isSameOrInsideRepo(resolvedRoot, resolvedPath) &&
          isSameOrInsideRepo(realRoot, realPath)),
      (identity) => {
        if (identity.size > ANALYZER_METADATA_BYTE_LIMIT) {
          onMetadataLimit?.(packageJsonPath, identity.size);
          return false;
        }
        return (
          shouldReadMetadata?.(packageJsonPath, identity.size) ??
          true
        );
      },
    );
    if (snapshot?.text === null || snapshot?.text === undefined) return null;
    const parsed = JSON.parse(snapshot.text) as {
      name?: unknown;
      exports?: unknown;
      main?: unknown;
      module?: unknown;
      tsconfig?: unknown;
      types?: unknown;
      typings?: unknown;
    };
    if (typeof parsed.name !== "string" || parsed.name.length === 0) return null;
    if (shouldRetainPackage && !shouldRetainPackage(packageJsonPath)) {
      return null;
    }
    return {
      root,
      name: parsed.name,
      exports: parsed.exports,
      main: typeof parsed.main === "string" ? parsed.main : null,
      module: typeof parsed.module === "string" ? parsed.module : null,
      tsconfig: typeof parsed.tsconfig === "string" ? parsed.tsconfig : null,
      types: typeof parsed.types === "string" ? parsed.types : null,
      typings: typeof parsed.typings === "string" ? parsed.typings : null,
    };
  } catch {
    return null;
  }
}

export function packageExportTargetsForKey(
  exportsValue: unknown,
  key: string,
  budget: BoundedExpansionBudget =
    createBoundedExpansionBudget(),
): string[] {
  const seenTargets = new Set<string>();
  if (exportsValue === undefined || exportsValue === null) return [];
  if (typeof exportsValue === "string" || Array.isArray(exportsValue)) {
    return key === "."
      ? flattenExportTargets(
          exportsValue,
          null,
          budget,
          seenTargets,
        )
      : [];
  }
  if (!isRecord(exportsValue)) return [];

  if (Object.prototype.hasOwnProperty.call(exportsValue, key)) {
    return flattenExportTargets(
      exportsValue[key],
      null,
      budget,
      seenTargets,
    );
  }

  let hasSubpathKeys = false;
  const matchedTargets: Array<{
    target: unknown;
    wildcard: string;
  }> = [];
  for (const pattern in exportsValue) {
    if (!Object.prototype.hasOwnProperty.call(exportsValue, pattern)) {
      continue;
    }
    if (!reserveBoundedTraversal(budget)) return [];
    if (pattern.startsWith(".")) hasSubpathKeys = true;
    const wildcardValue = matchExportPattern(pattern, key);
    if (wildcardValue === null) continue;
    matchedTargets.push({
      target: exportsValue[pattern],
      wildcard: wildcardValue,
    });
  }
  if (!hasSubpathKeys) {
    return key === "."
      ? flattenExportTargets(
          exportsValue,
          null,
          budget,
          seenTargets,
        )
      : [];
  }

  const matched: string[] = [];
  for (const { target, wildcard } of matchedTargets) {
    matched.push(
      ...flattenExportTargets(
        target,
        wildcard,
        budget,
        seenTargets,
      ),
    );
  }
  return matched;
}

function flattenExportTargets(
  value: unknown,
  wildcardValue: string | null,
  budget: BoundedExpansionBudget,
  seenTargets: Set<string>,
): string[] {
  const preferredKeys = [
    "types",
    "typings",
    "import",
    "default",
    "require",
    "node",
  ];
  const preferredKeySet = new Set(preferredKeys);
  const results: string[] = [];
  const pending: unknown[] = [];
  if (!reserveBoundedTraversal(budget)) return results;
  pending.push(value);

  while (pending.length > 0) {
    const current = pending.pop();
    if (typeof current === "string") {
      if (wildcardValue === null) {
        if (
          !seenTargets.has(current) &&
          appendBoundedExpansionResult(results, current, budget)
        ) {
          seenTargets.add(current);
        }
      } else {
        const expanded = boundedWildcardSubstitution(
          current,
          wildcardValue,
          budget,
          (candidate) => seenTargets.has(candidate),
        );
        if (expanded !== null) {
          seenTargets.add(expanded);
          results.push(expanded);
        }
      }
      continue;
    }
    if (Array.isArray(current)) {
      const children: unknown[] = [];
      for (let index = 0; index < current.length; index += 1) {
        if (!reserveBoundedTraversal(budget)) break;
        if (index in current) children.push(current[index]);
      }
      for (
        let index = children.length - 1;
        index >= 0;
        index -= 1
      ) {
        pending.push(children[index]);
      }
      continue;
    }
    if (!isRecord(current)) continue;

    const children: unknown[] = [];
    for (const key of preferredKeys) {
      if (!Object.prototype.hasOwnProperty.call(current, key)) {
        continue;
      }
      if (!reserveBoundedTraversal(budget)) break;
      children.push(current[key]);
    }
    if (budget.traversedEntries < budget.maxTraversalEntries) {
      for (const key in current) {
        if (
          !Object.prototype.hasOwnProperty.call(current, key) ||
          preferredKeySet.has(key)
        ) {
          continue;
        }
        if (!reserveBoundedTraversal(budget)) break;
        children.push(current[key]);
      }
    }
    for (
      let index = children.length - 1;
      index >= 0;
      index -= 1
    ) {
      pending.push(children[index]);
    }
  }
  return results;
}

function matchExportPattern(pattern: string, key: string): string | null {
  const wildcardIndex = pattern.indexOf("*");
  if (wildcardIndex === -1) return null;

  const prefix = pattern.slice(0, wildcardIndex);
  const suffix = pattern.slice(wildcardIndex + 1);
  if (!key.startsWith(prefix) || !key.endsWith(suffix)) return null;
  return key.slice(prefix.length, key.length - suffix.length);
}
