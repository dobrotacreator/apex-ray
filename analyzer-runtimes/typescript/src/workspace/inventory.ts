import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import type { Args } from "../types.js";
import {
  canonicalPathKey,
  isDeclarationFileName,
  isIgnoredDirectory,
  isInsideRepo,
  isRecord,
  isSameOrInsideRepo,
  isTypeScriptOrJavaScriptFileName,
  normalizeRelPath,
} from "../utils.js";

const FALLBACK_INVENTORY_ENTRY_LIMIT = 250_000;
const FALLBACK_INVENTORY_PATH_BYTE_LIMIT = 16 * 1024 * 1024;
const INVENTORY_FILE_LIMIT = 50_000;
const MANIFEST_BYTE_LIMIT = 16 * 1024 * 1024;

export interface RepoFileInventory {
  absPaths: string[];
  declarationAbsPaths: string[];
  packageJsonAbsPaths: string[];
  packagePathKeys: Set<string>;
  configJsonAbsPaths: string[];
  configPathKeys: Set<string>;
  pathKeys: Set<string>;
  fingerprint: string | null;
  partial: boolean;
  partialReason: string | null;
  configurationPartial: boolean;
  manifestBounded: boolean;
}

export interface RepoFileInventoryOptions {
  shouldStop?: () => boolean;
  maxEntries?: number;
  maxFiles?: number;
  maxPathBytes?: number;
  maxManifestBytes?: number;
  openDirectory?: (directoryPath: string) => fs.Dir;
}

type RepoPathValidationStatus = "safe" | "missing" | "unsafe";

interface RepoPathValidation {
  absPath: string;
  realPath: string | null;
  status: RepoPathValidationStatus;
}

type RepoPathValidationCache = Map<string, RepoPathValidation>;

interface ValidatedChangedPaths {
  absPathsByKey: Map<string, string>;
  retainedPathBytes: number;
  partialReason: string | null;
}

type ManifestReadResult =
  | { status: "ok"; text: string }
  | { status: "byte-limit" }
  | { status: "stopped" };

export function loadRepoFileInventory(
  args: Args,
  options: RepoFileInventoryOptions = {},
): RepoFileInventory {
  const realRepoPath = fs.realpathSync(args.repo);
  const validationCache: RepoPathValidationCache = new Map();
  const shouldStop = options.shouldStop ?? (() => false);
  const maxFiles = positiveLimit(
    options.maxFiles,
    INVENTORY_FILE_LIMIT,
  );
  const maxChangedValidationEntries = Math.min(
    maxFiles,
    positiveLimit(
      options.maxEntries,
      FALLBACK_INVENTORY_ENTRY_LIMIT,
    ),
  );
  const maxChangedPathBytes = positiveLimit(
    options.maxPathBytes,
    FALLBACK_INVENTORY_PATH_BYTE_LIMIT,
  );
  const changed = validatedChangedAbsPaths(
    args,
    realRepoPath,
    validationCache,
    maxFiles,
    maxChangedPathBytes,
    maxChangedValidationEntries,
    shouldStop,
  );
  if (!args.fileManifestPath) {
    return loadFallbackRepoFileInventory(
      args,
      options,
      changed,
      realRepoPath,
      validationCache,
    );
  }
  return loadManifestRepoFileInventory(
    args,
    options,
    changed,
    realRepoPath,
    validationCache,
  );
}

