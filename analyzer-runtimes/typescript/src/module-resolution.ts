import fs from "node:fs";
import path from "node:path";

import ts from "typescript";

import {
  packageExportTargetsForKey,
  readPackageInfo,
} from "./package-info.js";
import {
  findNearestConfig,
  normalizeTsConfigExtends,
  normalizeTsConfigFileText,
} from "./program.js";
import type { PackageInfo, RepoIndex, TsConfigPathAliases } from "./types.js";
import {
  ANALYZER_METADATA_BYTE_LIMIT,
} from "./constants.js";
import {
  appendBoundedExpansionResult,
  boundedWildcardSubstitution,
  createBoundedExpansionBudget,
  reserveBoundedTraversal,
  retainBoundedExpansionValue,
  type BoundedExpansionBudget,
} from "./bounded-expansion.js";
import {
  canonicalPathKey,
  isInsideRepo,
  isSameOrInsideRepo,
  normalizeRelPath,
  readStableFile,
  uniquePaths,
} from "./utils.js";

interface PathAliasCacheEntry {
  aliases: TsConfigPathAliases | null;
  retainedBytes: number;
}

const PATH_ALIAS_CACHE_ENTRY_LIMIT = 128;
const PATH_ALIAS_CACHE_BYTE_LIMIT = 4 * 1024 * 1024;
const PATH_ALIAS_MAPPING_LIMIT = 512;
const PATH_ALIAS_TARGET_LIMIT = 512;
const pathAliasCache = new Map<string, PathAliasCacheEntry>();
let pathAliasCacheRetainedBytes = 0;
// TypeScript substitutes extensions within these families during module
// resolution; mixing the ESM and CommonJS families creates false references.
const STANDARD_SOURCE_EXTENSIONS = [".ts", ".tsx", ".d.ts", ".js", ".jsx"];
const ESM_SOURCE_EXTENSIONS = [".mts", ".d.mts", ".mjs"];
const COMMONJS_SOURCE_EXTENSIONS = [".cts", ".d.cts", ".cjs"];
const EXTENSIONLESS_SOURCE_EXTENSIONS = [
  ...STANDARD_SOURCE_EXTENSIONS,
  ...ESM_SOURCE_EXTENSIONS,
  ...COMMONJS_SOURCE_EXTENSIONS,
];
const SOURCE_EXTENSION_SUBSTITUTIONS = new Map<string, string[]>([
  [".js", STANDARD_SOURCE_EXTENSIONS],
  [".jsx", STANDARD_SOURCE_EXTENSIONS],
  [".ts", STANDARD_SOURCE_EXTENSIONS],
  [".tsx", STANDARD_SOURCE_EXTENSIONS],
  [".mjs", ESM_SOURCE_EXTENSIONS],
  [".mts", ESM_SOURCE_EXTENSIONS],
  [".cjs", COMMONJS_SOURCE_EXTENSIONS],
  [".cts", COMMONJS_SOURCE_EXTENSIONS],
]);

export function isModuleSpecifierRelatedToPath(
  specifier: string,
  importerPath: string,
  targetPath: string,
  targetPackage: PackageInfo | null,
  onExpansionLimit?: () => void,
): boolean {
  const normalizedTargetPath = normalizeRelPath(path.resolve(targetPath));
  const targetBudget = createBoundedExpansionBudget();
  const candidateBudget = createBoundedExpansionBudget();
  let candidates: string[];
  if (specifier.startsWith(".")) {
    candidates = importCandidatePaths(
      importerPath,
      specifier,
      candidateBudget,
    );
  } else if (!targetPackage) {
    return false;
  } else if (specifier === targetPackage.name) {
    candidates = packageRootCandidatePaths(
      targetPackage,
      targetBudget,
      candidateBudget,
    );
  } else if (specifier.startsWith(`${targetPackage.name}/`)) {
    const subpath = specifier.slice(targetPackage.name.length + 1);
    candidates = [
      ...packageExportCandidatePaths(
        targetPackage,
        `./${subpath}`,
        targetBudget,
        candidateBudget,
      ),
      ...packageSubpathCandidatePaths(
        targetPackage.root,
        subpath,
        candidateBudget,
      ),
    ];
  } else {
    return false;
  }
  if (targetBudget.limited || candidateBudget.limited) {
    onExpansionLimit?.();
  }
  return candidates.some(
    (candidate) => candidate === normalizedTargetPath,
  );
}

