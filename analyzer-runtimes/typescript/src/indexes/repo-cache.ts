import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  ANALYZER_SOURCE_FILE_LIMIT,
  REPO_INDEX_CACHE_BYTE_LIMIT,
  REPO_INDEX_CACHE_FILE,
  REPO_INDEX_CACHE_VERSION,
  REPO_INDEX_SEMANTIC_ENTRY_LIMIT,
} from "../constants.js";
import type {
  ClassHeritageIndexEntry,
  DefaultImportIndexEntry,
  DiInjectionIndexEntry,
  DiProviderIndexEntry,
  ExportIndexEntry,
  IdentifierIndexEntry,
  IndexedReference,
  ImportIndexEntry,
  NamedImportIndexEntry,
  NamespaceImportIndexEntry,
  ReceiverIndexEntry,
  Reference,
  RepoFileIndexEntry,
  RepoIndexCacheFile,
  RepoIndexCacheFileEntry,
  RepoIndexCacheWriteResult,
  TypeAliasIndexEntry,
} from "../types.js";
import { isRecord, readStableFile } from "../utils.js";
import { semanticEntryCountForFile } from "./collection.js";

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

export function readRepoIndexCache(
  cachePath: string,
  _inventoryFingerprint: string | null = null,
  shouldStop: () => boolean = () => false,
): RepoIndexCacheFile | null {
  if (shouldStop()) return null;
  try {
    const snapshot = readStableFile(
      cachePath,
      () => true,
      (identity) =>
        !shouldStop() &&
        identity.size <= REPO_INDEX_CACHE_BYTE_LIMIT,
    );
    if (
      snapshot?.text === null ||
      snapshot?.text === undefined ||
      shouldStop()
    ) {
      return null;
    }
    if (
      cacheVersionBeforeParse(snapshot.text) !==
        REPO_INDEX_CACHE_VERSION ||
      cacheJsonExceedsEntrySafetyLimit(snapshot.text, shouldStop)
    ) {
      return null;
    }
    if (shouldStop()) return null;
    const parsed = JSON.parse(snapshot.text) as RepoIndexCacheFile;
    if (
      parsed.version !== REPO_INDEX_CACHE_VERSION ||
      !Array.isArray(parsed.files) ||
      parsed.files.length > ANALYZER_SOURCE_FILE_LIMIT
    ) {
      return null;
    }
    let semanticEntries = 0;
    for (const file of parsed.files) {
      if (
        shouldStop() ||
        !isRepoIndexCacheFileEntry(file, shouldStop)
      ) {
        return null;
      }
      const fileEntries = semanticEntryCountForFile(file);
      if (
        fileEntries >
        REPO_INDEX_SEMANTIC_ENTRY_LIMIT - semanticEntries
      ) {
        return null;
      }
      semanticEntries += fileEntries;
    }
    return parsed;
  } catch {
    return null;
  }
}

function cacheVersionBeforeParse(text: string): number | null {
  const match = /^\s*\{\s*"version"\s*:\s*(-?\d+)/u.exec(text);
  if (!match) return null;
  const version = Number(match[1]);
  return Number.isSafeInteger(version) ? version : null;
}

function cacheJsonExceedsEntrySafetyLimit(
  text: string,
  shouldStop: () => boolean,
): boolean {
  const stack: Array<{
    kind: "array" | "object";
    entries: number;
    expectingArrayValue: boolean;
    filesArray: boolean;
  }> = [];
  const aggregateArrayEntryLimit =
    ANALYZER_SOURCE_FILE_LIMIT + REPO_INDEX_SEMANTIC_ENTRY_LIMIT;
  let aggregateArrayEntries = 0;
  let inString = false;
  let escaped = false;
  let stringValue = "";
  let lastString: string | null = null;
  let pendingProperty: string | null = null;

  const startArrayValue = (): boolean => {
    const parent = stack.at(-1);
    if (parent?.kind !== "array" || !parent.expectingArrayValue) {
      return false;
    }
    parent.entries += 1;
    parent.expectingArrayValue = false;
    aggregateArrayEntries += 1;
    return (
      aggregateArrayEntries > aggregateArrayEntryLimit ||
      (parent.filesArray &&
        parent.entries > ANALYZER_SOURCE_FILE_LIMIT)
    );
  };

  for (let index = 0; index < text.length; index += 1) {
    if (index % 4_096 === 0 && shouldStop()) return true;
    const character = text[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === '"') {
        inString = false;
        lastString = stringValue;
      } else if (stringValue.length <= 16) {
        stringValue += character;
      }
      continue;
    }
    if (character === '"') {
      if (startArrayValue()) return true;
      inString = true;
      escaped = false;
      stringValue = "";
      continue;
    }
    if (
      character === " " ||
      character === "\n" ||
      character === "\r" ||
      character === "\t"
    ) {
      continue;
    }
    if (character === ":") {
      pendingProperty = lastString;
      continue;
    }
    if (character === ",") {
      const parent = stack.at(-1);
      if (parent?.kind === "array") {
        parent.expectingArrayValue = true;
      }
      pendingProperty = null;
      continue;
    }
    if (character === "[") {
      if (startArrayValue()) return true;
      const filesArray =
        stack.length === 1 &&
        stack[0]?.kind === "object" &&
        pendingProperty === "files";
      stack.push({
        kind: "array",
        entries: 0,
        expectingArrayValue: true,
        filesArray,
      });
      pendingProperty = null;
      if (stack.length > 128) return true;
      continue;
    }
    if (character === "{") {
      if (startArrayValue()) return true;
      stack.push({
        kind: "object",
        entries: 0,
        expectingArrayValue: false,
        filesArray: false,
      });
      pendingProperty = null;
      if (stack.length > 128) return true;
      continue;
    }
    if (character === "]" || character === "}") {
      stack.pop();
      pendingProperty = null;
      continue;
    }
    if (startArrayValue()) return true;
    pendingProperty = null;
  }
  return false;
}

