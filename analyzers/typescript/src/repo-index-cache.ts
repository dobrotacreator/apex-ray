import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { REPO_INDEX_CACHE_FILE, REPO_INDEX_CACHE_VERSION } from "./constants.js";
import type {
  ClassHeritageIndexEntry,
  DefaultImportIndexEntry,
  DiInjectionIndexEntry,
  DiProviderIndexEntry,
  ExportIndexEntry,
  IdentifierIndexEntry,
  ImportIndexEntry,
  NamedImportIndexEntry,
  NamespaceImportIndexEntry,
  ReceiverIndexEntry,
  Reference,
  RepoFileIndexEntry,
  RepoIndexCacheFile,
  RepoIndexCacheFileEntry,
  TypeAliasIndexEntry,
} from "./types.js";
import { isRecord } from "./utils.js";

export function repoIndexCachePath(repo: string, cacheDir: string | null): string {
  const resolvedCacheDir = cacheDir ? path.resolve(repo, cacheDir) : defaultRepoIndexCacheDir(repo);
  return path.join(resolvedCacheDir, REPO_INDEX_CACHE_FILE);
}

function defaultRepoIndexCacheDir(repo: string): string {
  const repoHash = crypto.createHash("sha256").update(path.resolve(repo)).digest("hex").slice(0, 16);
  return path.join(defaultCacheHome(), "repos", repoHash, "typescript");
}

function defaultCacheHome(): string {
  const explicit = process.env.APEX_RAY_CACHE_HOME;
  if (explicit && explicit.trim()) return path.resolve(explicit);
  const xdg = process.env.XDG_CACHE_HOME;
  if (xdg && xdg.trim()) return path.join(path.resolve(xdg), "apex-ray");
  const home = os.homedir();
  if (home && home.trim()) return path.join(home, ".cache", "apex-ray");
  return path.join(os.tmpdir(), "apex-ray-cache");
}

