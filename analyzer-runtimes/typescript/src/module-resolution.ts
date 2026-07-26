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
  canonicalPathKey,
  isInsideRepo,
  isSameOrInsideRepo,
  normalizeRelPath,
  readStableUtf8File,
  uniquePaths,
} from "./utils.js";

const pathAliasCache = new Map<string, TsConfigPathAliases | null>();
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
  if (!paths || Object.keys(paths).length === 0) return null;

  const optionsWithPathsBase = parsed.options as ts.CompilerOptions & { pathsBasePath?: string };
  return {
    basePath: parsed.options.baseUrl ?? optionsWithPathsBase.pathsBasePath ?? path.dirname(configPath),
    mappings: Object.entries(paths)
      .filter((entry): entry is [string, string[]] => Array.isArray(entry[1]))
      .map(([pattern, targets]) => ({ pattern, targets })),
  };
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
      const text = readStableUtf8File(
        resolvedPath,
        (candidatePath, realPath) =>
          isInsideRepo(repoRoot, candidatePath) &&
          isInsideRepo(realRepoRoot, realPath),
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
  const targets = packageExportTargetsForKey(packageInfo.exports, key);
  return targets.flatMap((target) => sourceCandidatePaths(path.resolve(packageInfo.root, target)));
}

function sourceCandidatePaths(basePath: string): string[] {
  const candidates = new Set<string>();
  const add = (candidate: string) => candidates.add(normalizeRelPath(path.resolve(candidate)));
  const ext = path.extname(basePath);

  add(basePath);
  if (ext) {
    const withoutExt = basePath.slice(0, -ext.length);
    for (const sourceExt of SOURCE_EXTENSION_SUBSTITUTIONS.get(ext.toLowerCase()) ?? []) {
      add(`${withoutExt}${sourceExt}`);
    }
  } else {
    for (const sourceExt of EXTENSIONLESS_SOURCE_EXTENSIONS) {
      add(`${basePath}${sourceExt}`);
    }
    for (const sourceExt of EXTENSIONLESS_SOURCE_EXTENSIONS) {
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
