import fs from "node:fs";
import path from "node:path";

import ts from "typescript";

import {
  ANALYZER_METADATA_BYTE_LIMIT,
  ANALYZER_METADATA_FILE_LIMIT,
  ANALYZER_PACKAGE_INFO_LIMIT,
  ANALYZER_SOURCE_BYTE_LIMIT,
  ANALYZER_SOURCE_FILE_LIMIT,
  FOCUSED_PROGRAM_CHANGED_FILE_THRESHOLD,
  FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT,
} from "./constants.js";
import {
  packageExportTargetsForKey,
  readPackageInfo,
} from "./package-info.js";
import { createBoundedExpansionBudget } from "./bounded-expansion.js";
import type { Args, PackageInfo, ProgramContext } from "./types.js";
import { loadRepoFileInventory, type RepoFileInventory } from "./workspace/inventory.js";
import {
  canonicalPathKey,
  formatDiagnostic,
  inspectStableFile,
  isDeclarationFileName,
  isInsideRepo,
  isSameOrInsideRepo,
  normalizeRelPath,
  readStableFile,
  scriptKindForPath,
  uniquePaths,
  walk,
} from "./utils.js";

interface WorkspacePackageInfoCacheEntry {
  packageInfo: PackageInfo | null;
  fileName: string | null;
  retainedBytes: number;
}
const workspacePackageInfoCache = new Map<
  string,
  WorkspacePackageInfoCacheEntry
>();
let workspacePackageInfoCacheBytes = 0;
interface BoundedWorkspacePackageIndex {
  packages: Map<string, PackageInfo>;
  retainedPackages: Array<{
    fileName: string;
    fileBytes: number;
  }>;
}
const boundedWorkspacePackageIndexCache = new WeakMap<
  RepoFileInventory,
  BoundedWorkspacePackageIndex
>();
const SUPPLEMENTAL_DECLARATION_ROOT_BYTE_LIMIT = 512 * 1024;
const SUPPLEMENTAL_DECLARATION_EXCLUDED_DIRECTORIES = new Set([
  ".next",
  ".pnpm",
  ".turbo",
  "build",
  "coverage",
  "dist",
  "generated",
  "node_modules",
  "out",
]);
type ConfigReadResult = ReturnType<typeof ts.readConfigFile>;

interface InternalFileMatcherPatterns {
  includeFilePatterns: readonly string[] | undefined;
  excludePattern: string | undefined;
}

interface TypeScriptFileMatcherApi {
  getFileMatcherPatterns(
    rootDir: string,
    excludes: readonly string[] | undefined,
    includes: readonly string[] | undefined,
    useCaseSensitiveFileNames: boolean,
    currentDirectory: string,
  ): InternalFileMatcherPatterns;
}

interface SupplementalDeclarationSelectionOptions {
  isEligible?: (fileName: string) => boolean;
  markPartial?: () => void;
  shouldStop?: () => boolean;
}

interface ProgramSourceBudget {
  acceptedFileInstances: number;
  acceptedBytes: number;
  reservedChangedFileBytesByKey: Map<string, number>;
  claimedReservedChangedKeys: Set<string>;
  warningEmitted: boolean;
  warnings: string[];
  inventory: RepoFileInventory | null;
}

interface ProgramSourceScope {
  acceptedFileBytesByKey: Map<string, number>;
  changedFileKeys: Set<string>;
}

interface CompilerMetadataBudget {
  retainedBytesByKey: Map<string, number>;
  lookupKeys: Set<string>;
  packageKeys: Set<string>;
  retainedBytes: number;
  rejectionCount: number;
  reserveLookup(fileName: string): boolean;
  reserveFile(fileName: string, fileBytes: number): boolean;
  reservePackage(fileName: string): boolean;
  reportFileLimit(fileName: string, fileBytes: number): void;
  reportExpansionLimit(): void;
}

