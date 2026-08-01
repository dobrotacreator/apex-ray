import path from "node:path";

import ts from "typescript";

import {
  ANALYZER_METADATA_BYTE_LIMIT,
  ANALYZER_METADATA_FILE_LIMIT,
} from "../constants.js";
import {
  appendBoundedExpansionResult,
  boundedWildcardSubstitution,
  createBoundedExpansionBudget,
  reserveBoundedTraversal,
  retainBoundedExpansionValue,
} from "../bounded-expansion.js";
import { moduleSpecifierCandidatePaths } from "../module-resolution.js";
import { readPackageInfo } from "../package-info.js";
import { isTestPath } from "../test-discovery.js";
import type {
  Args,
  PackageInfo,
  RepoIndexCacheFileEntry,
} from "../types.js";
import {
  canonicalPathKey,
  inspectStableFile,
  isRecord,
  isSameOrInsideRepo,
  normalizeRelPath,
  readStableFile,
} from "../utils.js";
import type { RepoFileInventory } from "../workspace/inventory.js";

const RELEVANCE_PROBE_FILE_BYTE_LIMIT = 64 * 1024;
const RELEVANCE_PROBE_FILE_LIMIT = 4_096;
const RELEVANCE_PROBE_TOTAL_BYTE_LIMIT = 4 * 1024 * 1024;
const CONFIG_ROOT_FILE_NAMES = new Set([
  "app",
  "client",
  "index",
  "main",
  "server",
]);
const TYPESCRIPT_CONFIG_FILE_NAMES = new Set([
  "jsconfig.json",
  "tsconfig.json",
]);

interface RelevancePathAlias {
  pattern: string;
  targets: string[];
  basePath: string;
}

export function orderRepoIndexPaths(
  args: Args,
  inventory: RepoFileInventory,
  cachedFiles: Map<string, RepoIndexCacheFileEntry>,
  shouldStop: () => boolean,
  onExpansionLimit: () => void = () => {},
): string[] {
  const repo = path.resolve(args.repo);
  const inventoryKeys = inventory.pathKeys;
  const changedKeys = new Set(
    args.changed
      .map((relPath) => canonicalPathKey(path.resolve(repo, relPath)))
      .filter((key) => inventoryKeys.has(key)),
  );
  const changedModuleStems = new Set(
    [...changedKeys].map(modulePathStem),
  );
  const changedDirectoryKeys = new Set(
    [...changedKeys].map((fileName) =>
      canonicalPathKey(path.dirname(fileName)),
    ),
  );
  const changedBaseNames = new Set(
    args.changed.map((relPath) =>
      sourceBaseName(normalizeRelPath(relPath).toLowerCase()),
    ),
  );
  const configContext = collectConfigContext(
    inventory,
    shouldStop,
    onExpansionLimit,
  );
  const changedPackages = collectChangedPackages(
    args,
    inventory,
    shouldStop,
  );
  const directImporterKeys = new Set<string>();
  let retainedProbeBytes = 0;
  let probedFiles = 0;

  const probePaths = [...inventory.absPaths].sort((left, right) => {
    const priorityDifference =
      probePriority(
        left,
        changedKeys,
        changedDirectoryKeys,
        changedBaseNames,
        configContext.rootKeys,
      ) -
      probePriority(
        right,
        changedKeys,
        changedDirectoryKeys,
        changedBaseNames,
        configContext.rootKeys,
      );
    if (priorityDifference !== 0) return priorityDifference;
    return left < right ? -1 : left > right ? 1 : 0;
  });
  for (const absPath of probePaths) {
    if (shouldStop()) break;
    const fileKey = canonicalPathKey(absPath);
    if (changedKeys.has(fileKey)) continue;
    const cached = cachedFiles.get(fileKey);
    let moduleSpecifiers: string[];
    if (cached) {
      moduleSpecifiers = cached.imports.map(
        (entry) => entry.moduleSpecifier,
      );
    } else {
      if (probedFiles >= RELEVANCE_PROBE_FILE_LIMIT) break;
      probedFiles += 1;
      let probeAccepted = false;
      const snapshot = readStableFile(
        absPath,
        (resolvedPath, realPath) =>
          inventoryKeys.has(canonicalPathKey(resolvedPath)) &&
          inventoryKeys.has(canonicalPathKey(realPath)),
        (identity) => {
          const accepted =
            identity.size <= RELEVANCE_PROBE_FILE_BYTE_LIMIT &&
            identity.size <=
              RELEVANCE_PROBE_TOTAL_BYTE_LIMIT - retainedProbeBytes;
          probeAccepted = accepted;
          return accepted;
        },
      );
      if (!snapshot?.text || !probeAccepted) continue;
      retainedProbeBytes += snapshot.identity.size;
      moduleSpecifiers = preProcessModuleSpecifiers(snapshot.text);
    }
    if (
      moduleSpecifiers.some((specifier) =>
        importTargetsChangedFile(
          specifier,
          absPath,
          repo,
          changedKeys,
          changedModuleStems,
          configContext.aliases,
          changedPackages,
          onExpansionLimit,
        ),
      )
    ) {
      directImporterKeys.add(fileKey);
    }
  }

  return [...inventory.absPaths].sort((left, right) => {
    const priorityDifference =
      relevancePriority(
        left,
        changedKeys,
        directImporterKeys,
        changedBaseNames,
        configContext.rootKeys,
      ) -
      relevancePriority(
        right,
        changedKeys,
        directImporterKeys,
        changedBaseNames,
        configContext.rootKeys,
      );
    if (priorityDifference !== 0) return priorityDifference;
    return left < right ? -1 : left > right ? 1 : 0;
  });
}

