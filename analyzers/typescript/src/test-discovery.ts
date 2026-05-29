import fs from "node:fs";
import path from "node:path";

import ts from "typescript";

import { propertyAssignmentNamed, unwrapExpression } from "./ast-utils.js";
import {
  findIndexedPackageForFile,
  isModuleSpecifierRelatedToPath,
  moduleSpecifierCandidatePaths,
} from "./module-resolution.js";
import type {
  PackageInfo,
  Reference,
  RelatedTestCandidate,
  RepoFileIndexEntry,
  RepoIndex,
  VitestTestConfig,
} from "./types.js";
import {
  isRepoRelativePath,
  isSameOrInsideRepo,
  normalizeRelPath,
  readUtf8,
  scriptKindForPath,
  uniquePaths,
} from "./utils.js";

const vitestConfigCache = new Map<string, VitestTestConfig | null>();
const vitestConfigPathsCache = new Map<string, string[]>();

export function findRelatedTests(
  repo: string,
  repoIndex: RepoIndex,
  changedFile: string,
  references: Reference[] = [],
): string[] {
  const candidates = new Map<string, RelatedTestCandidate>();
  const parsed = path.posix.parse(changedFile);
  const base = parsed.name;
  const changedPath = path.resolve(repo, changedFile);
  const changedPackage = findIndexedPackageForFile(repo, repoIndex, changedPath);
  const referencedSourceFiles = uniquePaths(
    references
      .filter(
        (reference) =>
          reference.kind !== "import" &&
          reference.file !== changedFile &&
          !isTestPath(reference.file.toLowerCase()),
      )
      .map((reference) => reference.file),
  );
  const referencedTestFiles = uniquePaths(
    references
      .filter(
        (reference) =>
          reference.kind !== "import" &&
          reference.file !== changedFile &&
          isTestPath(reference.file.toLowerCase()) &&
          isRunnableTestPath(repo, reference.file),
      )
      .map((reference) => reference.file),
  );
  const testPatterns = [
    `${base}.test`,
    `${base}.spec`,
    `${base}_test`,
    `${base}.tests`,
  ];

  for (const entry of repoIndex.files) {
    if (!isTestPath(entry.relLower)) continue;
    if (!isRunnableTestPath(repo, entry.relPath)) continue;
    if (testPatterns.some((pattern) => entry.relLower.includes(pattern.toLowerCase()))) {
      addCandidate(entry.relPath, relatedTestPriority("direct", entry.relPath, changedFile));
    }
    if (importsChangedFile(entry, repo, changedPath, changedPackage)) {
      addCandidate(entry.relPath, relatedTestPriority("changed-import", entry.relPath, changedFile));
    }
  }

  for (const referencedTestFile of referencedTestFiles) {
    addCandidate(referencedTestFile, relatedTestPriority("reference-test", referencedTestFile, changedFile));
  }

  for (const referencedFile of referencedSourceFiles) {
    const referencedPath = path.resolve(repo, referencedFile);
    const referencedPackage = findIndexedPackageForFile(repo, repoIndex, referencedPath);
    for (const entry of repoIndex.files) {
      if (!isTestPath(entry.relLower)) continue;
      if (!isRunnableTestPath(repo, entry.relPath)) continue;
      if (importsChangedFile(entry, repo, referencedPath, referencedPackage)) {
        addCandidate(entry.relPath, relatedTestPriority("reference-import", entry.relPath, referencedFile));
      }
    }
  }

  return [...candidates.values()]
    .sort((left, right) => left.priority - right.priority || left.relPath.localeCompare(right.relPath))
    .slice(0, 10)
    .map((candidate) => candidate.relPath);

  function addCandidate(relPath: string, priority: number): void {
    const existing = candidates.get(relPath);
    if (existing && existing.priority <= priority) return;
    candidates.set(relPath, { relPath, priority });
  }
}

export function isTestPath(relLower: string): boolean {
  return /(^|\/)(test|tests|__tests__|spec)(\/|$)|(\.|_)(test|spec)\./.test(relLower);
}

function relatedTestPriority(
  source: "direct" | "reference-test" | "changed-import" | "reference-import",
  testPath: string,
  targetPath: string,
): number {
  const sourcePriority =
    source === "direct" ? 0 : source === "reference-test" ? 500 : source === "changed-import" ? 1000 : 2000;
  return sourcePriority + pathDirectoryDistance(testPath, targetPath);
}

function pathDirectoryDistance(leftPath: string, rightPath: string): number {
  const left = path.posix.dirname(normalizeRelPath(leftPath)).split("/").filter(Boolean);
  const right = path.posix.dirname(normalizeRelPath(rightPath)).split("/").filter(Boolean);
  let common = 0;
  while (common < left.length && common < right.length && left[common] === right[common]) {
    common += 1;
  }
  return left.length - common + right.length - common;
}

function isRunnableTestPath(repo: string, relPath: string): boolean {
  if (!isTestPath(relPath.toLowerCase())) return false;

  const absPath = path.resolve(repo, relPath);
  const configs = vitestConfigsForFile(repo, absPath);
  if (configs.length === 0) return true;

  return configs.some((config) => matchesVitestConfig(config, absPath));
}

function matchesVitestConfig(config: VitestTestConfig, absPath: string): boolean {
  const relToConfig = normalizeRelPath(path.relative(config.root, absPath));
  if (!isRepoRelativePath(relToConfig)) return false;
  if (config.include.length > 0 && !config.include.some((pattern) => pattern.test(relToConfig))) return false;
  return !config.exclude.some((pattern) => pattern.test(relToConfig));
}