export function createProgramContexts(
  args: Args,
  warnings: string[],
  inventory: RepoFileInventory | null = args.fileManifestPath ? loadRepoFileInventory(args) : null,
  shouldStop: () => boolean = () => false,
): Map<string, ProgramContext> {
  const packageBoundaryCache = new Map<string, string>();
  const configReadCache = new Map<string, ConfigReadResult>();
  const metadataBudget = createCompilerMetadataBudget(
    warnings,
    inventory,
  );
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
      configReadResult =
        configReadCache.get(tsconfigPath) ??
        readStableConfigFile(
          args.repo,
          tsconfigPath,
          tsconfigPath,
          inventory,
          metadataBudget,
        );
      configReadCache.set(tsconfigPath, configReadResult);
      if (configReadResult.error) {
        if (firstRead) warnings.push(formatDiagnostic(configReadResult.error));
        markConfigurationPartial(inventory, warnings);
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
  const sourceBudget = createProgramSourceBudget(warnings, inventory);
  reserveChangedProgramSources(
    args.repo,
    args.changed,
    sourceBudget,
    inventory,
  );
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
    let context: ProgramContext;
    try {
      const program = createProgram(
        args.repo,
        group.tsconfigPath,
        group.configReadResult,
        group.changedFiles,
        warnings,
        args.largeChangeSetSize,
        inventory,
        noConfigDeclarationRoots,
        shouldStop,
        sourceBudget,
        metadataBudget,
      );
      context = {
        program,
        checker: program.getTypeChecker(),
        tsconfigPath: group.tsconfigPath,
      };
    } catch {
      warnings.push(
        `TypeScript compiler could not create a program for ${group.changedFiles.length} changed ` +
          `file${group.changedFiles.length === 1 ? "" : "s"}; analysis for that group is partial.`,
      );
      continue;
    }
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
      if (isSafeRepoConfigFile(repoRoot, candidate)) return candidate;
    }
    if (current === repoRoot) return null;
    const parent = path.dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

function isSafeRepoConfigFile(repo: string, candidate: string): boolean {
  const repoRoot = path.resolve(repo);
  const resolvedCandidate = path.resolve(candidate);
  if (!isInsideRepo(repoRoot, resolvedCandidate)) return false;

  let stat: fs.Stats;
  try {
    stat = fs.lstatSync(resolvedCandidate);
  } catch {
    return false;
  }
  if (!stat.isFile() || stat.isSymbolicLink()) return false;

  try {
    return isInsideRepo(
      fs.realpathSync(repoRoot),
      fs.realpathSync(resolvedCandidate),
    );
  } catch {
    return false;
  }
}

function readStableConfigFile(
  repo: string,
  configPath: string,
  primaryConfigPath: string,
  inventory: RepoFileInventory | null,
  metadataBudget?: CompilerMetadataBudget,
): ConfigReadResult {
  return ts.readConfigFile(configPath, (fileName) =>
    readPermittedConfigFile(
      repo,
      fileName,
      primaryConfigPath,
      inventory,
      metadataBudget,
    ),
  );
}

function readPermittedConfigFile(
  repo: string,
  fileName: string,
  primaryConfigPath: string,
  inventory: RepoFileInventory | null,
  metadataBudget?: CompilerMetadataBudget,
): string | undefined {
  return (
    readStableFile(
      fileName,
      (resolvedPath, realPath) =>
        isConfigFilePermitted(
          repo,
          resolvedPath,
          realPath,
          primaryConfigPath,
          inventory,
        ),
      (identity) => {
        if (identity.size > ANALYZER_METADATA_BYTE_LIMIT) {
          metadataBudget?.reportFileLimit(fileName, identity.size);
          return false;
        }
        return (
          metadataBudget?.reserveFile(fileName, identity.size) ??
          true
        );
      },
    )?.text ?? undefined
  );
}

function isConfigFilePermitted(
  repo: string,
  resolvedPath: string,
  realPath: string,
  primaryConfigPath: string,
  inventory: RepoFileInventory | null,
): boolean {
  const repoRoot = path.resolve(repo);
  let realRepoRoot: string;
  try {
    realRepoRoot = fs.realpathSync(repoRoot);
  } catch {
    return false;
  }
  if (
    !isInsideRepo(repoRoot, resolvedPath) ||
    !isInsideRepo(realRepoRoot, realPath)
  ) {
    return false;
  }
  if (canonicalPathKey(resolvedPath) === canonicalPathKey(primaryConfigPath)) {
    return true;
  }
  if (!inventory) return true;
  if (!inventory.manifestBounded) return true;

  const resolvedKey = canonicalPathKey(resolvedPath);
  const realKey = canonicalPathKey(realPath);
  if (
    (inventory.configPathKeys.has(resolvedKey) &&
      inventory.configPathKeys.has(realKey)) ||
    (inventory.packagePathKeys.has(resolvedKey) &&
      inventory.packagePathKeys.has(realKey))
  ) {
    return true;
  }
  return pathContainsNodeModules(realRepoRoot, realPath);
}

function markConfigurationPartial(
  inventory: RepoFileInventory | null,
  warnings: string[],
): void {
  if (!inventory) return;
  const reason =
    "TypeScript configuration could not be read completely; analysis context is partial.";
  inventory.configurationPartial = true;
  if (!warnings.includes(reason)) warnings.push(reason);
}

function createCompilerMetadataBudget(
  warnings: string[],
  inventory: RepoFileInventory | null,
): CompilerMetadataBudget {
  const budget = {
    retainedBytesByKey: new Map<string, number>(),
    lookupKeys: new Set<string>(),
    packageKeys: new Set<string>(),
    retainedBytes: 0,
    rejectionCount: 0,
  } as CompilerMetadataBudget;
  const reportLimit = (): void => {
    budget.rejectionCount += 1;
    if (inventory) inventory.configurationPartial = true;
    const reason =
      `TypeScript compiler metadata byte safety limit of ${ANALYZER_METADATA_BYTE_LIMIT} per file and in aggregate, ` +
      `or entry safety limit of ${ANALYZER_METADATA_FILE_LIMIT} files and ${ANALYZER_PACKAGE_INFO_LIMIT} package objects, was reached; ` +
      "configuration context is partial.";
    if (!warnings.includes(reason)) warnings.push(reason);
  };
  const reportExpansionLimit = (): void => {
    budget.rejectionCount += 1;
    if (inventory) inventory.configurationPartial = true;
    const reason =
      "TypeScript package export target expansion safety limit was reached; configuration context is partial.";
    if (!warnings.includes(reason)) warnings.push(reason);
  };
  budget.reserveLookup = (fileName) => {
    const key = canonicalPathKey(path.resolve(fileName));
    if (budget.lookupKeys.has(key)) return true;
    if (budget.lookupKeys.size >= ANALYZER_METADATA_FILE_LIMIT) {
      reportLimit();
      return false;
    }
    budget.lookupKeys.add(key);
    return true;
  };
  budget.reserveFile = (fileName, fileBytes) => {
    const key = canonicalPathKey(path.resolve(fileName));
    const previousBytes = budget.retainedBytesByKey.get(key);
    if (previousBytes !== undefined && fileBytes <= previousBytes) {
      return true;
    }
    if (!budget.reserveLookup(fileName)) return false;
    const additionalBytes = fileBytes - (previousBytes ?? 0);
    if (
      fileBytes > ANALYZER_METADATA_BYTE_LIMIT ||
      additionalBytes >
        ANALYZER_METADATA_BYTE_LIMIT - budget.retainedBytes
    ) {
      reportLimit();
      return false;
    }
    budget.retainedBytesByKey.set(key, fileBytes);
    budget.retainedBytes += additionalBytes;
    return true;
  };
  budget.reservePackage = (fileName) => {
    const key = canonicalPathKey(path.resolve(fileName));
    if (budget.packageKeys.has(key)) return true;
    if (budget.packageKeys.size >= ANALYZER_PACKAGE_INFO_LIMIT) {
      reportLimit();
      return false;
    }
    budget.packageKeys.add(key);
    return true;
  };
  budget.reportFileLimit = () => {
    reportLimit();
  };
  budget.reportExpansionLimit = () => {
    reportExpansionLimit();
  };
  return budget;
}

function createInventoryParseConfigHost(
  repo: string,
  primaryConfigPath: string,
  inventory: RepoFileInventory,
  shouldStop: () => boolean,
  metadataBudget?: CompilerMetadataBudget,
): ts.ParseConfigHost {
  const readCache = new Map<string, string | undefined>();
  const readFile = (fileName: string): string | undefined => {
    if (shouldStop()) return undefined;
    const key = canonicalPathKey(fileName);
    if (!readCache.has(key)) {
      if (metadataBudget && !metadataBudget.reserveLookup(fileName)) {
        readCache.set(key, undefined);
        return undefined;
      }
      const text = readPermittedConfigFile(
        repo,
        fileName,
        primaryConfigPath,
        inventory,
        metadataBudget,
      );
      const normalizedText =
        text === undefined
          ? undefined
          : normalizeTsConfigFileText(
              repo,
              fileName,
              text,
              inventory,
              shouldStop,
              metadataBudget,
            );
      const retainedText =
        normalizedText === undefined ||
        !metadataBudget ||
        metadataBudget.reserveFile(
          fileName,
          Buffer.byteLength(normalizedText, "utf8"),
        )
          ? normalizedText
          : undefined;
      readCache.set(
        key,
        retainedText,
      );
    }
    return readCache.get(key);
  };
  return {
    useCaseSensitiveFileNames: ts.sys.useCaseSensitiveFileNames,
    fileExists: (fileName) => readFile(fileName) !== undefined,
    readFile,
    readDirectory: (
      rootDir,
      extensions,
      excludes,
      includes,
      depth,
    ) =>
      inventoryReadDirectory(
        repo,
        inventory,
        rootDir,
        extensions,
        excludes,
        includes,
        depth,
        shouldStop,
      ),
  };
}

export function normalizeTsConfigFileText(
  repo: string,
  configPath: string,
  text: string,
  inventory: RepoFileInventory | null = null,
  shouldStop: () => boolean = () => false,
  metadataBudget?: CompilerMetadataBudget,
): string {
  if (
    shouldStop() ||
    path.basename(configPath).toLowerCase() === "package.json"
  ) {
    return text;
  }
  const parsed = ts.parseConfigFileTextToJson(configPath, text);
  if (parsed.error || !isRecord(parsed.config)) return text;
  const normalized = normalizeTsConfigExtends(
    repo,
    configPath,
    parsed.config,
    inventory,
    shouldStop,
    metadataBudget,
  );
  return normalized === parsed.config ? text : JSON.stringify(normalized);
}

function inventoryReadDirectory(
  repo: string,
  inventory: RepoFileInventory,
  rootDir: string,
  extensions: readonly string[],
  excludes: readonly string[] | undefined,
  includes: readonly string[],
  depth: number | undefined,
  shouldStop: () => boolean,
): string[] {
  const matcher = ts as unknown as TypeScriptFileMatcherApi;
  const patterns = matcher.getFileMatcherPatterns(
    rootDir,
    excludes,
    includes,
    ts.sys.useCaseSensitiveFileNames,
    repo,
  );
  const flags = ts.sys.useCaseSensitiveFileNames ? "" : "i";
  const includePatterns = patterns.includeFilePatterns?.map(
    (pattern) => new RegExp(pattern, flags),
  );
  const excludePattern = patterns.excludePattern
    ? new RegExp(patterns.excludePattern, flags)
    : null;
  const comparableExtensions = extensions.map((extension) =>
    ts.sys.useCaseSensitiveFileNames
      ? extension
      : extension.toLowerCase(),
  );
  const files: string[] = [];

  for (const absPath of inventory.absPaths) {
    if (shouldStop()) break;
    const normalized = normalizeRelPath(path.resolve(absPath));
    const comparable = ts.sys.useCaseSensitiveFileNames
      ? normalized
      : normalized.toLowerCase();
    if (
      comparableExtensions.length > 0 &&
      !comparableExtensions.some((extension) =>
        comparable.endsWith(extension),
      )
    ) {
      continue;
    }
    if (excludePattern?.test(normalized)) continue;
    if (
      includePatterns &&
      !includePatterns.some((pattern) => pattern.test(normalized))
    ) {
      continue;
    }
    if (depth !== undefined) {
      const relative = normalizeRelPath(path.relative(rootDir, absPath));
      if (
        relative === ".." ||
        relative.startsWith("../") ||
        relative.split("/").length - 1 > depth
      ) {
        continue;
      }
    }
    files.push(path.resolve(absPath));
  }
  return files.sort();
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
  shouldStop: () => boolean,
  sourceBudget: ProgramSourceBudget,
  metadataBudget: CompilerMetadataBudget,
): ts.Program {
  if (configPath) {
    const readResult =
      configReadResult ??
      readStableConfigFile(
        repo,
        configPath,
        configPath,
        inventory,
        metadataBudget,
      );
    if (readResult.error) {
      warnings.push(formatDiagnostic(readResult.error));
      markConfigurationPartial(inventory, warnings);
    } else {
      const parsed = ts.parseJsonConfigFileContent(
        normalizeTsConfigExtends(
          repo,
          configPath,
          readResult.config,
          inventory,
          shouldStop,
          metadataBudget,
        ),
        inventory
          ? createInventoryParseConfigHost(
              repo,
              configPath,
              inventory,
              shouldStop,
              metadataBudget,
            )
          : ts.sys,
        path.dirname(configPath),
      );
      if (parsed.errors.length > 0) {
        warnings.push(...parsed.errors.map(formatDiagnostic));
        markConfigurationPartial(inventory, warnings);
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
          {
            isEligible: (fileName) =>
              isEligibleSupplementalDeclarationRoot(repo, fileName),
            markPartial: () => {
              if (inventory) inventory.partial = true;
            },
            shouldStop,
          },
        );
        warnings.push(
          `Large TypeScript change set (${focusedProgramFileCount} files); using focused program roots to keep analysis bounded.`,
        );
        return createInventoryBoundedProgram(
          repo,
          uniquePaths([...changedRootNames, ...selectedDeclarationRoots]),
          reviewOptions,
          inventory,
          shouldStop,
          sourceBudget,
          createProgramSourceScope(sourceBudget, changedRootNames),
        );
      }
      return createInventoryBoundedProgram(
        repo,
        uniquePaths([...changedRootNames, ...permittedConfigRootNames]),
        reviewOptions,
        inventory,
        shouldStop,
        sourceBudget,
        createProgramSourceScope(sourceBudget, changedRootNames),
      );
    }
  }

  const changedRootNames = changed.map((file) => path.resolve(repo, file));
  const selectedDeclarationRoots = selectSupplementalDeclarationRoots(
    noConfigDeclarationRoots ?? inventory?.declarationAbsPaths ?? [],
    changedRootNames,
    warnings,
    {
      isEligible: (fileName) =>
        isEligibleSupplementalDeclarationRoot(repo, fileName),
      markPartial: () => {
        if (inventory) inventory.partial = true;
      },
      shouldStop,
    },
  );
  warnings.push(
    selectedDeclarationRoots.length
      ? `No tsconfig.json or jsconfig.json found; using changed files and ${selectedDeclarationRoots.length} permitted declaration root(s).`
      : "No tsconfig.json or jsconfig.json found; using changed files only.",
  );
  const options = {
    allowJs: true,
    checkJs: false,
    jsx: ts.JsxEmit.ReactJSX,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Node10,
    target: ts.ScriptTarget.ES2022,
  };
  return createInventoryBoundedProgram(
    repo,
    uniquePaths([...changedRootNames, ...selectedDeclarationRoots]),
    options,
    inventory,
    shouldStop,
    sourceBudget,
    createProgramSourceScope(sourceBudget, changedRootNames),
  );
}

