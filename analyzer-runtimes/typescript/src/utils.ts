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
    const resolved = canonicalPathKey(value);
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

export function canonicalPathKey(value: string): string {
  const normalized = normalizeRelPath(path.resolve(value));
  return ts.sys.useCaseSensitiveFileNames ? normalized : normalized.toLowerCase();
}

export function sourceFileName(source: ts.SourceFile): string {
  return normalizeRelPath(source.fileName);
}

export function readUtf8(filePath: string): string | null {
  try {
    return decodeTypeScriptFileBuffer(fs.readFileSync(filePath));
  } catch {
    return null;
  }
}

export interface StableFileIdentity {
  dev: number;
  ino: number;
  size: number;
  mtimeMs: number;
  ctimeMs: number;
}

export interface StableFileSnapshot {
  identity: StableFileIdentity;
  realPath: string;
  text: string | null;
}

export interface StableFileInspection {
  identity: StableFileIdentity;
  realPath: string;
}

export type StableFilePermission = (resolvedPath: string, realPath: string) => boolean;
type StableFileReadDecision = (identity: StableFileIdentity) => boolean;

export function readStableFile(
  filePath: string,
  isPermitted: StableFilePermission = () => true,
  shouldRead: StableFileReadDecision = () => true,
): StableFileSnapshot | null {
  const resolvedPath = path.resolve(filePath);
  let descriptor: number | null = null;
  try {
    const initialPathStat = fs.lstatSync(resolvedPath);
    if (!initialPathStat.isFile() || initialPathStat.isSymbolicLink()) return null;
    const initialRealPath = fs.realpathSync(resolvedPath);
    if (!isPermitted(resolvedPath, initialRealPath)) return null;

    const noFollow = typeof fs.constants.O_NOFOLLOW === "number"
      ? fs.constants.O_NOFOLLOW
      : 0;
    descriptor = fs.openSync(resolvedPath, fs.constants.O_RDONLY | noFollow);
    const openedStat = fs.fstatSync(descriptor);
    const openedIdentity = stableFileIdentity(openedStat);
    if (
      !openedStat.isFile() ||
      !sameStableFileIdentity(stableFileIdentity(initialPathStat), openedIdentity)
    ) {
      return null;
    }

    const text = shouldRead(openedIdentity)
      ? decodeTypeScriptFileBuffer(fs.readFileSync(descriptor))
      : null;
    const finalDescriptorStat = fs.fstatSync(descriptor);
    const finalPathStat = fs.lstatSync(resolvedPath);
    if (!finalPathStat.isFile() || finalPathStat.isSymbolicLink()) return null;
    const finalRealPath = fs.realpathSync(resolvedPath);
    if (
      !sameStableFileIdentity(openedIdentity, stableFileIdentity(finalDescriptorStat)) ||
      !sameStableFileIdentity(openedIdentity, stableFileIdentity(finalPathStat)) ||
      canonicalPathKey(initialRealPath) !== canonicalPathKey(finalRealPath) ||
      !isPermitted(resolvedPath, finalRealPath)
    ) {
      return null;
    }
    return { identity: openedIdentity, realPath: finalRealPath, text };
  } catch {
    return null;
  } finally {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor);
      } catch {
        // A failed close invalidates no already-read bytes, and callers never
        // retain the descriptor.
      }
    }
  }
}

export function inspectStableFile(
  filePath: string,
  isPermitted: StableFilePermission = () => true,
): StableFileInspection | null {
  const resolvedPath = path.resolve(filePath);
  try {
    const initialPathStat = fs.lstatSync(resolvedPath);
    if (!initialPathStat.isFile() || initialPathStat.isSymbolicLink()) return null;
    const initialIdentity = stableFileIdentity(initialPathStat);
    const realPath = fs.realpathSync(resolvedPath);
    if (!isPermitted(resolvedPath, realPath)) return null;
    const finalPathStat = fs.lstatSync(resolvedPath);
    if (
      !finalPathStat.isFile() ||
      finalPathStat.isSymbolicLink() ||
      !sameStableFileIdentity(
        initialIdentity,
        stableFileIdentity(finalPathStat),
      )
    ) {
      return null;
    }
    return { identity: initialIdentity, realPath };
  } catch {
    return null;
  }
}

function decodeTypeScriptFileBuffer(buffer: Buffer): string {
  let length = buffer.length;
  if (length >= 2 && buffer[0] === 0xfe && buffer[1] === 0xff) {
    length &= ~1;
    for (let index = 0; index < length; index += 2) {
      const first = buffer[index];
      buffer[index] = buffer[index + 1];
      buffer[index + 1] = first;
    }
    return buffer.toString("utf16le", 2);
  }
  if (length >= 2 && buffer[0] === 0xff && buffer[1] === 0xfe) {
    return buffer.toString("utf16le", 2);
  }
  if (
    length >= 3 &&
    buffer[0] === 0xef &&
    buffer[1] === 0xbb &&
    buffer[2] === 0xbf
  ) {
    return buffer.toString("utf8", 3);
  }
  return buffer.toString("utf8");
}

export function readStableUtf8File(
  filePath: string,
  isPermitted: StableFilePermission = () => true,
): StableFileSnapshot | null {
  return readStableFile(filePath, isPermitted);
}

export function stableFileIdentity(stat: fs.Stats): StableFileIdentity {
  return {
    dev: stat.dev,
    ino: stat.ino,
    size: stat.size,
    mtimeMs: stat.mtimeMs,
    ctimeMs: stat.ctimeMs,
  };
}

export function sameStableFileIdentity(
  left: StableFileIdentity,
  right: StableFileIdentity,
): boolean {
  return (
    left.dev === right.dev &&
    left.ino === right.ino &&
    left.size === right.size &&
    left.mtimeMs === right.mtimeMs &&
    left.ctimeMs === right.ctimeMs
  );
}

export function scriptKindForPath(filePath: string): ts.ScriptKind {
  if (/\.tsx$/i.test(filePath)) return ts.ScriptKind.TSX;
  if (/\.jsx$/i.test(filePath)) return ts.ScriptKind.JSX;
  if (/\.[cm]?js$/i.test(filePath)) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

export function isTypeScriptOrJavaScriptFileName(filePath: string): boolean {
  return /\.(?:[cm]?[jt]s|[jt]sx)$/i.test(normalizeRelPath(filePath));
}

export function isDeclarationFileName(filePath: string): boolean {
  return /\.d\.(?:ts|mts|cts)$/i.test(normalizeRelPath(filePath));
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