function isRepoIndexCacheFileEntry(
  value: unknown,
  shouldStop: () => boolean,
): value is RepoIndexCacheFileEntry {
  if (!isRecord(value)) return false;
  return (
    typeof value.relPath === "string" &&
    typeof value.dev === "number" &&
    typeof value.ino === "number" &&
    typeof value.size === "number" &&
    typeof value.mtimeMs === "number" &&
    typeof value.ctimeMs === "number" &&
    Array.isArray(value.imports) &&
    everyCacheValue(
      value.imports,
      (entry): entry is ImportIndexEntry =>
        isImportIndexEntry(entry, shouldStop),
      shouldStop,
    ) &&
    Array.isArray(value.exports) &&
    everyCacheValue(value.exports, isExportIndexEntry, shouldStop) &&
    Array.isArray(value.identifiers) &&
    everyCacheValue(
      value.identifiers,
      isIdentifierIndexEntry,
      shouldStop,
    ) &&
    Array.isArray(value.receivers) &&
    everyCacheValue(value.receivers, isReceiverIndexEntry, shouldStop) &&
    Array.isArray(value.typeAliases) &&
    everyCacheValue(
      value.typeAliases,
      isTypeAliasIndexEntry,
      shouldStop,
    ) &&
    Array.isArray(value.classHeritages) &&
    everyCacheValue(
      value.classHeritages,
      (entry): entry is ClassHeritageIndexEntry =>
        isClassHeritageIndexEntry(entry, shouldStop),
      shouldStop,
    ) &&
    Array.isArray(value.diProviders) &&
    everyCacheValue(
      value.diProviders,
      isDiProviderIndexEntry,
      shouldStop,
    ) &&
    Array.isArray(value.diInjections) &&
    everyCacheValue(
      value.diInjections,
      isDiInjectionIndexEntry,
      shouldStop,
    )
  );
}

function everyCacheValue<T>(
  values: unknown[],
  predicate: (value: unknown) => value is T,
  shouldStop: () => boolean,
): values is T[] {
  for (const value of values) {
    if (shouldStop() || !predicate(value)) return false;
  }
  return true;
}

function isImportIndexEntry(
  value: unknown,
  shouldStop: () => boolean,
): value is ImportIndexEntry {
  return (
    isRecord(value) &&
    typeof value.moduleSpecifier === "string" &&
    (value.defaultImport === null || isDefaultImportIndexEntry(value.defaultImport)) &&
    (value.namespaceImport === null || isNamespaceImportIndexEntry(value.namespaceImport)) &&
    Array.isArray(value.namedImports) &&
    everyCacheValue(
      value.namedImports,
      isNamedImportIndexEntry,
      shouldStop,
    )
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
    isIndexedReference(value.reference)
  );
}