function createInventoryBoundedProgram(
  repo: string,
  rootNames: string[],
  options: ts.CompilerOptions,
  inventory: RepoFileInventory | null,
  shouldStop: () => boolean,
  sourceBudget: ProgramSourceBudget,
  sourceScope: ProgramSourceScope,
): ts.Program {
  const host = ts.createCompilerHost(options);
  const defaultLibraryRoot = path.dirname(
    path.resolve(ts.getDefaultLibFilePath(options)),
  );
  const defaultLibraryRoots = [
    defaultLibraryRoot,
    fs.realpathSync(defaultLibraryRoot),
  ];
  const repoRoots = [
    path.resolve(repo),
    fs.realpathSync(repo),
  ];
  const isPermitted = (resolvedPath: string, realPath: string): boolean =>
    inventory === null ||
    isCompilerFilePermitted(
      repoRoots,
      resolvedPath,
      realPath,
      inventory,
      defaultLibraryRoots,
    );
  host.readFile = (fileName) => {
    if (shouldStop()) return undefined;
    return readStableFile(
      fileName,
      isPermitted,
      (identity) =>
        reserveProgramSourceFile(
          fileName,
          identity.size,
          sourceBudget,
          sourceScope,
          defaultLibraryRoots,
        ),
    )?.text ?? undefined;
  };
  host.getSourceFile = (
    fileName,
    languageVersionOrOptions,
    _onError,
    _shouldCreateNewSourceFile,
  ) => {
    if (shouldStop()) return undefined;
    const snapshot = readStableFile(
      fileName,
      isPermitted,
      (identity) =>
        reserveProgramSourceFile(
          fileName,
          identity.size,
          sourceBudget,
          sourceScope,
          defaultLibraryRoots,
        ),
    );
    if (snapshot?.text === null || snapshot?.text === undefined) return undefined;
    return ts.createSourceFile(
      fileName,
      snapshot.text,
      languageVersionOrOptions,
      false,
    );
  };
  return ts.createProgram({ rootNames, options, host });
}