function collectConfigContext(
  inventory: RepoFileInventory,
  shouldStop: () => boolean,
  onExpansionLimit: () => void,
): { rootKeys: Set<string>; aliases: RelevancePathAlias[] } {
  const roots = new Set<string>();
  const aliases: RelevancePathAlias[] = [];
  const configDirectories = new Set<string>();
  let retainedBytes = 0;
  let retainedFiles = 0;
  let retainedMappings = 0;
  let retainedTargets = 0;
  const aliasBudget = createBoundedExpansionBudget({
    maxResults: 1_024,
  });
  let aliasLimitReported = false;
  const markAliasLimit = (): void => {
    if (aliasLimitReported) return;
    aliasLimitReported = true;
    onExpansionLimit();
  };
  let stopAliasCollection = false;
  const orderedConfigPaths = [...inventory.configJsonAbsPaths].sort(
    (left, right) => {
      const priorityDifference =
        configMetadataPriority(left) - configMetadataPriority(right);
      if (priorityDifference !== 0) return priorityDifference;
      return left < right ? -1 : left > right ? 1 : 0;
    },
  );
  for (const configPath of orderedConfigPaths) {
    if (
      shouldStop() ||
      retainedFiles >= ANALYZER_METADATA_FILE_LIMIT ||
      retainedBytes >= ANALYZER_METADATA_BYTE_LIMIT
    ) {
      break;
    }
    configDirectories.add(
      canonicalPathKey(path.dirname(path.resolve(configPath))),
    );
    const snapshot = readStableFile(
      configPath,
      (resolvedPath, realPath) =>
        inventory.configPathKeys.has(canonicalPathKey(resolvedPath)) &&
        inventory.configPathKeys.has(canonicalPathKey(realPath)),
      (identity) =>
        identity.size <= ANALYZER_METADATA_BYTE_LIMIT &&
        identity.size <= ANALYZER_METADATA_BYTE_LIMIT - retainedBytes,
    );
    if (!snapshot?.text) continue;
    retainedFiles += 1;
    retainedBytes += snapshot.identity.size;
    const parsed = ts.parseConfigFileTextToJson(configPath, snapshot.text);
    if (parsed.error || !isRecord(parsed.config)) continue;
    const files = parsed.config.files;
    if (Array.isArray(files)) {
      for (const fileName of files) {
        if (typeof fileName !== "string") continue;
        const fileKey = canonicalPathKey(
          path.resolve(path.dirname(configPath), fileName),
        );
        if (inventory.pathKeys.has(fileKey)) roots.add(fileKey);
      }
    }
    const compilerOptions = parsed.config.compilerOptions;
    if (!isRecord(compilerOptions) || !isRecord(compilerOptions.paths)) {
      continue;
    }
    const basePath =
      typeof compilerOptions.baseUrl === "string"
        ? path.resolve(
            path.dirname(configPath),
            compilerOptions.baseUrl,
          )
        : path.dirname(configPath);
    for (const pattern in compilerOptions.paths) {
      if (
        !Object.prototype.hasOwnProperty.call(
          compilerOptions.paths,
          pattern,
        )
      ) {
        continue;
      }
      if (
        !reserveBoundedTraversal(aliasBudget) ||
        retainedMappings >= 512
      ) {
        aliasBudget.limited = true;
        markAliasLimit();
        stopAliasCollection = true;
        break;
      }
      const rawTargets = compilerOptions.paths[pattern];
      if (!Array.isArray(rawTargets)) continue;
      const retainedPattern = retainBoundedExpansionValue(
        pattern,
        aliasBudget,
      );
      if (retainedPattern === null) {
        markAliasLimit();
        continue;
      }
      const targets: string[] = [];
      for (
        let index = 0;
        index < rawTargets.length;
        index += 1
      ) {
        if (
          !reserveBoundedTraversal(aliasBudget) ||
          retainedTargets >= 512
        ) {
          aliasBudget.limited = true;
          markAliasLimit();
          stopAliasCollection = true;
          break;
        }
        const target = rawTargets[index];
        if (typeof target !== "string") continue;
        const retainedTarget = retainBoundedExpansionValue(
          target,
          aliasBudget,
        );
        if (retainedTarget === null) {
          markAliasLimit();
          continue;
        }
        retainedTargets += 1;
        targets.push(retainedTarget);
      }
      if (targets.length === 0) {
        if (stopAliasCollection) break;
        continue;
      }
      retainedMappings += 1;
      aliases.push({
        pattern: retainedPattern,
        targets,
        basePath,
      });
      if (stopAliasCollection) break;
    }
    if (stopAliasCollection) break;
  }
  if (aliasBudget.limited) markAliasLimit();
  for (const absPath of inventory.absPaths) {
    const directoryKey = canonicalPathKey(path.dirname(absPath));
    if (!configDirectories.has(directoryKey)) continue;
    if (CONFIG_ROOT_FILE_NAMES.has(sourceBaseName(absPath.toLowerCase()))) {
      roots.add(canonicalPathKey(absPath));
    }
  }
  return { rootKeys: roots, aliases };
}