function vitestConfigsForFile(repo: string, filePath: string): VitestTestConfig[] {
  return findNearestVitestConfigPaths(repo, filePath)
    .map((configPath) => readCachedVitestTestConfig(configPath))
    .filter((config): config is VitestTestConfig => config !== null);
}

function readCachedVitestTestConfig(configPath: string): VitestTestConfig | null {
  if (!vitestConfigCache.has(configPath)) {
    vitestConfigCache.set(configPath, readVitestTestConfig(configPath));
  }
  return vitestConfigCache.get(configPath) ?? null;
}

function findNearestVitestConfigPaths(repo: string, filePath: string): string[] {
  const repoRoot = path.resolve(repo);
  let current = path.dirname(path.resolve(filePath));

  while (isSameOrInsideRepo(repoRoot, current)) {
    const cacheKey = normalizeRelPath(current);
    if (!vitestConfigPathsCache.has(cacheKey)) {
      const paths = fs
        .readdirSync(current, { withFileTypes: true })
        .filter((entry) => entry.isFile() && isVitestConfigName(entry.name))
        .map((entry) => path.join(current, entry.name))
        .sort();
      vitestConfigPathsCache.set(cacheKey, paths);
    }
    const paths = vitestConfigPathsCache.get(cacheKey) ?? [];
    if (paths.length > 0) {
      return paths;
    }
    if (current === repoRoot) return [];
    const parent = path.dirname(current);
    if (parent === current) return [];
    current = parent;
  }
  return [];
}

function isVitestConfigName(name: string): boolean {
  return /^vitest(?:\.[^.]+)?\.config\.(?:ts|mts|cts|js|mjs|cjs)$/.test(name);
}

function readVitestTestConfig(configPath: string): VitestTestConfig | null {
  const text = readUtf8(configPath);
  if (text === null) return null;

  const source = ts.createSourceFile(configPath, text, ts.ScriptTarget.ES2022, true, scriptKindForPath(configPath));
  const rootConfig = findVitestRootConfigObject(source);
  const testConfig = rootConfig ? objectPropertyValue(rootConfig, "test") : null;
  if (!testConfig || !ts.isObjectLiteralExpression(testConfig)) return null;

  return {
    root: path.dirname(configPath),
    include: stringArrayProperty(testConfig, "include").map(globToRegExp),
    exclude: stringArrayProperty(testConfig, "exclude").map(globToRegExp),
  };
}

function findVitestRootConfigObject(source: ts.SourceFile): ts.ObjectLiteralExpression | null {
  for (const statement of source.statements) {
    if (ts.isExportAssignment(statement)) {
      const expression = unwrapExpression(statement.expression);
      const configObject = vitestConfigObjectFromExpression(expression);
      if (configObject) return configObject;
    }
    if (ts.isExpressionStatement(statement)) {
      const expression = unwrapExpression(statement.expression);
      const configObject = vitestConfigObjectFromExpression(expression);
      if (configObject) return configObject;
    }
  }
  return null;
}

function vitestConfigObjectFromExpression(expression: ts.Expression | null): ts.ObjectLiteralExpression | null {
  if (!expression) return null;
  if (ts.isObjectLiteralExpression(expression)) return expression;
  if (!ts.isCallExpression(expression)) return null;

  const [firstArgument] = expression.arguments;
  const unwrapped = unwrapExpression(firstArgument);
  return unwrapped && ts.isObjectLiteralExpression(unwrapped) ? unwrapped : null;
}

function stringArrayProperty(object: ts.ObjectLiteralExpression, name: string): string[] {
  const value = objectPropertyValue(object, name);
  if (!value || !ts.isArrayLiteralExpression(value)) return [];

  const strings: string[] = [];
  for (const element of value.elements) {
    if (ts.isStringLiteral(element) || ts.isNoSubstitutionTemplateLiteral(element)) {
      strings.push(element.text);
    }
  }
  return strings;
}

function objectPropertyValue(object: ts.ObjectLiteralExpression, name: string): ts.Expression | null {
  const property = propertyAssignmentNamed(object, name);
  return property ? unwrapExpression(property.initializer) : null;
}

function globToRegExp(pattern: string): RegExp {
  pattern = normalizeRelPath(pattern);
  let expression = "^";
  for (let index = 0; index < pattern.length; index += 1) {
    const char = pattern[index];
    if (char === "*") {
      if (pattern[index + 1] === "*") {
        index += 1;
        if (pattern[index + 1] === "/") {
          index += 1;
          expression += "(?:.*/)?";
        } else {
          expression += ".*";
        }
      } else {
        expression += "[^/]*";
      }
      continue;
    }
    if (char === "?") {
      expression += "[^/]";
      continue;
    }
    if (char === "{") {
      const closeIndex = pattern.indexOf("}", index + 1);
      if (closeIndex !== -1) {
        const alternatives = pattern
          .slice(index + 1, closeIndex)
          .split(",")
          .map((part) => escapeRegex(part));
        expression += `(?:${alternatives.join("|")})`;
        index = closeIndex;
        continue;
      }
    }
    expression += escapeRegex(char);
  }
  return new RegExp(`${expression}$`);
}

function escapeRegex(value: string): string {
  return value.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
}

function importsChangedFile(
  entry: RepoFileIndexEntry,
  repo: string,
  changedPath: string,
  changedPackage: PackageInfo | null,
): boolean {
  const normalizedChangedPath = normalizeRelPath(path.resolve(changedPath));
  return entry.imports.some((importEntry) =>
    isModuleSpecifierRelatedToPath(importEntry.moduleSpecifier, entry.absPath, changedPath, changedPackage) ||
    moduleSpecifierCandidatePaths(importEntry.moduleSpecifier, entry.absPath, repo, changedPackage).some(
      (candidate) => candidate === normalizedChangedPath,
    ),
  );
}
