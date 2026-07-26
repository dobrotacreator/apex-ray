import fs from "node:fs";
import path from "node:path";

import ts from "typescript";

import {
  FOCUSED_PROGRAM_CHANGED_FILE_THRESHOLD,
  FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT,
} from "./constants.js";
import { readPackageInfo } from "./package-info.js";
import type { Args, ProgramContext } from "./types.js";
import { loadRepoFileInventory, type RepoFileInventory } from "./workspace/inventory.js";
import {
  formatDiagnostic,
  isDeclarationFileName,
  isInsideRepo,
  isSameOrInsideRepo,
  normalizeRelPath,
  scriptKindForPath,
  uniquePaths,
  walk,
} from "./utils.js";

const workspacePackageRootCache = new Map<string, string | null>();
type ConfigReadResult = ReturnType<typeof ts.readConfigFile>;

export function createProgramContexts(
  args: Args,
  warnings: string[],
  inventory: RepoFileInventory | null = args.fileManifestPath ? loadRepoFileInventory(args) : null,
): Map<string, ProgramContext> {
  const packageBoundaryCache = new Map<string, string>();
  const configReadCache = new Map<string, ConfigReadResult>();
  const groups = new Map<
    string,
    {
      tsconfigPath: string | null;
      configReadResult: ConfigReadResult | null;
      packageRoot: string;
      changedFiles: string[];
    }
  >();
  for (const changedFile of args.changed) {
    let tsconfigPath = findNearestConfig(args.repo, changedFile);
    let configReadResult: ConfigReadResult | null = null;
    if (tsconfigPath) {
      const firstRead = !configReadCache.has(tsconfigPath);
      configReadResult = configReadCache.get(tsconfigPath) ?? ts.readConfigFile(tsconfigPath, ts.sys.readFile);
      configReadCache.set(tsconfigPath, configReadResult);
      if (configReadResult.error) {
        if (firstRead) warnings.push(formatDiagnostic(configReadResult.error));
        tsconfigPath = null;
        configReadResult = null;
      }
    }
    const packageRoot = findNearestPackageBoundary(args.repo, changedFile, packageBoundaryCache);
    const key = tsconfigPath ?? `<no-tsconfig>:${normalizeRelPath(packageRoot)}`;
    const group = groups.get(key) ?? {
      tsconfigPath,
      configReadResult,
      packageRoot,
      changedFiles: [],
    };
    group.changedFiles.push(changedFile);
    groups.set(key, group);
  }

  const contextsByFile = new Map<string, ProgramContext>();
  const declarationRootsByPackage = inventory
    ? indexDeclarationRootsByPackage(
        args.repo,
        inventory.declarationAbsPaths,
        packageBoundaryCache,
      )
    : null;
  for (const group of groups.values()) {
    const noConfigDeclarationRoots = declarationRootsByPackage
      ? declarationRootsForPackageHierarchy(
          args.repo,
          group.packageRoot,
          declarationRootsByPackage,
        )
      : null;
    const program = createProgram(
      args.repo,
      group.tsconfigPath,
      group.configReadResult,
      group.changedFiles,
      warnings,
      args.largeChangeSetSize,
      inventory,
      noConfigDeclarationRoots,
    );
    const context = {
      program,
      checker: program.getTypeChecker(),
      tsconfigPath: group.tsconfigPath,
    };
    for (const changedFile of group.changedFiles) {
      contextsByFile.set(changedFile, context);
    }
  }

  return contextsByFile;
}