export function readRepoIndexCache(cachePath: string): RepoIndexCacheFile | null {
  try {
    const parsed = JSON.parse(fs.readFileSync(cachePath, "utf-8")) as RepoIndexCacheFile;
    if (parsed.version !== REPO_INDEX_CACHE_VERSION || !Array.isArray(parsed.files)) return null;
    if (!parsed.files.every(isRepoIndexCacheFileEntry)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function isRepoIndexCacheFileEntry(value: unknown): value is RepoIndexCacheFileEntry {
  if (!isRecord(value)) return false;
  return (
    typeof value.relPath === "string" &&
    typeof value.size === "number" &&
    typeof value.mtimeMs === "number" &&
    Array.isArray(value.imports) &&
    value.imports.every(isImportIndexEntry) &&
    Array.isArray(value.exports) &&
    value.exports.every(isExportIndexEntry) &&
    Array.isArray(value.identifiers) &&
    value.identifiers.every(isIdentifierIndexEntry) &&
    Array.isArray(value.receivers) &&
    value.receivers.every(isReceiverIndexEntry) &&
    Array.isArray(value.typeAliases) &&
    value.typeAliases.every(isTypeAliasIndexEntry) &&
    Array.isArray(value.classHeritages) &&
    value.classHeritages.every(isClassHeritageIndexEntry) &&
    Array.isArray(value.diProviders) &&
    value.diProviders.every(isDiProviderIndexEntry) &&
    Array.isArray(value.diInjections) &&
    value.diInjections.every(isDiInjectionIndexEntry)
  );
}

function isImportIndexEntry(value: unknown): value is ImportIndexEntry {
  return (
    isRecord(value) &&
    typeof value.moduleSpecifier === "string" &&
    (value.defaultImport === null || isDefaultImportIndexEntry(value.defaultImport)) &&
    (value.namespaceImport === null || isNamespaceImportIndexEntry(value.namespaceImport)) &&
    Array.isArray(value.namedImports) &&
    value.namedImports.every(isNamedImportIndexEntry)
  );
}

function isDefaultImportIndexEntry(value: unknown): value is DefaultImportIndexEntry {
  return isRecord(value) && typeof value.localName === "string" && isReference(value.reference);
}

function isNamespaceImportIndexEntry(value: unknown): value is NamespaceImportIndexEntry {
  return isRecord(value) && typeof value.localName === "string" && isReference(value.reference);
}

function isNamedImportIndexEntry(value: unknown): value is NamedImportIndexEntry {
  return (
    isRecord(value) &&
    typeof value.importedName === "string" &&
    typeof value.localName === "string" &&
    isReference(value.reference)
  );
}

function isExportIndexEntry(value: unknown): value is ExportIndexEntry {
  return (
    isRecord(value) &&
    (typeof value.moduleSpecifier === "string" || value.moduleSpecifier === null) &&
    typeof value.localName === "string" &&
    typeof value.exportedName === "string" &&
    isReference(value.reference)
  );
}

function isIdentifierIndexEntry(value: unknown): value is IdentifierIndexEntry {
  return (
    isRecord(value) &&
    typeof value.name === "string" &&
    (typeof value.namespaceQualifier === "string" || value.namespaceQualifier === null) &&
    isReference(value.reference)
  );
}

function isReceiverIndexEntry(value: unknown): value is ReceiverIndexEntry {
  return (
    isRecord(value) &&
    typeof value.receiverName === "string" &&
    (typeof value.typeName === "string" || value.typeName === null) &&
    typeof value.startLine === "number" &&
    typeof value.endLine === "number" &&
    isReference(value.reference)
  );
}

function isTypeAliasIndexEntry(value: unknown): value is TypeAliasIndexEntry {
  return isRecord(value) && typeof value.name === "string" && typeof value.targetName === "string";
}

function isClassHeritageIndexEntry(value: unknown): value is ClassHeritageIndexEntry {
  return (
    isRecord(value) &&
    typeof value.className === "string" &&
    Array.isArray(value.baseNames) &&
    value.baseNames.every((name) => typeof name === "string")
  );
}

function isDiProviderIndexEntry(value: unknown): value is DiProviderIndexEntry {
  return (
    isRecord(value) &&
    typeof value.tokenName === "string" &&
    typeof value.implementationName === "string" &&
    isReference(value.reference) &&
    (value.sourceArrayName === undefined || typeof value.sourceArrayName === "string")
  );
}

function isDiInjectionIndexEntry(value: unknown): value is DiInjectionIndexEntry {
  return isRecord(value) && typeof value.tokenName === "string" && isReference(value.reference);
}

function isReference(value: unknown): value is Reference {
  return (
    isRecord(value) &&
    typeof value.file === "string" &&
    typeof value.line === "number" &&
    (value.endLine === undefined || typeof value.endLine === "number") &&
    typeof value.text === "string" &&
    typeof value.kind === "string"
  );
}

export function writeRepoIndexCache(cachePath: string, files: RepoFileIndexEntry[]): boolean {
  try {
    fs.mkdirSync(path.dirname(cachePath), { recursive: true });
    const tmpPath = `${cachePath}.${process.pid}.${Date.now()}.tmp`;
    const payload: RepoIndexCacheFile = {
      version: REPO_INDEX_CACHE_VERSION,
      files: files.map((file) => ({
        relPath: file.relPath,
        size: file.size,
        mtimeMs: file.mtimeMs,
        imports: file.imports,
        exports: file.exports,
        identifiers: file.identifiers,
        receivers: file.receivers,
        typeAliases: file.typeAliases,
        classHeritages: file.classHeritages,
        diProviders: file.diProviders,
        diInjections: file.diInjections,
      })),
    };
    fs.writeFileSync(tmpPath, JSON.stringify(payload), "utf-8");
    fs.renameSync(tmpPath, cachePath);
    return true;
  } catch {
    return false;
  }
}
