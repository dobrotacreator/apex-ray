import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import type { Args } from "../types.js";
import { isDeclarationFileName, isInsideRepo, isRecord, normalizeRelPath, walk } from "../utils.js";

export interface RepoFileInventory {
  absPaths: string[];
  declarationAbsPaths: string[];
  pathKeys: Set<string>;
  fingerprint: string | null;
}

export function loadRepoFileInventory(args: Args): RepoFileInventory {
  if (!args.fileManifestPath) {
    const absPaths: string[] = [];
    walk(args.repo, (absPath) => absPaths.push(absPath));
    return inventoryFromAbsPaths(absPaths, null);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(args.fileManifestPath, "utf8"));
  } catch (error) {
    throw new Error(
      `Could not read TypeScript file manifest ${args.fileManifestPath}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
  if (
    !isRecord(parsed) ||
    parsed.version !== 1 ||
    !Array.isArray(parsed.files) ||
    !parsed.files.every((value) => typeof value === "string")
  ) {
    throw new Error(`Invalid TypeScript file manifest: ${args.fileManifestPath}`);
  }

  const relPaths = [...new Set(parsed.files.map((value) => normalizeRelPath(value)))].sort();
  const realRepoPath = fs.realpathSync(args.repo);
  const absPaths = relPaths.map((relPath) => {
    const absPath = path.resolve(args.repo, relPath);
    if (path.isAbsolute(relPath) || !isInsideRepo(args.repo, absPath)) {
      throw new Error(`TypeScript file manifest path is outside the repository: ${relPath}`);
    }
    if (fs.existsSync(absPath)) {
      const realPath = fs.realpathSync(absPath);
      if (!isInsideRepo(realRepoPath, realPath)) {
        throw new Error(`TypeScript file manifest path is outside the repository: ${relPath}`);
      }
    }
    return absPath;
  });
  const fingerprint = crypto.createHash("sha256").update(relPaths.join("\0")).digest("hex");
  return inventoryFromAbsPaths(absPaths, fingerprint);
}

function inventoryFromAbsPaths(absPaths: string[], fingerprint: string | null): RepoFileInventory {
  const declarationAbsPaths = absPaths.filter(isDeclarationFileName);
  return {
    absPaths,
    declarationAbsPaths,
    pathKeys: new Set(
      absPaths.map((fileName) => normalizeRelPath(path.resolve(fileName))),
    ),
    fingerprint,
  };
}