export function findNearestConfig(repo: string, changedFile: string): string | null {
  const repoRoot = path.resolve(repo);
  const changedPath = path.resolve(repoRoot, changedFile);
  if (!isInsideRepo(repoRoot, changedPath)) return null;

  let current = path.dirname(changedPath);
  while (true) {
    for (const name of ["tsconfig.json", "jsconfig.json"]) {
      const candidate = path.join(current, name);
      if (fs.existsSync(candidate)) return candidate;
    }
    if (current === repoRoot) return null;
    const parent = path.dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

function createProgram(
  repo: string,
  configPath: string | null,
  configReadResult: ConfigReadResult | null,
  changed: string[],
  warnings: string[],
  largeChangeSetSize: number | null,
  inventory: RepoFileInventory | null,
  noConfigDeclarationRoots: string[] | null,
): ts.Program {
  if (configPath) {
    const readResult = configReadResult ?? ts.readConfigFile(configPath, ts.sys.readFile);
    if (readResult.error) {
      warnings.push(formatDiagnostic(readResult.error));
    } else {
      const parsed = ts.parseJsonConfigFileContent(
        normalizeTsConfigExtends(repo, configPath, readResult.config),
        ts.sys,
        path.dirname(configPath),
      );
      if (parsed.errors.length > 0) {
        warnings.push(...parsed.errors.map(formatDiagnostic));
      }
      const changedRootNames = changed.map((file) => path.resolve(repo, file));
      const permittedConfigRootNames = configRootNamesPermittedByInventory(
        repo,
        parsed.fileNames,
        inventory,
      );
      const permittedDeclarationRoots = permittedConfigRootNames.filter(isDeclarationFileName);
      const reviewOptions = compilerOptionsForChangedRoots(parsed.options, changedRootNames);
      const focusedProgramFileCount = largeChangeSetSize ?? changedRootNames.length;
      if (
        changedRootNames.length >= FOCUSED_PROGRAM_CHANGED_FILE_THRESHOLD ||
        focusedProgramFileCount >= FOCUSED_PROGRAM_CHANGED_FILE_THRESHOLD
      ) {
        const selectedDeclarationRoots = selectSupplementalDeclarationRoots(
          permittedDeclarationRoots,
          changedRootNames,
          warnings,
        );
        warnings.push(
          `Large TypeScript change set (${focusedProgramFileCount} files); using focused program roots to keep analysis bounded.`,
        );
        return ts.createProgram({
          rootNames: uniquePaths([...changedRootNames, ...selectedDeclarationRoots]),
          options: reviewOptions,
        });
      }
      return ts.createProgram({
        rootNames: uniquePaths([...permittedConfigRootNames, ...changedRootNames]),
        options: reviewOptions,
      });
    }
  }

  const changedRootNames = changed.map((file) => path.resolve(repo, file));
  const selectedDeclarationRoots = selectSupplementalDeclarationRoots(
    noConfigDeclarationRoots ?? inventory?.declarationAbsPaths ?? [],
    changedRootNames,
    warnings,
  );
  warnings.push(
    selectedDeclarationRoots.length
      ? `No tsconfig.json or jsconfig.json found; using changed files and ${selectedDeclarationRoots.length} permitted declaration root(s).`
      : "No tsconfig.json or jsconfig.json found; using changed files only.",
  );
  return ts.createProgram({
    rootNames: uniquePaths([...changedRootNames, ...selectedDeclarationRoots]),
    options: {
      allowJs: true,
      checkJs: false,
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.Node10,
      target: ts.ScriptTarget.ES2022,
    },
  });
}

function compilerOptionsForChangedRoots(
  options: ts.CompilerOptions,
  changedRootNames: string[],
): ts.CompilerOptions {
  const includesJavaScript = changedRootNames.some((fileName) => {
    const scriptKind = scriptKindForPath(fileName);
    return scriptKind === ts.ScriptKind.JS || scriptKind === ts.ScriptKind.JSX;
  });
  return includesJavaScript && !options.allowJs
    ? { ...options, allowJs: true }
    : options;
}

function findNearestPackageBoundary(
  repo: string,
  fileName: string,
  cache: Map<string, string>,
): string {
  const repoRoot = path.resolve(repo);
  let current = path.dirname(path.resolve(repoRoot, fileName));
  if (!isSameOrInsideRepo(repoRoot, current)) return repoRoot;

  const visited: string[] = [];
  let boundary: string;
  while (true) {
    const cached = cache.get(current);
    if (cached) {
      boundary = cached;
      break;
    }
    visited.push(current);
    if (fs.existsSync(path.join(current, "package.json")) || current === repoRoot) {
      boundary = current;
      break;
    }
    const parent = path.dirname(current);
    if (parent === current || !isSameOrInsideRepo(repoRoot, parent)) {
      boundary = repoRoot;
      break;
    }
    current = parent;
  }
  for (const directory of visited) cache.set(directory, boundary);
  return boundary;
}

function indexDeclarationRootsByPackage(
  repo: string,
  declarationRoots: string[],
  cache: Map<string, string>,
): Map<string, string[]> {
  const rootsByPackage = new Map<string, string[]>();
  for (const fileName of declarationRoots) {
    const packageKey = normalizeRelPath(
      findNearestPackageBoundary(repo, fileName, cache),
    );
    const roots = rootsByPackage.get(packageKey) ?? [];
    roots.push(fileName);
    rootsByPackage.set(packageKey, roots);
  }
  return rootsByPackage;
}

function declarationRootsForPackageHierarchy(
  repo: string,
  packageRoot: string,
  rootsByPackage: Map<string, string[]>,
): string[] {
  const repoRoot = path.resolve(repo);
  let current = path.resolve(packageRoot);
  const declarationRoots: string[] = [];
  while (isSameOrInsideRepo(repoRoot, current)) {
    declarationRoots.push(
      ...(rootsByPackage.get(normalizeRelPath(current)) ?? []),
    );
    if (current === repoRoot) break;
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return declarationRoots;
}

function configRootNamesPermittedByInventory(
  repo: string,
  configFileNames: string[],
  inventory: RepoFileInventory | null,
): string[] {
  if (!inventory) return configFileNames;
  const repoRoot = path.resolve(repo);
  return configFileNames.filter((fileName) => {
    const resolved = path.resolve(fileName);
    return (
      !isInsideRepo(repoRoot, resolved) ||
      inventory.pathKeys.has(normalizeRelPath(resolved))
    );
  });
}

function selectSupplementalDeclarationRoots(
  declarationRoots: string[],
  changedRoots: string[],
  warnings: string[],
): string[] {
  const changedKeys = new Set(
    changedRoots.map((fileName) => normalizeRelPath(path.resolve(fileName))),
  );
  const supplementalRoots = uniquePaths(declarationRoots)
    .filter((fileName) => !changedKeys.has(normalizeRelPath(path.resolve(fileName))))
    .sort(compareDeclarationRoots);
  if (supplementalRoots.length <= FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT) {
    return supplementalRoots;
  }
  warnings.push(
    `TypeScript declaration roots capped at ${FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT} of ${supplementalRoots.length}; ambient declaration coverage is partial.`,
  );
  return supplementalRoots.slice(0, FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT);
}

function compareDeclarationRoots(left: string, right: string): number {
  const leftPriority = ambientDeclarationPriority(left);
  const rightPriority = ambientDeclarationPriority(right);
  if (leftPriority !== rightPriority) return leftPriority - rightPriority;

  const leftKey = normalizeRelPath(path.resolve(left)).toLowerCase();
  const rightKey = normalizeRelPath(path.resolve(right)).toLowerCase();
  if (leftKey < rightKey) return -1;
  if (leftKey > rightKey) return 1;
  const resolvedLeft = normalizeRelPath(path.resolve(left));
  const resolvedRight = normalizeRelPath(path.resolve(right));
  if (resolvedLeft < resolvedRight) return -1;
  if (resolvedLeft > resolvedRight) return 1;
  return 0;
}

function ambientDeclarationPriority(fileName: string): number {
  const declarationStem = path.basename(fileName).replace(/\.d\.(?:ts|mts|cts)$/i, "");
  return /(?:^|[._-])(?:global|globals|env)(?:$|[._-])/i.test(declarationStem) ? 0 : 1;
}

export function normalizeTsConfigExtends(repo: string, configPath: string, config: unknown): unknown {
  if (!isRecord(config)) return config;
  const extendsValue = config.extends;
  if (typeof extendsValue === "string") {
    return {
      ...config,
      extends: resolveTsConfigExtends(repo, configPath, extendsValue),
    };
  }
  if (Array.isArray(extendsValue)) {
    return {
      ...config,
      extends: extendsValue.map((value) =>
        typeof value === "string" ? resolveTsConfigExtends(repo, configPath, value) : value,
      ),
    };
  }
  return config;
}

function resolveTsConfigExtends(repo: string, configPath: string, extendsValue: string): string {
  if (extendsValue.startsWith(".") || path.isAbsolute(extendsValue)) return extendsValue;

  const parsed = parsePackageSpecifier(extendsValue);
  if (!parsed) return extendsValue;

  const packageRoot = findWorkspacePackageRoot(repo, parsed.packageName);
  if (!packageRoot) return extendsValue;

  if (parsed.subpath) return path.join(packageRoot, parsed.subpath);
  const defaultConfig = path.join(packageRoot, "tsconfig.json");
  return fs.existsSync(defaultConfig) ? defaultConfig : path.join(packageRoot, "package.json");
}

function parsePackageSpecifier(specifier: string): { packageName: string; subpath: string } | null {
  const parts = specifier.split("/");
  if (specifier.startsWith("@")) {
    if (parts.length < 2 || !parts[0] || !parts[1]) return null;
    return {
      packageName: `${parts[0]}/${parts[1]}`,
      subpath: parts.slice(2).join("/"),
    };
  }
  if (!parts[0]) return null;
  return {
    packageName: parts[0],
    subpath: parts.slice(1).join("/"),
  };
}

function findWorkspacePackageRoot(repo: string, packageName: string): string | null {
  const repoRoot = path.resolve(repo);
  const cacheKey = `${repoRoot}\0${packageName}`;
  if (workspacePackageRootCache.has(cacheKey)) {
    return workspacePackageRootCache.get(cacheKey) ?? null;
  }

  let found: string | null = null;
  walk(repoRoot, (absPath) => {
    if (found || path.basename(absPath) !== "package.json") return;
    const packageInfo = readPackageInfo(path.dirname(absPath), absPath);
    if (packageInfo?.name === packageName) {
      found = packageInfo.root;
    }
  });
  workspacePackageRootCache.set(cacheKey, found);
  return found;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