function configMetadataPriority(filePath: string): number {
  return TYPESCRIPT_CONFIG_FILE_NAMES.has(
    path.basename(filePath).toLowerCase(),
  )
    ? 0
    : 1;
}

function preProcessModuleSpecifiers(text: string): string[] {
  try {
    return ts
      .preProcessFile(text, true, true)
      .importedFiles.map((entry) => entry.fileName);
  } catch {
    return [];
  }
}

function importTargetsChangedFile(
  specifier: string,
  importerPath: string,
  repo: string,
  changedKeys: Set<string>,
  changedModuleStems: Set<string>,
  aliases: RelevancePathAlias[],
  changedPackages: PackageInfo[],
  onExpansionLimit: () => void,
): boolean {
  if (specifier.startsWith(".")) {
    return candidatePathsTargetChangedFile(
      moduleSpecifierCandidatePaths(
        specifier,
        importerPath,
        repo,
        null,
        onExpansionLimit,
      ),
      changedKeys,
      changedModuleStems,
    );
  }
  const targetPackage =
    changedPackages.find(
      (packageInfo) =>
        specifier === packageInfo.name ||
        specifier.startsWith(`${packageInfo.name}/`),
    ) ?? null;
  if (
    targetPackage &&
    candidatePathsTargetChangedFile(
      moduleSpecifierCandidatePaths(
        specifier,
        importerPath,
        repo,
        targetPackage,
        onExpansionLimit,
      ),
      changedKeys,
      changedModuleStems,
    )
  ) {
    return true;
  }
  const targetBudget = createBoundedExpansionBudget();
  const candidateBudget = createBoundedExpansionBudget();
  for (const alias of aliases) {
    const wildcard = matchAliasPattern(alias.pattern, specifier);
    if (wildcard === null) continue;
    const candidates: string[] = [];
    for (const target of alias.targets) {
      const expanded = boundedWildcardSubstitution(
        target,
        wildcard,
        targetBudget,
      );
      if (expanded === null) continue;
      appendBoundedExpansionResult(
        candidates,
        path.resolve(alias.basePath, expanded),
        candidateBudget,
      );
    }
    if (
      candidatePathsTargetChangedFile(
        candidates,
        changedKeys,
        changedModuleStems,
      )
    ) {
      if (targetBudget.limited || candidateBudget.limited) {
        onExpansionLimit();
      }
      return true;
    }
  }
  if (targetBudget.limited || candidateBudget.limited) {
    onExpansionLimit();
  }
  return false;
}

function candidatePathsTargetChangedFile(
  candidates: string[],
  changedKeys: Set<string>,
  changedModuleStems: Set<string>,
): boolean {
  return candidates.some((candidate) => {
    const key = canonicalPathKey(path.resolve(candidate));
    if (changedKeys.has(key)) return true;
    const stem = modulePathStem(candidate);
    return (
      changedModuleStems.has(stem) ||
      changedModuleStems.has(
        canonicalPathKey(path.join(stem, "index")),
      )
    );
  });
}