function createProgramSourceBudget(
  warnings: string[],
  inventory: RepoFileInventory | null,
): ProgramSourceBudget {
  return {
    acceptedFileInstances: 0,
    acceptedBytes: 0,
    reservedChangedFileBytesByKey: new Map(),
    claimedReservedChangedKeys: new Set(),
    warningEmitted: false,
    warnings,
    inventory,
  };
}

function createProgramSourceScope(
  _budget: ProgramSourceBudget,
  changedRootNames: string[],
): ProgramSourceScope {
  return {
    acceptedFileBytesByKey: new Map(),
    changedFileKeys: new Set(
      changedRootNames.map((fileName) =>
        canonicalPathKey(path.resolve(fileName)),
      ),
    ),
  };
}

function reserveChangedProgramSources(
  repo: string,
  changed: string[],
  budget: ProgramSourceBudget,
  inventory: RepoFileInventory | null,
): void {
  const repoRoot = path.resolve(repo);
  let realRepoRoot: string;
  try {
    realRepoRoot = fs.realpathSync(repoRoot);
  } catch {
    return;
  }
  const isPermitted = (resolvedPath: string, realPath: string): boolean =>
    isInsideRepo(repoRoot, resolvedPath) &&
    isInsideRepo(realRepoRoot, realPath) &&
    (
      inventory === null ||
      (
        inventory.pathKeys.has(canonicalPathKey(resolvedPath)) &&
        inventory.pathKeys.has(canonicalPathKey(realPath))
      )
    );
  for (const changedFile of changed) {
    const absPath = path.resolve(repoRoot, changedFile);
    const fileKey = canonicalPathKey(absPath);
    if (budget.reservedChangedFileBytesByKey.has(fileKey)) continue;
    const inspection = inspectStableFile(absPath, isPermitted);
    if (!inspection) continue;
    if (!reserveProgramSourceInstance(inspection.identity.size, budget)) {
      continue;
    }
    budget.reservedChangedFileBytesByKey.set(
      fileKey,
      inspection.identity.size,
    );
  }
}