export function moduleSpecifierCandidatePaths(
  specifier: string,
  importerPath: string,
  repo: string,
  targetPackage: PackageInfo | null,
  onExpansionLimit?: () => void,
): string[] {
  const targetBudget = createBoundedExpansionBudget();
  const candidateBudget = createBoundedExpansionBudget();
  let expansionLimitReported = false;
  const reportExpansionLimit = (): void => {
    if (expansionLimitReported) return;
    expansionLimitReported = true;
    onExpansionLimit?.();
  };
  let candidates: string[];
  if (specifier.startsWith(".")) {
    candidates = importCandidatePaths(
      importerPath,
      specifier,
      candidateBudget,
    );
  } else if (!targetPackage) {
    candidates = tsconfigPathAliasCandidatePaths(
      repo,
      specifier,
      importerPath,
      targetBudget,
      candidateBudget,
    );
  } else if (specifier === targetPackage.name) {
    candidates = packageRootCandidatePaths(
      targetPackage,
      targetBudget,
      candidateBudget,
    );
  } else if (specifier.startsWith(`${targetPackage.name}/`)) {
    const subpath = specifier.slice(targetPackage.name.length + 1);
    candidates = [
      ...packageExportCandidatePaths(
        targetPackage,
        `./${subpath}`,
        targetBudget,
        candidateBudget,
      ),
      ...packageSubpathCandidatePaths(
        targetPackage.root,
        subpath,
        candidateBudget,
      ),
    ];
  } else {
    candidates = tsconfigPathAliasCandidatePaths(
      repo,
      specifier,
      importerPath,
      targetBudget,
      candidateBudget,
    );
  }
  if (targetBudget.limited || candidateBudget.limited) {
    reportExpansionLimit();
  }
  return candidates;
}

export function findIndexedPackageForFile(repo: string, repoIndex: RepoIndex, filePath: string): PackageInfo | null {
  const key = normalizeRelPath(path.resolve(filePath));
  if (!repoIndex.packageByFile.has(key)) {
    repoIndex.packageByFile.set(key, findPackageForFile(repo, filePath));
  }
  return repoIndex.packageByFile.get(key) ?? null;
}

function importCandidatePaths(
  importerPath: string,
  specifier: string,
  budget: BoundedExpansionBudget = createBoundedExpansionBudget(),
): string[] {
  return sourceCandidatePaths(
    path.resolve(path.dirname(importerPath), specifier),
    budget,
  );
}

function tsconfigPathAliasCandidatePaths(
  repo: string,
  specifier: string,
  importerPath: string,
  targetBudget: BoundedExpansionBudget,
  candidateBudget: BoundedExpansionBudget,
): string[] {
  const relImporterPath = normalizeRelPath(path.relative(repo, importerPath));
  const configPath = findNearestConfig(repo, relImporterPath);
  if (!configPath) return [];

  const aliases = readTsConfigPathAliases(repo, configPath);
  if (!aliases) return [];
  if (aliases.partial) targetBudget.limited = true;

  const candidates: string[] = [];
  for (const mapping of aliases.mappings) {
    const wildcardValue = matchPathAliasPattern(mapping.pattern, specifier);
    if (wildcardValue === null) continue;
    for (const target of mapping.targets) {
      const expandedTarget = boundedWildcardSubstitution(
        target,
        wildcardValue,
        targetBudget,
      );
      if (expandedTarget === null) continue;
      const resolvedTarget = path.resolve(
        aliases.basePath,
        expandedTarget,
      );
      candidates.push(
        ...sourceCandidatePaths(resolvedTarget, candidateBudget),
      );
      if (
        candidateBudget.retainedResults >=
        candidateBudget.maxResults
      ) {
        candidateBudget.limited = true;
        return candidates;
      }
    }
  }
  return candidates;
}

function readTsConfigPathAliases(repo: string, configPath: string): TsConfigPathAliases | null {
  const resolvedConfigPath = path.resolve(configPath);
  const cacheKey = `${path.resolve(repo)}\0${resolvedConfigPath}`;
  const cached = pathAliasCache.get(cacheKey);
  if (cached) {
    pathAliasCache.delete(cacheKey);
    pathAliasCache.set(cacheKey, cached);
    return cached.aliases;
  }

  const aliases = readTsConfigPathAliasesUncached(repo, resolvedConfigPath);
  setPathAliasCache(cacheKey, aliases);
  return aliases;
}