function collectChangedPackages(
  args: Args,
  inventory: RepoFileInventory,
  shouldStop: () => boolean,
): PackageInfo[] {
  const changedPaths = args.changed.map((relPath) =>
    path.resolve(args.repo, relPath),
  );
  const candidatePackagePaths = inventory.packageJsonAbsPaths
    .filter((packageJsonPath) =>
      changedPaths.some((changedPath) =>
        isSameOrInsideRepo(path.dirname(packageJsonPath), changedPath),
      ),
    )
    .sort(
      (left, right) =>
        path.dirname(right).length - path.dirname(left).length,
    );
  const packages = new Map<string, PackageInfo>();
  let retainedBytes = 0;
  let inspectedPackages = 0;
  for (const packageJsonPath of candidatePackagePaths) {
    if (
      shouldStop() ||
      inspectedPackages >= 128 ||
      retainedBytes >= ANALYZER_METADATA_BYTE_LIMIT
    ) {
      break;
    }
    inspectedPackages += 1;
    const inspection = inspectStableFile(
      packageJsonPath,
      (resolvedPath, realPath) =>
        inventory.packagePathKeys.has(
          canonicalPathKey(resolvedPath),
        ) &&
        inventory.packagePathKeys.has(canonicalPathKey(realPath)),
    );
    if (
      !inspection ||
      inspection.identity.size >
        ANALYZER_METADATA_BYTE_LIMIT - retainedBytes
    ) {
      continue;
    }
    retainedBytes += inspection.identity.size;
    const packageInfo = readPackageInfo(
      path.dirname(packageJsonPath),
      packageJsonPath,
      (resolvedPath, realPath) =>
        inventory.packagePathKeys.has(
          canonicalPathKey(resolvedPath),
        ) &&
        inventory.packagePathKeys.has(canonicalPathKey(realPath)),
    );
    if (packageInfo) packages.set(packageInfo.name, packageInfo);
  }
  return [...packages.values()];
}

function matchAliasPattern(
  pattern: string,
  specifier: string,
): string | null {
  const wildcardIndex = pattern.indexOf("*");
  if (wildcardIndex < 0) return pattern === specifier ? "" : null;
  const prefix = pattern.slice(0, wildcardIndex);
  const suffix = pattern.slice(wildcardIndex + 1);
  if (
    !specifier.startsWith(prefix) ||
    !specifier.endsWith(suffix) ||
    specifier.length < prefix.length + suffix.length
  ) {
    return null;
  }
  return specifier.slice(
    prefix.length,
    specifier.length - suffix.length,
  );
}

function probePriority(
  absPath: string,
  changedKeys: Set<string>,
  changedDirectoryKeys: Set<string>,
  changedBaseNames: Set<string>,
  configRootKeys: Set<string>,
): number {
  const key = canonicalPathKey(absPath);
  if (changedKeys.has(key)) return 0;
  const normalized = normalizeRelPath(absPath).toLowerCase();
  const test = isTestPath(normalized);
  if (
    test &&
    changedBaseNames.has(sourceBaseName(normalized))
  ) {
    return 1;
  }
  if (configRootKeys.has(key)) return 2;
  if (test) return 3;
  if (
    changedDirectoryKeys.has(
      canonicalPathKey(path.dirname(absPath)),
    )
  ) {
    return 4;
  }
  return 5;
}

function relevancePriority(
  absPath: string,
  changedKeys: Set<string>,
  directImporterKeys: Set<string>,
  changedBaseNames: Set<string>,
  configRootKeys: Set<string>,
): number {
  const key = canonicalPathKey(absPath);
  if (changedKeys.has(key)) return 0;
  const test = isTestPath(normalizeRelPath(absPath).toLowerCase());
  if (directImporterKeys.has(key)) return test ? 1 : 2;
  if (
    test &&
    changedBaseNames.has(sourceBaseName(absPath.toLowerCase()))
  ) {
    return 3;
  }
  if (configRootKeys.has(key)) return 4;
  if (test) return 5;
  return 6;
}

function modulePathStem(filePath: string): string {
  return canonicalPathKey(
    path.resolve(filePath).replace(
      /(?:\.d)?\.[cm]?[jt]sx?$/iu,
      "",
    ),
  );
}

function sourceBaseName(filePath: string): string {
  return path.basename(filePath).replace(
    /(?:\.(?:test|spec))?(?:\.d)?\.[cm]?[jt]sx?$/iu,
    "",
  );
}