function reserveProgramSourceFile(
  fileName: string,
  fileBytes: number,
  budget: ProgramSourceBudget,
  scope: ProgramSourceScope,
  defaultLibraryRoots: string[],
): boolean {
  const resolvedPath = path.resolve(fileName);
  const fileKey = canonicalPathKey(resolvedPath);
  if (
    defaultLibraryRoots.some((libraryRoot) =>
      isSameOrInsideRepo(libraryRoot, resolvedPath),
    )
  ) {
    return true;
  }
  const previouslyAcceptedBytes = scope.acceptedFileBytesByKey.get(fileKey);
  if (
    previouslyAcceptedBytes !== undefined &&
    fileBytes <= previouslyAcceptedBytes
  ) {
    return true;
  }
  if (previouslyAcceptedBytes !== undefined) {
    const additionalBytes = fileBytes - previouslyAcceptedBytes;
    if (
      additionalBytes >
      ANALYZER_SOURCE_BYTE_LIMIT - budget.acceptedBytes
    ) {
      markProgramSourceBudgetReached(budget);
      return false;
    }
    budget.acceptedBytes += additionalBytes;
    scope.acceptedFileBytesByKey.set(fileKey, fileBytes);
    return true;
  }
  const reservedChangedBytes =
    scope.changedFileKeys.has(fileKey) &&
    !budget.claimedReservedChangedKeys.has(fileKey)
      ? budget.reservedChangedFileBytesByKey.get(fileKey)
      : undefined;
  if (reservedChangedBytes !== undefined) {
    const additionalBytes = Math.max(0, fileBytes - reservedChangedBytes);
    if (
      additionalBytes > 0 &&
      additionalBytes > ANALYZER_SOURCE_BYTE_LIMIT - budget.acceptedBytes
    ) {
      markProgramSourceBudgetReached(budget);
      return false;
    }
    budget.acceptedBytes += additionalBytes;
    budget.claimedReservedChangedKeys.add(fileKey);
    scope.acceptedFileBytesByKey.set(fileKey, fileBytes);
    return true;
  }

  if (!reserveProgramSourceInstance(fileBytes, budget)) {
    return false;
  }
  scope.acceptedFileBytesByKey.set(fileKey, fileBytes);
  return true;
}

function reserveProgramSourceInstance(
  fileBytes: number,
  budget: ProgramSourceBudget,
): boolean {
  if (
    budget.acceptedFileInstances >= ANALYZER_SOURCE_FILE_LIMIT ||
    fileBytes > ANALYZER_SOURCE_BYTE_LIMIT - budget.acceptedBytes
  ) {
    markProgramSourceBudgetReached(budget);
    return false;
  }
  budget.acceptedFileInstances += 1;
  budget.acceptedBytes += fileBytes;
  return true;
}

function markProgramSourceBudgetReached(
  budget: ProgramSourceBudget,
): void {
  if (budget.warningEmitted) return;
  budget.warningEmitted = true;
  if (budget.inventory) budget.inventory.partial = true;
  budget.warnings.push(
    `TypeScript program source budget reached the safety limit of ${ANALYZER_SOURCE_FILE_LIMIT} retained source instances or ${ANALYZER_SOURCE_BYTE_LIMIT} bytes across the analyzer shard; compiler context is partial.`,
  );
}

function isCompilerFilePermitted(
  repoRoots: string[],
  resolvedPath: string,
  realPath: string,
  inventory: RepoFileInventory,
  defaultLibraryRoots: string[],
): boolean {
  const realRepoRoot = repoRoots.find((repoRoot) =>
    isInsideRepo(repoRoot, realPath),
  );
  const resolvedKey = canonicalPathKey(resolvedPath);
  const realKey = canonicalPathKey(realPath);
  if (
    realRepoRoot &&
    (
      (inventory.pathKeys.has(resolvedKey) &&
        inventory.pathKeys.has(realKey)) ||
      (inventory.packagePathKeys.has(resolvedKey) &&
        inventory.packagePathKeys.has(realKey)) ||
      (inventory.configPathKeys.has(resolvedKey) &&
        inventory.configPathKeys.has(realKey))
    )
  ) {
    return true;
  }

  if (
    realRepoRoot &&
    repoRoots.some((repoRoot) =>
      pathContainsNodeModules(repoRoot, resolvedPath),
    ) &&
    (
      inventory.pathKeys.has(realKey) ||
      inventory.packagePathKeys.has(realKey) ||
      inventory.configPathKeys.has(realKey)
    )
  ) {
    return true;
  }

  if (
    realRepoRoot &&
    repoRoots.some((repoRoot) =>
      pathContainsNodeModules(repoRoot, realPath),
    )
  ) {
    return true;
  }

  return defaultLibraryRoots.some(
    (libraryRoot) =>
      isSameOrInsideRepo(libraryRoot, resolvedPath) ||
      isSameOrInsideRepo(libraryRoot, realPath),
  );
}