function readTsConfigPathAliasesUncached(repo: string, configPath: string): TsConfigPathAliases | null {
  const configReader = createRepoConfigReader(repo);
  if (!configReader) return null;
  const readResult = ts.readConfigFile(configPath, configReader.readFile);
  if (readResult.error) return null;

  const parsed = ts.parseJsonConfigFileContent(
    normalizeTsConfigExtends(repo, configPath, readResult.config),
    configReader.host,
    path.dirname(configPath),
  );
  const paths = parsed.options.paths;
  if (!paths) return null;

  const optionsWithPathsBase = parsed.options as ts.CompilerOptions & { pathsBasePath?: string };
  const retentionBudget = createBoundedExpansionBudget({
    maxResults:
      PATH_ALIAS_MAPPING_LIMIT + PATH_ALIAS_TARGET_LIMIT,
  });
  const mappings: TsConfigPathAliases["mappings"] = [];
  let retainedTargets = 0;
  let partial = false;
  let stopMappingCollection = false;
  for (const pattern in paths) {
    if (!Object.prototype.hasOwnProperty.call(paths, pattern)) {
      continue;
    }
    if (!reserveBoundedTraversal(retentionBudget)) {
      partial = true;
      break;
    }
    const rawTargets = paths[pattern];
    if (!Array.isArray(rawTargets)) continue;
    if (mappings.length >= PATH_ALIAS_MAPPING_LIMIT) {
      partial = true;
      break;
    }
    const retainedPattern = retainBoundedExpansionValue(
      pattern,
      retentionBudget,
    );
    if (retainedPattern === null) {
      partial = true;
      continue;
    }
    const targets: string[] = [];
    for (
      let index = 0;
      index < rawTargets.length;
      index += 1
    ) {
      if (!reserveBoundedTraversal(retentionBudget)) {
        partial = true;
        stopMappingCollection = true;
        break;
      }
      const target = rawTargets[index];
      if (typeof target !== "string") continue;
      if (retainedTargets >= PATH_ALIAS_TARGET_LIMIT) {
        partial = true;
        stopMappingCollection = true;
        break;
      }
      const retainedTarget = retainBoundedExpansionValue(
        target,
        retentionBudget,
      );
      if (retainedTarget === null) {
        partial = true;
        continue;
      }
      retainedTargets += 1;
      targets.push(retainedTarget);
    }
    if (targets.length > 0) {
      mappings.push({ pattern: retainedPattern, targets });
    }
    if (stopMappingCollection) break;
  }
  partial ||= retentionBudget.limited;
  if (mappings.length === 0 && !partial) return null;
  return {
    basePath: parsed.options.baseUrl ?? optionsWithPathsBase.pathsBasePath ?? path.dirname(configPath),
    mappings,
    partial,
    retainedBytes: retentionBudget.retainedBytes,
  };
}

function setPathAliasCache(
  cacheKey: string,
  aliases: TsConfigPathAliases | null,
): void {
  const retainedBytes =
    Buffer.byteLength(cacheKey) +
    (aliases?.retainedBytes ?? 0) +
    (aliases ? Buffer.byteLength(aliases.basePath) : 0);
  if (retainedBytes > PATH_ALIAS_CACHE_BYTE_LIMIT) return;
  const previous = pathAliasCache.get(cacheKey);
  if (previous) {
    pathAliasCacheRetainedBytes -= previous.retainedBytes;
    pathAliasCache.delete(cacheKey);
  }
  pathAliasCache.set(cacheKey, { aliases, retainedBytes });
  pathAliasCacheRetainedBytes += retainedBytes;
  while (
    pathAliasCache.size > PATH_ALIAS_CACHE_ENTRY_LIMIT ||
    pathAliasCacheRetainedBytes > PATH_ALIAS_CACHE_BYTE_LIMIT
  ) {
    const oldestKey = pathAliasCache.keys().next().value as
      | string
      | undefined;
    if (oldestKey === undefined) break;
    const oldest = pathAliasCache.get(oldestKey);
    pathAliasCache.delete(oldestKey);
    pathAliasCacheRetainedBytes -= oldest?.retainedBytes ?? 0;
  }
}