function isIndexedReference(value: unknown): value is IndexedReference {
  return (
    isRecord(value) &&
    typeof value.file === "string" &&
    typeof value.line === "number" &&
    (value.endLine === undefined || typeof value.endLine === "number") &&
    typeof value.kind === "string"
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

function isClassHeritageIndexEntry(
  value: unknown,
  shouldStop: () => boolean,
): value is ClassHeritageIndexEntry {
  return (
    isRecord(value) &&
    typeof value.className === "string" &&
    Array.isArray(value.baseNames) &&
    everyCacheValue(
      value.baseNames,
      (name): name is string => typeof name === "string",
      shouldStop,
    )
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

export function writeRepoIndexCache(
  cachePath: string,
  files: RepoFileIndexEntry[],
  inventoryFingerprint: string | null = null,
  shouldStop: () => boolean = () => false,
): RepoIndexCacheWriteResult {
  let tmpPath: string | null = null;
  let descriptor: number | null = null;
  try {
    if (
      shouldStop() ||
      files.length > ANALYZER_SOURCE_FILE_LIMIT ||
      exceedsSemanticEntryLimit(files, shouldStop)
    ) {
      return { written: false, error: null, limited: true };
    }
    fs.mkdirSync(path.dirname(cachePath), { recursive: true });
    tmpPath = `${cachePath}.${process.pid}.${Date.now()}.tmp`;
    descriptor = fs.openSync(tmpPath, "wx", 0o600);
    const prefix =
      `{"version":${REPO_INDEX_CACHE_VERSION},` +
      `"inventoryFingerprint":${JSON.stringify(inventoryFingerprint)},` +
      '"files":[';
    const suffix = "]}";
    let writtenBytes =
      Buffer.byteLength(prefix, "utf8") +
      Buffer.byteLength(suffix, "utf8");
    if (writtenBytes > REPO_INDEX_CACHE_BYTE_LIMIT) {
      return { written: false, error: null, limited: true };
    }
    writeUtf8(descriptor, prefix);
    for (const [index, file] of files.entries()) {
      if (shouldStop()) {
        return { written: false, error: null, limited: true };
      }
      const cacheEntry: RepoIndexCacheFileEntry = {
        relPath: file.relPath,
        dev: file.dev,
        ino: file.ino,
        size: file.size,
        mtimeMs: file.mtimeMs,
        ctimeMs: file.ctimeMs,
        imports: file.imports,
        exports: file.exports,
        identifiers: file.identifiers,
        receivers: file.receivers,
        typeAliases: file.typeAliases,
        classHeritages: file.classHeritages,
        diProviders: file.diProviders,
        diInjections: file.diInjections,
      };
      const serialized = `${index === 0 ? "" : ","}${JSON.stringify(cacheEntry)}`;
      const serializedBytes = Buffer.byteLength(serialized, "utf8");
      if (
        serializedBytes >
        REPO_INDEX_CACHE_BYTE_LIMIT - writtenBytes
      ) {
        return { written: false, error: null, limited: true };
      }
      writeUtf8(descriptor, serialized);
      writtenBytes += serializedBytes;
    }
    writeUtf8(descriptor, suffix);
    fs.closeSync(descriptor);
    descriptor = null;
    fs.renameSync(tmpPath, cachePath);
    tmpPath = null;
    return { written: true, error: null };
  } catch (error) {
    return {
      written: false,
      error: error instanceof Error ? error.message : String(error),
    };
  } finally {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor);
      } catch {
        // Preserve the original write or limit result.
      }
    }
    if (tmpPath !== null) {
      try {
        fs.rmSync(tmpPath, { force: true });
      } catch {
        // Preserve the original write or limit result.
      }
    }
  }
}

function exceedsSemanticEntryLimit(
  files: RepoFileIndexEntry[],
  shouldStop: () => boolean,
): boolean {
  let semanticEntries = 0;
  for (const file of files) {
    if (shouldStop()) return true;
    const fileEntries = semanticEntryCountForFile(file);
    if (
      fileEntries >
      REPO_INDEX_SEMANTIC_ENTRY_LIMIT - semanticEntries
    ) {
      return true;
    }
    semanticEntries += fileEntries;
  }
  return false;
}

function writeUtf8(descriptor: number, text: string): void {
  const buffer = Buffer.from(text, "utf8");
  let offset = 0;
  while (offset < buffer.length) {
    const written = fs.writeSync(
      descriptor,
      buffer,
      offset,
      buffer.length - offset,
    );
    if (written <= 0) {
      throw new Error("repo index cache write made no progress");
    }
    offset += written;
  }
}
