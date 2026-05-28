import fs from "node:fs";
import path from "node:path";

import ts from "typescript";

import { IGNORED_DIRECTORY_NAMES } from "./constants.js";

export function walk(root: string, onFile: (path: string) => void): void {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (isIgnoredDirectory(entry.name)) continue;
    const absPath = path.join(root, entry.name);
    if (entry.isDirectory()) {
      walk(absPath, onFile);
    } else if (entry.isFile()) {
      onFile(absPath);
    }
  }
}

export function isIgnoredDirectory(name: string): boolean {
  return IGNORED_DIRECTORY_NAMES.has(name);
}

export function isInsideRepo(repo: string, candidate: string): boolean {
  return isRepoRelativePath(normalizeRelPath(path.relative(repo, candidate)));
}

export function isSameOrInsideRepo(repo: string, candidate: string): boolean {
  const relative = normalizeRelPath(path.relative(repo, candidate));
  return relative === "" || isRepoRelativePath(relative);
}

export function isRepoRelativePath(value: string): boolean {
  return value !== "" && value !== ".." && !value.startsWith("../") && !path.isAbsolute(value);
}

export function rangesOverlap(aStart: number, aEnd: number, bStart: number, bEnd: number): boolean {
  return aStart <= bEnd && bStart <= aEnd;
}

export function uniquePaths(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const resolved = normalizeRelPath(path.resolve(value));
    if (seen.has(resolved)) continue;
    seen.add(resolved);
    result.push(value);
  }
  return result;
}

export function formatDiagnostic(diagnostic: ts.Diagnostic): string {
  return ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n");
}

export function normalizeRelPath(value: string): string {
  return value.replaceAll("\\", "/");
}

export function sourceFileName(source: ts.SourceFile): string {
  return normalizeRelPath(source.fileName);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
