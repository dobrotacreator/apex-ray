import fs from "node:fs";
import path from "node:path";

import ts from "typescript";

import { readPackageInfo } from "./package-info.js";
import { findNearestConfig, normalizeTsConfigExtends } from "./program.js";
import type { PackageInfo, RepoIndex, TsConfigPathAliases } from "./types.js";
import { isRecord, isSameOrInsideRepo, normalizeRelPath, uniquePaths } from "./utils.js";

const pathAliasCache = new Map<string, TsConfigPathAliases | null>();

export function isModuleSpecifierRelatedToPath(
  specifier: string,
  importerPath: string,
  targetPath: string,
  targetPackage: PackageInfo | null,
): boolean {
  const normalizedTargetPath = normalizeRelPath(path.resolve(targetPath));
  if (specifier.startsWith(".")) {
    return importCandidatePaths(importerPath, specifier).some((candidate) => candidate === normalizedTargetPath);
  }

  if (!targetPackage) return false;
  if (specifier === targetPackage.name) {
    return packageRootCandidatePaths(targetPackage).some((candidate) => candidate === normalizedTargetPath);
  }
  if (specifier.startsWith(`${targetPackage.name}/`)) {
    const subpath = specifier.slice(targetPackage.name.length + 1);
    return [
      ...packageExportCandidatePaths(targetPackage, `./${subpath}`),
      ...packageSubpathCandidatePaths(targetPackage.root, subpath),
    ].some((candidate) => candidate === normalizedTargetPath);
  }
  return false;
}

export function moduleSpecifierCandidatePaths(
  specifier: string,
  importerPath: string,
  repo: string,
  targetPackage: PackageInfo | null,
): string[] {
  if (specifier.startsWith(".")) {
    return importCandidatePaths(importerPath, specifier);
  }

  if (!targetPackage) return tsconfigPathAliasCandidatePaths(repo, specifier, importerPath);
  if (specifier === targetPackage.name) {
    return packageRootCandidatePaths(targetPackage);
  }
  if (specifier.startsWith(`${targetPackage.name}/`)) {
    const subpath = specifier.slice(targetPackage.name.length + 1);
    return [
      ...packageExportCandidatePaths(targetPackage, `./${subpath}`),
      ...packageSubpathCandidatePaths(targetPackage.root, subpath),
    ];
  }
  return tsconfigPathAliasCandidatePaths(repo, specifier, importerPath);
}

export function findIndexedPackageForFile(repo: string, repoIndex: RepoIndex, filePath: string): PackageInfo | null {
  const key = normalizeRelPath(path.resolve(filePath));
  if (!repoIndex.packageByFile.has(key)) {
    repoIndex.packageByFile.set(key, findPackageForFile(repo, filePath));
  }
  return repoIndex.packageByFile.get(key) ?? null;
}

function importCandidatePaths(importerPath: string, specifier: string): string[] {
  return sourceCandidatePaths(path.resolve(path.dirname(importerPath), specifier));
}

function tsconfigPathAliasCandidatePaths(repo: string, specifier: string, importerPath: string): string[] {
  const relImporterPath = normalizeRelPath(path.relative(repo, importerPath));
  const configPath = findNearestConfig(repo, relImporterPath);
  if (!configPath) return [];

  const aliases = readTsConfigPathAliases(repo, configPath);
  if (!aliases) return [];

  const candidates: string[] = [];
  for (const mapping of aliases.mappings) {
    const wildcardValue = matchPathAliasPattern(mapping.pattern, specifier);
    if (wildcardValue === null) continue;
    for (const target of mapping.targets) {
      const resolvedTarget = path.resolve(aliases.basePath, applyPathAliasWildcard(target, wildcardValue));
      candidates.push(...sourceCandidatePaths(resolvedTarget));
    }
  }
  return candidates;
}

function readTsConfigPathAliases(repo: string, configPath: string): TsConfigPathAliases | null {
  const resolvedConfigPath = path.resolve(configPath);
  const cacheKey = `${path.resolve(repo)}\0${resolvedConfigPath}`;
  if (pathAliasCache.has(cacheKey)) {
    return pathAliasCache.get(cacheKey) ?? null;
  }

  const aliases = readTsConfigPathAliasesUncached(repo, resolvedConfigPath);
  pathAliasCache.set(cacheKey, aliases);
  return aliases;
}

function readTsConfigPathAliasesUncached(repo: string, configPath: string): TsConfigPathAliases | null {
  const readResult = ts.readConfigFile(configPath, ts.sys.readFile);
  if (readResult.error) return null;

  const parsed = ts.parseJsonConfigFileContent(
    normalizeTsConfigExtends(repo, configPath, readResult.config),
    ts.sys,
    path.dirname(configPath),
  );
  const paths = parsed.options.paths;
  if (!paths || Object.keys(paths).length === 0) return null;

  const optionsWithPathsBase = parsed.options as ts.CompilerOptions & { pathsBasePath?: string };
  return {
    basePath: parsed.options.baseUrl ?? optionsWithPathsBase.pathsBasePath ?? path.dirname(configPath),
    mappings: Object.entries(paths)
      .filter((entry): entry is [string, string[]] => Array.isArray(entry[1]))
      .map(([pattern, targets]) => ({ pattern, targets })),
  };
}

