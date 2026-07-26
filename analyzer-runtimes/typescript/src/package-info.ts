import fs from "node:fs";
import path from "node:path";

import type { PackageInfo } from "./types.js";
import {
  isRecord,
  isSameOrInsideRepo,
  readStableUtf8File,
  type StableFilePermission,
} from "./utils.js";

export function readPackageInfo(
  root: string,
  packageJsonPath: string,
  isPermitted?: StableFilePermission,
): PackageInfo | null {
  try {
    const resolvedRoot = path.resolve(root);
    const realRoot = fs.realpathSync(resolvedRoot);
    const snapshot = readStableUtf8File(
      packageJsonPath,
      isPermitted ??
        ((resolvedPath, realPath) =>
          isSameOrInsideRepo(resolvedRoot, resolvedPath) &&
          isSameOrInsideRepo(realRoot, realPath)),
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
): string[] {
  if (exportsValue === undefined || exportsValue === null) return [];
  if (typeof exportsValue === "string" || Array.isArray(exportsValue)) {
    return key === "." ? flattenExportTargets(exportsValue, null) : [];
  }
  if (!isRecord(exportsValue)) return [];

  const keys = Object.keys(exportsValue);
  const hasSubpathKeys = keys.some((candidate) => candidate.startsWith("."));
  if (!hasSubpathKeys) {
    return key === "." ? flattenExportTargets(exportsValue, null) : [];
  }

  const exactTarget = exportsValue[key];
  if (exactTarget !== undefined) {
    return flattenExportTargets(exactTarget, null);
  }

  const matched: string[] = [];
  for (const [pattern, target] of Object.entries(exportsValue)) {
    const wildcardValue = matchExportPattern(pattern, key);
    if (wildcardValue === null) continue;
    matched.push(...flattenExportTargets(target, wildcardValue));
  }
  return matched;
}

function flattenExportTargets(
  value: unknown,
  wildcardValue: string | null,
): string[] {
  if (typeof value === "string") {
    return [
      wildcardValue === null
        ? value
        : value.replaceAll("*", wildcardValue),
    ];
  }
  if (Array.isArray(value)) {
    return value.flatMap((item) =>
      flattenExportTargets(item, wildcardValue)
    );
  }
  if (!isRecord(value)) return [];

  const preferredKeys = [
    "types",
    "typings",
    "import",
    "default",
    "require",
    "node",
  ];
  const keys = [
    ...preferredKeys.filter((key) =>
      Object.prototype.hasOwnProperty.call(value, key)
    ),
    ...Object.keys(value).filter((key) => !preferredKeys.includes(key)),
  ];
  return keys.flatMap((key) =>
    flattenExportTargets(value[key], wildcardValue)
  );
}

function matchExportPattern(pattern: string, key: string): string | null {
  const wildcardIndex = pattern.indexOf("*");
  if (wildcardIndex === -1) return null;

  const prefix = pattern.slice(0, wildcardIndex);
  const suffix = pattern.slice(wildcardIndex + 1);
  if (!key.startsWith(prefix) || !key.endsWith(suffix)) return null;
  return key.slice(prefix.length, key.length - suffix.length);
}