function pathContainsNodeModules(root: string, candidate: string): boolean {
  if (!isInsideRepo(root, candidate)) return false;
  return normalizeRelPath(path.relative(root, candidate))
    .split("/")
    .some((part) => part.toLowerCase() === "node_modules");
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
    const packageKey = canonicalPathKey(
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
      ...(rootsByPackage.get(canonicalPathKey(current)) ?? []),
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
      isInsideRepo(repoRoot, resolved) &&
      inventory.pathKeys.has(canonicalPathKey(resolved))
    );
  });
}

interface DeclarationRootCandidate {
  fileName: string;
  priority: number;
  lowerKey: string;
  resolvedKey: string;
}

export function selectSupplementalDeclarationRoots(
  declarationRoots: string[],
  changedRoots: string[],
  warnings: string[],
  options: SupplementalDeclarationSelectionOptions = {},
): string[] {
  const changedKeys = new Set(changedRoots.map(canonicalPathKey));
  const seenKeys = new Set<string>();
  const selected: DeclarationRootCandidate[] = [];
  let supplementalRootCount = 0;
  let ineligibleRootCount = 0;
  for (const fileName of declarationRoots) {
    if (options.shouldStop?.()) break;
    const resolvedKey = normalizeRelPath(path.resolve(fileName));
    const lowerKey = resolvedKey.toLowerCase();
    const canonicalKey = ts.sys.useCaseSensitiveFileNames
      ? resolvedKey
      : lowerKey;
    if (changedKeys.has(canonicalKey) || seenKeys.has(canonicalKey)) continue;
    seenKeys.add(canonicalKey);
    if (options.isEligible && !options.isEligible(fileName)) {
      ineligibleRootCount += 1;
      continue;
    }
    supplementalRootCount += 1;

    const candidate: DeclarationRootCandidate = {
      fileName,
      priority: ambientDeclarationPriority(fileName),
      lowerKey,
      resolvedKey,
    };
    const lastSelected = selected.at(-1);
    if (
      selected.length >= FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT &&
      lastSelected &&
      compareDeclarationRootCandidates(candidate, lastSelected) >= 0
    ) {
      continue;
    }
    insertDeclarationRootCandidate(selected, candidate);
    if (selected.length > FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT) {
      selected.pop();
    }
  }

  if (supplementalRootCount > FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT) {
    options.markPartial?.();
    warnings.push(
      `TypeScript declaration roots capped at ${FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT} of ${supplementalRootCount}; ambient declaration coverage is partial.`,
    );
  }
  if (ineligibleRootCount > 0) {
    options.markPartial?.();
    warnings.push(
      `TypeScript skipped ${ineligibleRootCount} supplemental declaration root${ineligibleRootCount === 1 ? "" : "s"} outside focused size/path policies; ambient declaration coverage is partial.`,
    );
  }
  return selected.map((candidate) => candidate.fileName);
}

function isEligibleSupplementalDeclarationRoot(
  repo: string,
  fileName: string,
): boolean {
  const repoRoot = path.resolve(repo);
  const resolvedPath = path.resolve(fileName);
  if (!isInsideRepo(repoRoot, resolvedPath)) return false;
  const relativeParts = normalizeRelPath(path.relative(repoRoot, resolvedPath))
    .split("/")
    .map((part) => part.toLowerCase());
  if (
    relativeParts.some((part) =>
      SUPPLEMENTAL_DECLARATION_EXCLUDED_DIRECTORIES.has(part),
    )
  ) {
    return false;
  }
  const baseName = path.basename(resolvedPath).toLowerCase();
  if (
    baseName.includes(".generated.") ||
    baseName.includes(".min.") ||
    baseName.startsWith("generated.")
  ) {
    return false;
  }
  try {
    const fileStat = fs.lstatSync(resolvedPath);
    return (
      fileStat.isFile() &&
      !fileStat.isSymbolicLink() &&
      fileStat.size <= SUPPLEMENTAL_DECLARATION_ROOT_BYTE_LIMIT
    );
  } catch {
    return false;
  }
}

function insertDeclarationRootCandidate(
  selected: DeclarationRootCandidate[],
  candidate: DeclarationRootCandidate,
): void {
  let low = 0;
  let high = selected.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (compareDeclarationRootCandidates(candidate, selected[middle]) < 0) {
      high = middle;
    } else {
      low = middle + 1;
    }
  }
  selected.splice(low, 0, candidate);
}

function compareDeclarationRootCandidates(
  left: DeclarationRootCandidate,
  right: DeclarationRootCandidate,
): number {
  if (left.priority !== right.priority) return left.priority - right.priority;
  if (left.lowerKey < right.lowerKey) return -1;
  if (left.lowerKey > right.lowerKey) return 1;
  if (left.resolvedKey < right.resolvedKey) return -1;
  if (left.resolvedKey > right.resolvedKey) return 1;
  return 0;
}