function loadManifestRepoFileInventory(
  args: Args,
  options: RepoFileInventoryOptions,
  changed: ValidatedChangedPaths,
  realRepoPath: string,
  validationCache: RepoPathValidationCache,
): RepoFileInventory {
  const manifestPath = args.fileManifestPath;
  if (!manifestPath) throw new Error("TypeScript file manifest path is required.");
  const shouldStop = options.shouldStop ?? (() => false);
  if (shouldStop()) {
    return partialInventory(
      changed.absPathsByKey.values(),
      combinePartialReasons(
        changed.partialReason,
        "TypeScript manifest inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
      ),
      validationCache,
    );
  }
  const maxManifestBytes = positiveLimit(
    options.maxManifestBytes,
    MANIFEST_BYTE_LIMIT,
  );
  const maxFiles = positiveLimit(options.maxFiles, INVENTORY_FILE_LIMIT);
  let manifestText: string;
  let parsed: unknown;
  try {
    const readResult = readBoundedManifestFile(
      manifestPath,
      maxManifestBytes,
      shouldStop,
    );
    if (readResult.status === "byte-limit") {
      return partialInventory(
        changed.absPathsByKey.values(),
        combinePartialReasons(
          changed.partialReason,
          `TypeScript file manifest reached the manifest byte safety limit of ${maxManifestBytes}; repository context is partial.`,
        ),
        validationCache,
      );
    }
    if (readResult.status === "stopped") {
      return partialInventory(
        changed.absPathsByKey.values(),
        combinePartialReasons(
          changed.partialReason,
          "TypeScript manifest inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
        ),
        validationCache,
      );
    }
    manifestText = readResult.text;
    if (shouldStop()) {
      return partialInventory(
        changed.absPathsByKey.values(),
        combinePartialReasons(
          changed.partialReason,
          "TypeScript manifest inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
        ),
        validationCache,
      );
    }
    if (manifestExceedsEntrySafetyLimit(manifestText, maxFiles)) {
      return partialInventory(
        changed.absPathsByKey.values(),
        combinePartialReasons(
          changed.partialReason,
          `TypeScript file manifest reached the manifest entry safety limit of ${maxFiles} source files per array before parsing; repository context is partial.`,
        ),
        validationCache,
      );
    }
    parsed = JSON.parse(manifestText);
  } catch (error) {
    throw new Error(
      `Could not read TypeScript file manifest ${manifestPath}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
  if (
    !isRecord(parsed) ||
    parsed.version !== 2 ||
    !Array.isArray(parsed.files) ||
    (parsed.package_files !== undefined &&
      !Array.isArray(parsed.package_files)) ||
    (parsed.config_files !== undefined &&
      !Array.isArray(parsed.config_files)) ||
    (parsed.partial_reason !== undefined &&
      (typeof parsed.partial_reason !== "string" ||
        parsed.partial_reason.trim().length === 0))
  ) {
    throw new Error(`Invalid TypeScript file manifest: ${manifestPath}`);
  }

  const relPathSet = new Set<string>();
  const absPathsByKey = changed.absPathsByKey;
  const packagePathsByKey = new Map<string, string>();
  const configPathsByKey = new Map<string, string>();
  let partialReason: string | null = changed.partialReason;
  // Version-two producers may truncate before launching Node so the manifest
  // itself stays within this consumer's entry and byte safety bounds.
  if (typeof parsed.partial_reason === "string") {
    partialReason = combinePartialReasons(
      partialReason,
      parsed.partial_reason,
    );
  }
  for (const value of parsed.files) {
    if (shouldStop()) {
      partialReason = combinePartialReasons(
        partialReason,
        "TypeScript manifest inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
      );
      break;
    }
    if (typeof value !== "string") {
      throw new Error(`Invalid TypeScript file manifest: ${manifestPath}`);
    }
    const relPath = normalizeRelPath(value);
    const relPathKey = canonicalPathKey(path.resolve(args.repo, relPath));
    if (relPathSet.has(relPathKey)) continue;
    if (relPathSet.size >= maxFiles) {
      partialReason = combinePartialReasons(
        partialReason,
        `TypeScript manifest inventory scan reached the safety limit of ${maxFiles} source files; ` +
          "repository context is partial.",
      );
      break;
    }
    relPathSet.add(relPathKey);
    const validation = validateRepoRelativePath(
      args.repo,
      realRepoPath,
      relPath,
      "TypeScript file manifest path",
      validationCache,
    );
    if (validation.status !== "safe") {
      partialReason ??= validationPartialReason(
        "TypeScript file manifest path",
        relPath,
        validation.status,
      );
      continue;
    }
    const absPathKey = canonicalPathKey(validation.absPath);
    if (absPathsByKey.has(absPathKey)) continue;
    if (absPathsByKey.size >= maxFiles) {
      partialReason = combinePartialReasons(
        partialReason,
        `TypeScript manifest inventory scan reached the safety limit of ${maxFiles} source files; ` +
          "repository context is partial.",
      );
      break;
    }
    absPathsByKey.set(absPathKey, validation.absPath);
  }
  const packageFiles = Array.isArray(parsed.package_files)
    ? parsed.package_files
    : [];
  for (const value of packageFiles) {
    if (shouldStop()) {
      partialReason = combinePartialReasons(
        partialReason,
        "TypeScript manifest inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
      );
      break;
    }
    if (typeof value !== "string") {
      throw new Error(`Invalid TypeScript file manifest: ${manifestPath}`);
    }
    const relPath = normalizeRelPath(value);
    if (path.posix.basename(relPath) !== "package.json") {
      throw new Error(`Invalid TypeScript file manifest: ${manifestPath}`);
    }
    const validation = validateRepoRelativePath(
      args.repo,
      realRepoPath,
      relPath,
      "TypeScript package manifest path",
      validationCache,
    );
    if (validation.status !== "safe") {
      partialReason ??= validationPartialReason(
        "TypeScript package manifest path",
        relPath,
        validation.status,
      );
      continue;
    }
    const absPath = validation.absPath;
    const pathKey = canonicalPathKey(absPath);
    if (packagePathsByKey.has(pathKey)) continue;
    if (packagePathsByKey.size >= maxFiles) {
      partialReason ??=
        `TypeScript manifest inventory scan reached the safety limit of ${maxFiles} package files; ` +
        "repository context is partial.";
      break;
    }
    packagePathsByKey.set(pathKey, absPath);
  }
  const configFiles = Array.isArray(parsed.config_files)
    ? parsed.config_files
    : [];
  for (const value of configFiles) {
    if (shouldStop()) {
      partialReason = combinePartialReasons(
        partialReason,
        "TypeScript manifest inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
      );
      break;
    }
    if (typeof value !== "string") {
      throw new Error(`Invalid TypeScript file manifest: ${manifestPath}`);
    }
    const relPath = normalizeRelPath(value);
    const validation = validateRepoRelativePath(
      args.repo,
      realRepoPath,
      relPath,
      "TypeScript config manifest path",
      validationCache,
    );
    if (validation.status !== "safe") {
      partialReason ??= validationPartialReason(
        "TypeScript config manifest path",
        relPath,
        validation.status,
      );
      continue;
    }
    const absPath = validation.absPath;
    const pathKey = canonicalPathKey(absPath);
    if (configPathsByKey.has(pathKey)) continue;
    if (configPathsByKey.size >= maxFiles) {
      partialReason ??=
        `TypeScript manifest inventory scan reached the safety limit of ${maxFiles} config files; ` +
        "repository context is partial.";
      break;
    }
    configPathsByKey.set(pathKey, absPath);
  }
  const absPaths = [...absPathsByKey.values()].sort();
  const packageJsonAbsPaths = [...packagePathsByKey.values()].sort();
  const configJsonAbsPaths = [...configPathsByKey.values()].sort();
  const fingerprint = partialReason
    ? null
    : crypto
        .createHash("sha256")
        .update(
          [
            ...absPaths.map(
              (fileName) =>
                `source:${normalizeRelPath(path.relative(args.repo, fileName))}`,
            ),
            ...packageJsonAbsPaths.map(
              (fileName) =>
                `package:${normalizeRelPath(path.relative(args.repo, fileName))}`,
            ),
            ...configJsonAbsPaths.map(
              (fileName) =>
                `config:${normalizeRelPath(path.relative(args.repo, fileName))}`,
            ),
          ]
            .sort()
            .join("\0"),
        )
        .digest("hex");
  return inventoryFromAbsPaths(
    absPaths,
    fingerprint,
    partialReason,
    packageJsonAbsPaths,
    configJsonAbsPaths,
    validationCache,
    true,
  );
}

function loadFallbackRepoFileInventory(
  args: Args,
  options: RepoFileInventoryOptions,
  changed: ValidatedChangedPaths,
  realRepoPath: string,
  validationCache: RepoPathValidationCache,
): RepoFileInventory {
  const shouldStop = options.shouldStop ?? (() => false);
  const maxEntries = positiveLimit(options.maxEntries, FALLBACK_INVENTORY_ENTRY_LIMIT);
  const maxFiles = positiveLimit(options.maxFiles, INVENTORY_FILE_LIMIT);
  const maxPathBytes = positiveLimit(
    options.maxPathBytes,
    FALLBACK_INVENTORY_PATH_BYTE_LIMIT,
  );
  const openDirectory = options.openDirectory ?? fs.opendirSync;
  const absPathsByKey = changed.absPathsByKey;
  const packagePathSet = new Set<string>();
  const configPathSet = new Set<string>();
  const pendingDirectories = [path.resolve(args.repo)];
  let visitedEntries = 0;
  let retainedPathBytes = changed.retainedPathBytes;
  let partialReason: string | null = changed.partialReason;

  scan: while (pendingDirectories.length > 0) {
    if (shouldStop()) {
      partialReason = combinePartialReasons(
        partialReason,
        "TypeScript fallback inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
      );
      break;
    }
    const directoryPath = pendingDirectories.pop();
    if (!directoryPath) break;
    let directory: fs.Dir;
    try {
      directory = openDirectory(directoryPath);
    } catch {
      const relativeDirectory =
        normalizeRelPath(path.relative(args.repo, directoryPath)) || ".";
      partialReason ??=
        `TypeScript fallback inventory could not read directory ${relativeDirectory}; ` +
        "repository context is partial.";
      continue;
    }
    const openedDirectoryIsSafe = isSafeFallbackDirectory(
      args.repo,
      realRepoPath,
      directoryPath,
    );
    if (!openedDirectoryIsSafe) {
      const relativeDirectory =
        normalizeRelPath(path.relative(args.repo, directoryPath)) || ".";
      partialReason ??=
        `TypeScript fallback inventory could not validate opened directory ${relativeDirectory}; ` +
        "repository context is partial.";
      try {
        directory.closeSync();
      } catch {
        partialReason ??=
          `TypeScript fallback inventory could not close directory ${relativeDirectory}; ` +
          "repository context is partial.";
      }
      continue;
    }
    try {
      while (true) {
        if (shouldStop()) {
          partialReason = combinePartialReasons(
            partialReason,
            "TypeScript fallback inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
          );
          break scan;
        }
        const entry = directory.readSync();
        if (!entry) break;
        visitedEntries += 1;
        if (visitedEntries > maxEntries) {
          partialReason = combinePartialReasons(
            partialReason,
            `TypeScript fallback inventory scan reached the safety limit of ${maxEntries} filesystem entries; ` +
              "repository context is partial.",
          );
          break scan;
        }
        if (isIgnoredDirectory(entry.name)) continue;
        if (entry.isDirectory()) {
          const absPath = path.join(directoryPath, entry.name);
          const relPath = normalizeRelPath(path.relative(args.repo, absPath));
          const pathBytes = Buffer.byteLength(relPath, "utf8") + 1;
          if (retainedPathBytes + pathBytes > maxPathBytes) {
            partialReason = combinePartialReasons(
              partialReason,
              `TypeScript fallback inventory scan reached the retained-path byte safety limit of ${maxPathBytes}; ` +
                "repository context is partial.",
            );
            break scan;
          }
          retainedPathBytes += pathBytes;
          pendingDirectories.push(absPath);
          continue;
        }
        if (!entry.isFile()) continue;
        const extension = path.extname(entry.name).toLowerCase();
        const isMetadata = extension === ".json" || extension === ".jsonc";
        const isSource = isTypeScriptOrJavaScriptFileName(entry.name);
        if (!isMetadata && !isSource) continue;
        const absPath = path.join(directoryPath, entry.name);
        const relPath = normalizeRelPath(path.relative(args.repo, absPath));
        if (
          isSource &&
          absPathsByKey.has(canonicalPathKey(absPath))
        ) {
          continue;
        }
        const pathBytes = Buffer.byteLength(relPath, "utf8") + 1;
        if (retainedPathBytes + pathBytes > maxPathBytes) {
          partialReason = combinePartialReasons(
            partialReason,
            `TypeScript fallback inventory scan reached the retained-path byte safety limit of ${maxPathBytes}; ` +
              "repository context is partial.",
          );
          break scan;
        }
        retainedPathBytes += pathBytes;
        if (isMetadata) {
          const validation = validateRepoRelativePath(
            args.repo,
            realRepoPath,
            relPath,
            "TypeScript fallback metadata path",
            validationCache,
          );
          if (validation.status !== "safe") {
            partialReason ??= validationPartialReason(
              "TypeScript fallback metadata path",
              relPath,
              validation.status,
            );
            continue;
          }
          const resolvedPath = validation.absPath;
          if (configPathSet.size < maxFiles) {
            configPathSet.add(resolvedPath);
          } else {
            partialReason ??=
              `TypeScript fallback inventory scan reached the safety limit of ${maxFiles} config files; ` +
              "repository context is partial.";
          }
          if (entry.name === "package.json") {
            if (packagePathSet.size < maxFiles) {
              packagePathSet.add(resolvedPath);
            } else {
              partialReason ??=
                `TypeScript fallback inventory scan reached the safety limit of ${maxFiles} package files; ` +
                "repository context is partial.";
            }
          }
          continue;
        }
        const validation = validateRepoRelativePath(
          args.repo,
          realRepoPath,
          relPath,
          "TypeScript fallback source path",
          validationCache,
        );
        if (validation.status !== "safe") {
          partialReason ??= validationPartialReason(
            "TypeScript fallback source path",
            relPath,
            validation.status,
          );
          continue;
        }
        const absPathKey = canonicalPathKey(validation.absPath);
        if (absPathsByKey.has(absPathKey)) continue;
        if (absPathsByKey.size >= maxFiles) {
          partialReason = combinePartialReasons(
            partialReason,
            `TypeScript fallback inventory scan reached the safety limit of ${maxFiles} source files; ` +
              "repository context is partial.",
          );
          break scan;
        }
        absPathsByKey.set(absPathKey, validation.absPath);
      }
    } catch {
      const relativeDirectory =
        normalizeRelPath(path.relative(args.repo, directoryPath)) || ".";
      partialReason ??=
        `TypeScript fallback inventory could not finish reading directory ${relativeDirectory}; ` +
        "repository context is partial.";
    } finally {
      try {
        directory.closeSync();
      } catch {
        const relativeDirectory =
          normalizeRelPath(path.relative(args.repo, directoryPath)) || ".";
        partialReason ??=
          `TypeScript fallback inventory could not close directory ${relativeDirectory}; ` +
          "repository context is partial.";
      }
    }
  }

  return inventoryFromAbsPaths(
    [...absPathsByKey.values()].sort(),
    null,
    partialReason,
    [...packagePathSet],
    [...configPathSet],
    validationCache,
    false,
  );
}

function readBoundedManifestFile(
  manifestPath: string,
  maxBytes: number,
  shouldStop: () => boolean,
): ManifestReadResult {
  let descriptor: number | null = null;
  try {
    const noFollow =
      typeof fs.constants.O_NOFOLLOW === "number"
        ? fs.constants.O_NOFOLLOW
        : 0;
    descriptor = fs.openSync(
      manifestPath,
      fs.constants.O_RDONLY | noFollow,
    );
    const before = fs.fstatSync(descriptor);
    if (!before.isFile()) {
      throw new Error("manifest is not a regular file");
    }
    if (before.size > maxBytes) return { status: "byte-limit" };

    const chunks: Buffer[] = [];
    let totalBytes = 0;
    while (totalBytes <= maxBytes) {
      if (shouldStop()) return { status: "stopped" };
      const chunk = Buffer.allocUnsafe(
        Math.min(64 * 1024, maxBytes + 1 - totalBytes),
      );
      const bytesRead = fs.readSync(
        descriptor,
        chunk,
        0,
        chunk.length,
        null,
      );
      if (bytesRead === 0) break;
      chunks.push(chunk.subarray(0, bytesRead));
      totalBytes += bytesRead;
    }
    if (totalBytes > maxBytes) return { status: "byte-limit" };

    const after = fs.fstatSync(descriptor);
    if (
      before.dev !== after.dev ||
      before.ino !== after.ino ||
      before.size !== after.size ||
      before.mtimeMs !== after.mtimeMs ||
      before.ctimeMs !== after.ctimeMs
    ) {
      throw new Error("manifest changed while it was being read");
    }
    return {
      status: "ok",
      text: Buffer.concat(chunks, totalBytes).toString("utf8"),
    };
  } finally {
    if (descriptor !== null) fs.closeSync(descriptor);
  }
}

function manifestExceedsEntrySafetyLimit(
  manifestText: string,
  maxEntriesPerArray: number,
): boolean {
  const stack: Array<{
    kind: "array" | "object";
    entries: number;
    expectingArrayValue: boolean;
  }> = [];
  const maxStructuralSeparators = maxEntriesPerArray * 3 + 64;
  let structuralSeparators = 0;
  let inString = false;
  let escaped = false;

  const startArrayValue = (): boolean => {
    const parent = stack.at(-1);
    if (parent?.kind !== "array" || !parent.expectingArrayValue) {
      return false;
    }
    parent.entries += 1;
    parent.expectingArrayValue = false;
    return parent.entries > maxEntriesPerArray;
  };

  for (let index = 0; index < manifestText.length; index += 1) {
    const character = manifestText[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === '"') {
        inString = false;
      }
      continue;
    }
    if (character === '"') {
      if (startArrayValue()) return true;
      inString = true;
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
    if (character === ",") {
      structuralSeparators += 1;
      if (structuralSeparators > maxStructuralSeparators) return true;
      const parent = stack.at(-1);
      if (parent?.kind === "array") parent.expectingArrayValue = true;
      continue;
    }
    if (character === "[") {
      if (startArrayValue()) return true;
      stack.push({
        kind: "array",
        entries: 0,
        expectingArrayValue: true,
      });
      if (stack.length > 128) return true;
      continue;
    }
    if (character === "{") {
      if (startArrayValue()) return true;
      stack.push({
        kind: "object",
        entries: 0,
        expectingArrayValue: false,
      });
      if (stack.length > 128) return true;
      continue;
    }
    if (character === "]" || character === "}") {
      stack.pop();
      continue;
    }
    if (startArrayValue()) return true;
  }
  return false;
}

function validatedChangedAbsPaths(
  args: Args,
  realRepoPath: string,
  validationCache: RepoPathValidationCache,
  maxFiles: number,
  maxPathBytes: number,
  maxValidationEntries: number,
  shouldStop: () => boolean,
): ValidatedChangedPaths {
  const changedByKey = new Map<string, string>();
  let retainedPathBytes = 0;
  let partialReason: string | null = null;
  let fileLimitReported = false;
  let pathByteLimitReported = false;
  let validationIssueReported = false;
  let validationAttempts = 0;
  for (const changedFile of args.changed) {
    const relPath = normalizeRelPath(changedFile);
    const validationKey = canonicalPathKey(
      path.resolve(args.repo, relPath),
    );
    if (!isTypeScriptOrJavaScriptFileName(validationKey)) continue;
    if (changedByKey.has(validationKey)) continue;
    if (changedByKey.size >= maxFiles) {
      if (!fileLimitReported) {
        partialReason = combinePartialReasons(
          partialReason,
          `TypeScript changed path inventory reached the safety limit of ${maxFiles} source files; ` +
            "repository context is partial.",
        );
        fileLimitReported = true;
      }
      if (shouldStop()) {
        partialReason = combinePartialReasons(
          partialReason,
          "TypeScript changed path inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
        );
      }
      break;
    }
    if (validationAttempts > 0 && shouldStop()) {
      partialReason = combinePartialReasons(
        partialReason,
        "TypeScript changed path inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
      );
      break;
    }
    if (validationAttempts >= maxValidationEntries) {
      partialReason = combinePartialReasons(
        partialReason,
        `TypeScript changed path inventory reached the validation safety limit of ${maxValidationEntries} paths; ` +
          "repository context is partial.",
      );
      break;
    }
    if (
      changedByKey.size > 0 &&
      retainedPathBytes >= maxPathBytes
    ) {
      if (!pathByteLimitReported) {
        partialReason = combinePartialReasons(
          partialReason,
          `TypeScript changed path inventory reached the retained-path byte safety limit of ${maxPathBytes}; ` +
            "repository context is partial.",
        );
        pathByteLimitReported = true;
      }
      if (shouldStop()) {
        partialReason = combinePartialReasons(
          partialReason,
          "TypeScript changed path inventory scan stopped because the analysis time budget was exhausted; repository context is partial.",
        );
      }
      break;
    }
    const pathBytes = Buffer.byteLength(relPath, "utf8") + 1;
    if (
      changedByKey.size > 0 &&
      retainedPathBytes + pathBytes > maxPathBytes
    ) {
      if (!pathByteLimitReported) {
        partialReason = combinePartialReasons(
          partialReason,
          `TypeScript changed path inventory reached the retained-path byte safety limit of ${maxPathBytes}; ` +
            "repository context is partial.",
        );
        pathByteLimitReported = true;
      }
      continue;
    }
    const validationWasCached = validationCache.has(validationKey);
    validationAttempts += 1;
    const validation = validateRepoRelativePath(
      args.repo,
      realRepoPath,
      relPath,
      "TypeScript changed path",
      validationCache,
    );
    const absPath = validation.absPath;
    if (
      validation.status !== "safe" &&
      !validationIssueReported
    ) {
      partialReason = combinePartialReasons(
        partialReason,
        validationPartialReason(
          "TypeScript changed path",
          relPath,
          validation.status,
        ),
      );
      validationIssueReported = true;
    }
    if (validation.status === "unsafe") {
      if (!validationWasCached) validationCache.delete(validationKey);
      continue;
    }

    const pathKey = canonicalPathKey(absPath);
    if (changedByKey.has(pathKey)) continue;

    if (
      retainedPathBytes + pathBytes > maxPathBytes
    ) {
      if (!pathByteLimitReported) {
        partialReason = combinePartialReasons(
          partialReason,
          `TypeScript changed path inventory reached the retained-path byte safety limit of ${maxPathBytes}; ` +
            "repository context is partial.",
        );
        pathByteLimitReported = true;
      }
      // Preserve one changed root so callers can still report the original
      // changed-file failure when a configured byte limit is smaller than
      // a single safe repository-relative path.
      if (changedByKey.size > 0) {
        if (!validationWasCached) {
          validationCache.delete(validationKey);
        }
        continue;
      }
    }
    changedByKey.set(pathKey, absPath);
    retainedPathBytes += pathBytes;
  }
  return {
    absPathsByKey: changedByKey,
    retainedPathBytes,
    partialReason,
  };
}

function validateRepoRelativePath(
  repo: string,
  realRepoPath: string,
  relPath: string,
  label: string,
  validationCache: RepoPathValidationCache,
): RepoPathValidation {
  const absPath = path.resolve(repo, relPath);
  if (path.isAbsolute(relPath) || !isInsideRepo(repo, absPath)) {
    throw new Error(`${label} is outside the repository: ${relPath}`);
  }
  const cacheKey = canonicalPathKey(absPath);
  const cached = validationCache.get(cacheKey);
  if (cached) return cached;

  let pathStat: fs.Stats;
  try {
    pathStat = fs.lstatSync(absPath);
  } catch (error) {
    if (
      error instanceof Error &&
      "code" in error &&
      error.code === "ENOENT"
    ) {
      const missing: RepoPathValidation = {
        absPath,
        realPath: null,
        status: "missing",
      };
      validationCache.set(cacheKey, missing);
      return missing;
    }
    const unsafe: RepoPathValidation = {
      absPath,
      realPath: null,
      status: "unsafe",
    };
    validationCache.set(cacheKey, unsafe);
    return unsafe;
  }
  if (!pathStat.isFile() || pathStat.isSymbolicLink()) {
    const unsafe: RepoPathValidation = {
      absPath,
      realPath: null,
      status: "unsafe",
    };
    validationCache.set(cacheKey, unsafe);
    return unsafe;
  }
  let realPath: string;
  try {
    realPath = fs.realpathSync(absPath);
  } catch {
    const unsafe: RepoPathValidation = {
      absPath,
      realPath: null,
      status: "unsafe",
    };
    validationCache.set(cacheKey, unsafe);
    return unsafe;
  }
  if (!isInsideRepo(realRepoPath, realPath)) {
    const unsafe: RepoPathValidation = {
      absPath,
      realPath: null,
      status: "unsafe",
    };
    validationCache.set(cacheKey, unsafe);
    return unsafe;
  }
  const safe: RepoPathValidation = {
    absPath,
    realPath,
    status: "safe",
  };
  validationCache.set(cacheKey, safe);
  return safe;
}

function isSafeFallbackDirectory(
  repo: string,
  realRepoPath: string,
  directoryPath: string,
): boolean {
  const repoRoot = path.resolve(repo);
  const resolvedDirectory = path.resolve(directoryPath);
  if (!isSameOrInsideRepo(repoRoot, resolvedDirectory)) return false;

  let directoryStat: fs.Stats;
  try {
    directoryStat = fs.lstatSync(resolvedDirectory);
    if (directoryStat.isSymbolicLink()) {
      if (resolvedDirectory !== repoRoot) return false;
      directoryStat = fs.statSync(resolvedDirectory);
    }
  } catch {
    return false;
  }
  if (!directoryStat.isDirectory()) return false;

  let realDirectory: string;
  try {
    realDirectory = fs.realpathSync(resolvedDirectory);
  } catch {
    return false;
  }
  return isSameOrInsideRepo(realRepoPath, realDirectory);
}

function partialInventory(
  changedAbsPaths: Iterable<string>,
  reason: string,
  validationCache: RepoPathValidationCache,
): RepoFileInventory {
  return inventoryFromAbsPaths(
    [...changedAbsPaths].sort(),
    null,
    reason,
    [],
    [],
    validationCache,
    true,
  );
}

function positiveLimit(value: number | undefined, fallback: number): number {
  return value !== undefined && Number.isFinite(value) && value > 0
    ? Math.min(Math.floor(value), fallback)
    : fallback;
}

function inventoryFromAbsPaths(
  absPaths: string[],
  fingerprint: string | null,
  partialReason: string | null,
  packageJsonAbsPaths: string[] = [],
  configJsonAbsPaths: string[] = [],
  validationCache: RepoPathValidationCache = new Map(),
  manifestBounded = true,
): RepoFileInventory {
  const uniqueAbsPaths = [
    ...new Map(
      absPaths.map((fileName) => [canonicalPathKey(fileName), fileName]),
    ).values(),
  ].sort();
  const uniquePackageJsonAbsPaths = [
    ...new Map(
      packageJsonAbsPaths.map((fileName) => [
        canonicalPathKey(fileName),
        fileName,
      ]),
    ).values(),
  ].sort();
  const uniqueConfigJsonAbsPaths = [
    ...new Map(
      configJsonAbsPaths.map((fileName) => [
        canonicalPathKey(fileName),
        fileName,
      ]),
    ).values(),
  ].sort();
  const declarationAbsPaths = uniqueAbsPaths.filter(isDeclarationFileName);
  return {
    absPaths: uniqueAbsPaths,
    declarationAbsPaths,
    packageJsonAbsPaths: uniquePackageJsonAbsPaths,
    packagePathKeys: inventoryPathKeys(
      uniquePackageJsonAbsPaths,
      validationCache,
    ),
    configJsonAbsPaths: uniqueConfigJsonAbsPaths,
    configPathKeys: inventoryPathKeys(
      uniqueConfigJsonAbsPaths,
      validationCache,
    ),
    pathKeys: inventoryPathKeys(uniqueAbsPaths, validationCache),
    fingerprint,
    partial: partialReason !== null,
    partialReason,
    configurationPartial: false,
    manifestBounded,
  };
}

function inventoryPathKeys(
  absPaths: string[],
  validationCache: RepoPathValidationCache,
): Set<string> {
  const keys = new Set<string>();
  for (const fileName of absPaths) {
    const pathKey = canonicalPathKey(fileName);
    keys.add(pathKey);
    const cached = validationCache.get(pathKey);
    if (cached) {
      if (cached.status === "safe" && cached.realPath) {
        keys.add(canonicalPathKey(cached.realPath));
      }
      continue;
    }
    try {
      keys.add(canonicalPathKey(fs.realpathSync(fileName)));
    } catch {
      // Missing changed paths remain permission keys so the analyzer can
      // report them as failed instead of widening access to other sources.
    }
  }
  return keys;
}

function validationPartialReason(
  label: string,
  relPath: string,
  status: Exclude<RepoPathValidationStatus, "safe">,
): string {
  const detail = status === "missing" ? "was unavailable" : "could not be read safely";
  return `${label} ${relPath} ${detail}; repository context is partial.`;
}

function combinePartialReasons(
  first: string | null,
  second: string,
): string {
  return first ? `${first} ${second}` : second;
}
