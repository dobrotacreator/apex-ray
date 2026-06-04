import fs from "node:fs";
import path from "node:path";

import ts from "typescript";

import { FOCUSED_PROGRAM_CHANGED_FILE_THRESHOLD } from "./constants.js";
import { readPackageInfo } from "./package-info.js";
import type { Args, ProgramContext } from "./types.js";
import {
  formatDiagnostic,
  isInsideRepo,
  normalizeRelPath,
  uniquePaths,
  walk,
} from "./utils.js";

const workspacePackageRootCache = new Map<string, string | null>();

export function createProgramContexts(args: Args, warnings: string[]): Map<string, ProgramContext> {
  const groups = new Map<string, { tsconfigPath: string | null; changedFiles: string[] }>();
  for (const changedFile of args.changed) {
    const tsconfigPath = findNearestConfig(args.repo, changedFile);
    const key = tsconfigPath ?? "<no-tsconfig>";
    const group = groups.get(key) ?? { tsconfigPath, changedFiles: [] };
    group.changedFiles.push(changedFile);
    groups.set(key, group);
  }

  const contextsByFile = new Map<string, ProgramContext>();
  for (const group of groups.values()) {
    const program = createProgram(
      args.repo,
      group.tsconfigPath,
      group.changedFiles,
      warnings,
      args.largeChangeSetSize,
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
  changed: string[],
  warnings: string[],
  largeChangeSetSize: number | null,
): ts.Program {
  if (configPath) {
    const readResult = ts.readConfigFile(configPath, ts.sys.readFile);
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
      const focusedProgramFileCount = largeChangeSetSize ?? changedRootNames.length;
      if (
        changedRootNames.length >= FOCUSED_PROGRAM_CHANGED_FILE_THRESHOLD ||
        focusedProgramFileCount >= FOCUSED_PROGRAM_CHANGED_FILE_THRESHOLD
      ) {
        warnings.push(
          `Large TypeScript change set (${focusedProgramFileCount} files); using focused program roots to keep analysis bounded.`,
        );
        return ts.createProgram({
          rootNames: uniquePaths(changedRootNames),
          options: parsed.options,
        });
      }
      return ts.createProgram({
        rootNames: uniquePaths([...parsed.fileNames, ...changedRootNames]),
        options: parsed.options,
      });
    }
  }

  warnings.push("No tsconfig.json or jsconfig.json found; using changed files only.");
  return ts.createProgram({
    rootNames: changed.map((file) => path.resolve(repo, file)),
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