function createRepoConfigReader(
  repo: string,
): {
  readFile: (fileName: string) => string | undefined;
  host: ts.ParseConfigHost;
} | null {
  const repoRoot = path.resolve(repo);
  let realRepoRoot: string;
  try {
    realRepoRoot = fs.realpathSync(repoRoot);
  } catch {
    return null;
  }
  const readCache = new Map<string, string | undefined>();
  const readFile = (fileName: string): string | undefined => {
    const resolvedPath = path.resolve(fileName);
    const key = canonicalPathKey(resolvedPath);
    if (!readCache.has(key)) {
      if (!isInsideRepo(repoRoot, resolvedPath)) {
        readCache.set(key, undefined);
        return undefined;
      }
      const text = readStableFile(
        resolvedPath,
        (candidatePath, realPath) =>
          isInsideRepo(repoRoot, candidatePath) &&
          isInsideRepo(realRepoRoot, realPath),
        (identity) => identity.size <= ANALYZER_METADATA_BYTE_LIMIT,
      )?.text;
      readCache.set(
        key,
        text === undefined || text === null
          ? undefined
          : normalizeTsConfigFileText(
              repo,
              resolvedPath,
              text,
            ),
      );
    }
    return readCache.get(key);
  };
  return {
    readFile,
    host: {
      useCaseSensitiveFileNames: ts.sys.useCaseSensitiveFileNames,
      fileExists: (fileName) => readFile(fileName) !== undefined,
      readFile,
      readDirectory: () => [],
    },
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

function packageSubpathCandidatePaths(
  packageRoot: string,
  subpath: string,
  candidateBudget: BoundedExpansionBudget =
    createBoundedExpansionBudget(),
): string[] {
  return [
    ...sourceCandidatePaths(
      path.resolve(packageRoot, subpath),
      candidateBudget,
    ),
    ...sourceCandidatePaths(
      path.resolve(packageRoot, "src", subpath),
      candidateBudget,
    ),
  ];
}

function packageRootCandidatePaths(
  packageInfo: PackageInfo,
  targetBudget: BoundedExpansionBudget =
    createBoundedExpansionBudget(),
  candidateBudget: BoundedExpansionBudget =
    createBoundedExpansionBudget(),
): string[] {
  return uniquePaths([
    ...packageExportCandidatePaths(
      packageInfo,
      ".",
      targetBudget,
      candidateBudget,
    ),
    ...packageEntrypointCandidatePaths(
      packageInfo,
      candidateBudget,
    ),
    ...packageSubpathCandidatePaths(
      packageInfo.root,
      "",
      candidateBudget,
    ),
  ]);
}

function packageEntrypointCandidatePaths(
  packageInfo: PackageInfo,
  candidateBudget: BoundedExpansionBudget,
): string[] {
  const candidates: string[] = [];
  for (const entrypoint of [
    packageInfo.types,
    packageInfo.typings,
    packageInfo.module,
    packageInfo.main,
  ]) {
    if (!entrypoint) continue;
    candidates.push(
      ...sourceCandidatePaths(
        path.resolve(packageInfo.root, entrypoint),
        candidateBudget,
      ),
    );
  }
  return candidates;
}

function packageExportCandidatePaths(
  packageInfo: PackageInfo,
  key: string,
  targetBudget: BoundedExpansionBudget =
    createBoundedExpansionBudget(),
  candidateBudget: BoundedExpansionBudget =
    createBoundedExpansionBudget(),
): string[] {
  const targets = packageExportTargetsForKey(
    packageInfo.exports,
    key,
    targetBudget,
  );
  const candidates: string[] = [];
  for (const target of targets) {
    candidates.push(
      ...sourceCandidatePaths(
        path.resolve(packageInfo.root, target),
        candidateBudget,
      ),
    );
  }
  return candidates;
}

function sourceCandidatePaths(
  basePath: string,
  budget: BoundedExpansionBudget =
    createBoundedExpansionBudget(),
): string[] {
  const candidates: string[] = [];
  const candidateKeys = new Set<string>();
  const add = (candidate: string): boolean => {
    const normalized = normalizeRelPath(path.resolve(candidate));
    if (candidateKeys.has(normalized)) return true;
    if (
      appendBoundedExpansionResult(
        candidates,
        normalized,
        budget,
      )
    ) {
      candidateKeys.add(normalized);
      return true;
    }
    return false;
  };
  const ext = path.extname(basePath);

  if (!add(basePath)) return candidates;
  if (ext) {
    const withoutExt = basePath.slice(0, -ext.length);
    for (const sourceExt of SOURCE_EXTENSION_SUBSTITUTIONS.get(ext.toLowerCase()) ?? []) {
      if (budget.retainedResults >= budget.maxResults) {
        budget.limited = true;
        break;
      }
      add(`${withoutExt}${sourceExt}`);
    }
  } else {
    for (const sourceExt of EXTENSIONLESS_SOURCE_EXTENSIONS) {
      if (budget.retainedResults >= budget.maxResults) {
        budget.limited = true;
        break;
      }
      add(`${basePath}${sourceExt}`);
    }
    for (const sourceExt of EXTENSIONLESS_SOURCE_EXTENSIONS) {
      if (budget.retainedResults >= budget.maxResults) {
        budget.limited = true;
        break;
      }
      add(path.join(basePath, `index${sourceExt}`));
    }
  }

  return candidates;
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
