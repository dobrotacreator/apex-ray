import path from "node:path";

import ts from "typescript";

import { collectDiInjectionIndex, collectDiProviderIndex } from "./di-index.js";
import { collectExportIndex, collectImportIndex } from "./import-export-index.js";
import {
  collectClassHeritageIndex,
  collectIdentifierIndex,
  collectReceiverIndex,
  collectTypeAliasIndex,
} from "./semantic-file-index.js";
import type { RepoFileIndexEntry } from "../types.js";
import { normalizeRelPath, scriptKindForPath } from "../utils.js";

interface SourceFileIndexInput {
  repo: string;
  absPath: string;
  relPath: string;
  size: number;
  mtimeMs: number;
  text: string;
}

export function isAnalyzableSourceFile(filePath: string): boolean {
  const normalized = normalizeRelPath(filePath);
  return /\.(ts|tsx|js|jsx)$/.test(normalized) && !/\.d\.ts$/.test(normalized);
}

export function indexSourceFile(input: SourceFileIndexInput): RepoFileIndexEntry {
  const source = ts.createSourceFile(input.absPath, input.text, ts.ScriptTarget.ES2022, true, scriptKindForPath(input.absPath));
  return {
    absPath: path.resolve(input.absPath),
    relPath: input.relPath,
    relLower: input.relPath.toLowerCase(),
    size: input.size,
    mtimeMs: input.mtimeMs,
    imports: collectImportIndex(input.repo, source),
    exports: collectExportIndex(input.repo, source),
    identifiers: collectIdentifierIndex(input.repo, source),
    receivers: collectReceiverIndex(input.repo, source),
    typeAliases: collectTypeAliasIndex(source),
    classHeritages: collectClassHeritageIndex(source),
    diProviders: collectDiProviderIndex(input.repo, source),
    diInjections: collectDiInjectionIndex(input.repo, source),
  };
}