function ambientDeclarationPriority(fileName: string): number {
  const declarationStem = path.basename(fileName).replace(/\.d\.(?:ts|mts|cts)$/i, "");
  return /(?:^|[._-])(?:global|globals|env)(?:$|[._-])/i.test(declarationStem) ? 0 : 1;
}

export function normalizeTsConfigExtends(
  repo: string,
  configPath: string,
  config: unknown,
  inventory: RepoFileInventory | null = null,
  shouldStop: () => boolean = () => false,
  metadataBudget?: CompilerMetadataBudget,
): unknown {
  if (!isRecord(config)) return config;
  const activeBudget =
    metadataBudget ?? createCompilerMetadataBudget([], inventory);
  const extendsValue = config.extends;
  if (typeof extendsValue === "string") {
    const resolvedExtends = resolveTsConfigExtends(
      repo,
      configPath,
      extendsValue,
      inventory,
      shouldStop,
      activeBudget,
    );
    if (resolvedExtends === extendsValue) return config;
    return {
      ...config,
      extends: resolvedExtends,
    };
  }
  if (Array.isArray(extendsValue)) {
    let changed = false;
    const resolvedExtends = extendsValue.map((value) => {
      if (typeof value !== "string") return value;
      const resolved = resolveTsConfigExtends(
        repo,
        configPath,
        value,
        inventory,
        shouldStop,
        activeBudget,
      );
      if (resolved !== value) changed = true;
      return resolved;
    });
    if (!changed) return config;
    return {
      ...config,
      extends: resolvedExtends,
    };
  }
  return config;
}