function matchPathAliasPattern(pattern: string, specifier: string): string | null {
  const wildcardIndex = pattern.indexOf("*");
  if (wildcardIndex === -1) return pattern === specifier ? "" : null;

  const prefix = pattern.slice(0, wildcardIndex);
  const suffix = pattern.slice(wildcardIndex + 1);
  if (!specifier.startsWith(prefix) || !specifier.endsWith(suffix)) return null;
  return specifier.slice(prefix.length, specifier.length - suffix.length);
}

function applyPathAliasWildcard(target: string, wildcardValue: string): string {
  return target.replaceAll("*", wildcardValue);
}

function packageSubpathCandidatePaths(packageRoot: string, subpath: string): string[] {
  return [
    ...sourceCandidatePaths(path.resolve(packageRoot, subpath)),
    ...sourceCandidatePaths(path.resolve(packageRoot, "src", subpath)),
  ];
}

function packageRootCandidatePaths(packageInfo: PackageInfo): string[] {
  return uniquePaths([
    ...packageExportCandidatePaths(packageInfo, "."),
    ...packageEntrypointCandidatePaths(packageInfo),
    ...packageSubpathCandidatePaths(packageInfo.root, ""),
  ]);
}

function packageEntrypointCandidatePaths(packageInfo: PackageInfo): string[] {
  return [
    packageInfo.types,
    packageInfo.typings,
    packageInfo.module,
    packageInfo.main,
  ].flatMap((entrypoint) => (entrypoint ? sourceCandidatePaths(path.resolve(packageInfo.root, entrypoint)) : []));
}

function packageExportCandidatePaths(packageInfo: PackageInfo, key: string): string[] {
  const targets = exportTargetsForKey(packageInfo.exports, key);
  return targets.flatMap((target) => sourceCandidatePaths(path.resolve(packageInfo.root, target)));
}

function exportTargetsForKey(exportsValue: unknown, key: string): string[] {
  if (exportsValue === undefined || exportsValue === null) return [];
  if (typeof exportsValue === "string" || Array.isArray(exportsValue)) {
    return key === "." ? flattenExportTargets(exportsValue, null) : [];
  }
  if (!isRecord(exportsValue)) return [];

  const exactTarget = exportsValue[key];
  if (exactTarget !== undefined) return flattenExportTargets(exactTarget, null);

  const matched: string[] = [];
  for (const [pattern, target] of Object.entries(exportsValue)) {
    const wildcardValue = matchExportPattern(pattern, key);
    if (wildcardValue === null) continue;
    matched.push(...flattenExportTargets(target, wildcardValue));
  }
  return matched;
}

function flattenExportTargets(value: unknown, wildcardValue: string | null): string[] {
  if (typeof value === "string") {
    return [applyExportWildcard(value, wildcardValue)];
  }
  if (Array.isArray(value)) {
    return value.flatMap((item) => flattenExportTargets(item, wildcardValue));
  }
  if (!isRecord(value)) return [];

  const preferredKeys = ["types", "typings", "import", "default", "require", "node"];
  const keys = [
    ...preferredKeys.filter((key) => Object.prototype.hasOwnProperty.call(value, key)),
    ...Object.keys(value).filter((key) => !preferredKeys.includes(key)),
  ];
  return keys.flatMap((key) => flattenExportTargets(value[key], wildcardValue));
}

function matchExportPattern(pattern: string, key: string): string | null {
  const wildcardIndex = pattern.indexOf("*");
  if (wildcardIndex === -1) return null;

  const prefix = pattern.slice(0, wildcardIndex);
  const suffix = pattern.slice(wildcardIndex + 1);
  if (!key.startsWith(prefix) || !key.endsWith(suffix)) return null;
  return key.slice(prefix.length, key.length - suffix.length);
}

function applyExportWildcard(value: string, wildcardValue: string | null): string {
  return wildcardValue === null ? value : value.replaceAll("*", wildcardValue);
}

function sourceCandidatePaths(basePath: string): string[] {
  const candidates = new Set<string>();
  const add = (candidate: string) => candidates.add(normalizeRelPath(path.resolve(candidate)));
  const ext = path.extname(basePath);

  add(basePath);
  if (ext) {
    const withoutExt = basePath.slice(0, -ext.length);
    for (const sourceExt of [".ts", ".tsx", ".js", ".jsx"]) {
      add(`${withoutExt}${sourceExt}`);
    }
  } else {
    for (const sourceExt of [".ts", ".tsx", ".js", ".jsx"]) {
      add(`${basePath}${sourceExt}`);
    }
    for (const sourceExt of [".ts", ".tsx", ".js", ".jsx"]) {
      add(path.join(basePath, `index${sourceExt}`));
    }
  }

  return [...candidates];
}

function findPackageForFile(repo: string, filePath: string): PackageInfo | null {
  const repoRoot = path.resolve(repo);
  let current = path.dirname(path.resolve(filePath));
  while (isSameOrInsideRepo(repoRoot, current)) {
    const packageJsonPath = path.join(current, "package.json");
    if (fs.existsSync(packageJsonPath)) {
      const packageInfo = readPackageInfo(current, packageJsonPath);
      if (packageInfo) return packageInfo;
    }
    if (current === repoRoot) return null;
    const parent = path.dirname(current);
    if (parent === current) return null;
    current = parent;
  }
  return null;
}