function resolveTsConfigExtends(
  repo: string,
  _configPath: string,
  extendsValue: string,
  inventory: RepoFileInventory | null,
  shouldStop: () => boolean,
  metadataBudget?: CompilerMetadataBudget,
): string {
  if (extendsValue.startsWith(".") || path.isAbsolute(extendsValue)) return extendsValue;

  const parsed = parsePackageSpecifier(extendsValue);
  if (!parsed) return extendsValue;

  const packageInfo = findWorkspacePackageInfo(
    repo,
    parsed.packageName,
    inventory,
    shouldStop,
    metadataBudget,
  );
  if (!packageInfo) return extendsValue;

  if (parsed.subpath) {
    if (
      packageInfo.exports !== undefined &&
      packageInfo.exports !== null
    ) {
      const expansionBudget = createBoundedExpansionBudget();
      const targets = packageExportTargetsForKey(
        packageInfo.exports,
        `./${parsed.subpath}`,
        expansionBudget,
      );
      if (expansionBudget.limited) {
        metadataBudget?.reportExpansionLimit();
      }
      return (
        resolvePackageConfigTarget(
          repo,
          packageInfo,
          targets,
          inventory,
          true,
        ) ?? extendsValue
      );
    }
    return (
      resolvePackageConfigTarget(
        repo,
        packageInfo,
        [parsed.subpath],
        inventory,
      ) ?? extendsValue
    );
  }
  if (packageInfo.tsconfig) {
    return (
      resolvePackageConfigTarget(
        repo,
        packageInfo,
        [packageInfo.tsconfig],
        inventory,
      ) ?? extendsValue
    );
  }
  if (
    packageInfo.exports !== undefined &&
    packageInfo.exports !== null
  ) {
    const expansionBudget = createBoundedExpansionBudget();
    const targets = packageExportTargetsForKey(
      packageInfo.exports,
      ".",
      expansionBudget,
    );
    if (expansionBudget.limited) {
      metadataBudget?.reportExpansionLimit();
    }
    return (
      resolvePackageConfigTarget(
        repo,
        packageInfo,
        targets,
        inventory,
        true,
      ) ?? extendsValue
    );
  }
  return (
    resolvePackageConfigTarget(
      repo,
      packageInfo,
      ["tsconfig.json"],
      inventory,
    ) ?? extendsValue
  );
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

function resolvePackageConfigTarget(
  repo: string,
  packageInfo: PackageInfo,
  targets: string[],
  inventory: RepoFileInventory | null,
  requirePackageExportTarget = false,
): string | null {
  const packageRoot = path.resolve(packageInfo.root);
  let realPackageRoot: string;
  try {
    realPackageRoot = fs.realpathSync(packageRoot);
  } catch {
    return null;
  }

  for (const target of targets) {
    if (
      requirePackageExportTarget &&
      !target.startsWith("./")
    ) {
      continue;
    }
    const resolvedTarget = path.resolve(packageRoot, target);
    const candidates = path.extname(resolvedTarget)
      ? [resolvedTarget]
      : [resolvedTarget, `${resolvedTarget}.json`];
    for (const candidate of candidates) {
      if (
        !isSameOrInsideRepo(packageRoot, candidate) ||
        !isSafeRepoConfigFile(repo, candidate)
      ) {
        continue;
      }
      let realCandidate: string;
      try {
        realCandidate = fs.realpathSync(candidate);
      } catch {
        continue;
      }
      if (!isSameOrInsideRepo(realPackageRoot, realCandidate)) continue;
      if (inventory?.manifestBounded) {
        const resolvedKey = canonicalPathKey(candidate);
        const realKey = canonicalPathKey(realCandidate);
        if (
          !inventory.configPathKeys.has(resolvedKey) ||
          !inventory.configPathKeys.has(realKey)
        ) {
          continue;
        }
      }
      return candidate;
    }
  }
  return null;
}

function findWorkspacePackageInfo(
  repo: string,
  packageName: string,
  inventory: RepoFileInventory | null,
  shouldStop: () => boolean,
  metadataBudget?: CompilerMetadataBudget,
): PackageInfo | null {
  const repoRoot = path.resolve(repo);
  const activeBudget =
    metadataBudget ?? createCompilerMetadataBudget([], inventory);
  if (inventory) {
    const cachedIndex = boundedWorkspacePackageIndexCache.get(inventory);
    if (cachedIndex) {
      for (const retainedPackage of cachedIndex.retainedPackages) {
        if (
          !activeBudget.reserveFile(
            retainedPackage.fileName,
            retainedPackage.fileBytes,
          ) ||
          !activeBudget.reservePackage(retainedPackage.fileName)
        ) {
          return null;
        }
      }
      return cachedIndex.packages.get(packageName) ?? null;
    }
    {
      const realRepoRoot = fs.realpathSync(repoRoot);
      const builtIndex = new Map<string, PackageInfo>();
      const retainedPackages: BoundedWorkspacePackageIndex["retainedPackages"] =
        [];
      let complete = true;
      for (const packageJsonPath of inventory.packageJsonAbsPaths) {
        if (shouldStop()) {
          complete = false;
          break;
        }
        const rejectionCount = activeBudget.rejectionCount;
        let retainedFileBytes = 0;
        const packageInfo = readPackageInfo(
          path.dirname(packageJsonPath),
          packageJsonPath,
          (resolvedPath, realPath) =>
            isInsideRepo(repoRoot, resolvedPath) &&
            isInsideRepo(realRepoRoot, realPath) &&
            inventory.packagePathKeys.has(canonicalPathKey(resolvedPath)) &&
            inventory.packagePathKeys.has(canonicalPathKey(realPath)),
          activeBudget.reportFileLimit,
          (fileName, fileBytes) => {
            const accepted = activeBudget.reserveFile(
              fileName,
              fileBytes,
            );
            if (accepted) retainedFileBytes = fileBytes;
            return accepted;
          },
          activeBudget.reservePackage,
        );
        if (activeBudget.rejectionCount !== rejectionCount) {
          complete = false;
          break;
        }
        if (packageInfo && !builtIndex.has(packageInfo.name)) {
          builtIndex.set(packageInfo.name, packageInfo);
          retainedPackages.push({
            fileName: packageJsonPath,
            fileBytes: retainedFileBytes,
          });
        }
      }
      if (complete) {
        boundedWorkspacePackageIndexCache.set(inventory, {
          packages: builtIndex,
          retainedPackages,
        });
      }
      return builtIndex.get(packageName) ?? null;
    }
  }

  const cacheKey = `${repoRoot}\0${packageName}`;
  const cachedPackage = workspacePackageInfoCache.get(cacheKey);
  if (cachedPackage) {
    if (
      cachedPackage.fileName &&
      (
        !activeBudget.reserveFile(
          cachedPackage.fileName,
          cachedPackage.retainedBytes,
        ) ||
        !activeBudget.reservePackage(cachedPackage.fileName)
      )
    ) {
      return null;
    }
    workspacePackageInfoCache.delete(cacheKey);
    workspacePackageInfoCache.set(cacheKey, cachedPackage);
    return cachedPackage.packageInfo;
  }
  let found: PackageInfo | null = null;
  let foundFileName: string | null = null;
  let foundFileBytes = 0;
  let complete = true;
  walk(repoRoot, (absPath) => {
    if (
      found ||
      path.basename(absPath) !== "package.json"
    ) {
      return;
    }
    if (shouldStop()) {
      complete = false;
      return;
    }
    const rejectionCount = activeBudget.rejectionCount;
    const packageInfo = readPackageInfo(
      path.dirname(absPath),
      absPath,
      undefined,
      activeBudget.reportFileLimit,
      (fileName, fileBytes) => {
        const accepted = activeBudget.reserveFile(fileName, fileBytes);
        if (accepted) foundFileBytes = fileBytes;
        return accepted;
      },
      activeBudget.reservePackage,
    );
    if (activeBudget.rejectionCount !== rejectionCount) {
      complete = false;
      return;
    }
    if (packageInfo?.name === packageName) {
      found = packageInfo;
      foundFileName = absPath;
    }
  });
  if (complete) {
    cacheWorkspacePackageInfo(
      cacheKey,
      found,
      foundFileName,
      found ? foundFileBytes : 0,
    );
  }
  return found;
}

function cacheWorkspacePackageInfo(
  cacheKey: string,
  packageInfo: PackageInfo | null,
  fileName: string | null,
  retainedBytes: number,
): void {
  const existing = workspacePackageInfoCache.get(cacheKey);
  if (existing) {
    workspacePackageInfoCacheBytes -= existing.retainedBytes;
    workspacePackageInfoCache.delete(cacheKey);
  }
  while (
    workspacePackageInfoCache.size >= ANALYZER_PACKAGE_INFO_LIMIT ||
    retainedBytes >
      ANALYZER_METADATA_BYTE_LIMIT - workspacePackageInfoCacheBytes
  ) {
    const oldestKey = workspacePackageInfoCache.keys().next().value;
    if (typeof oldestKey !== "string") break;
    const evicted = workspacePackageInfoCache.get(oldestKey);
    workspacePackageInfoCache.delete(oldestKey);
    workspacePackageInfoCacheBytes -= evicted?.retainedBytes ?? 0;
  }
  if (
    workspacePackageInfoCache.size >= ANALYZER_PACKAGE_INFO_LIMIT ||
    retainedBytes >
      ANALYZER_METADATA_BYTE_LIMIT - workspacePackageInfoCacheBytes
  ) {
    return;
  }
  workspacePackageInfoCache.set(cacheKey, {
    packageInfo,
    fileName,
    retainedBytes,
  });
  workspacePackageInfoCacheBytes += retainedBytes;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
