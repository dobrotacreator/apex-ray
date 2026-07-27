import path from "node:path";

import ts from "typescript";

import type { IndexCollectionControl } from "./collection.js";
import { collectDiInjectionIndex, collectDiProviderIndex } from "./di.js";
import { collectExportIndex, collectImportIndex } from "./import-export.js";
import {
  collectClassHeritageIndex,
  collectIdentifierIndex,
  collectReceiverIndex,
  collectTypeAliasIndex,
} from "./semantic-file.js";
import type { RepoFileIndexEntry } from "../types.js";
import {
  isDeclarationFileName,
  isTypeScriptOrJavaScriptFileName,
  normalizeRelPath,
  scriptKindForPath,
} from "../utils.js";

interface SourceFileIndexInput {
  repo: string;
  absPath: string;
  relPath: string;
  dev: number;
  ino: number;
  size: number;
  mtimeMs: number;
  ctimeMs: number;
  text: string;
}

export function isAnalyzableSourceFile(filePath: string): boolean {
  const normalized = normalizeRelPath(filePath);
  return isTypeScriptOrJavaScriptFileName(normalized) && !isDeclarationFileName(normalized);
}

export function indexSourceFile(
  input: SourceFileIndexInput,
  control?: IndexCollectionControl,
): RepoFileIndexEntry {
  const source = ts.createSourceFile(input.absPath, input.text, ts.ScriptTarget.ES2022, true, scriptKindForPath(input.absPath));
  return {
    absPath: path.resolve(input.absPath),
    relPath: input.relPath,
    relLower: input.relPath.toLowerCase(),
    dev: input.dev,
    ino: input.ino,
    size: input.size,
    mtimeMs: input.mtimeMs,
    ctimeMs: input.ctimeMs,
    imports: collectImportIndex(input.repo, source, control),
    exports: collectExportIndex(input.repo, source, control),
    identifiers: collectIdentifierIndex(input.repo, source, control),
    receivers: collectReceiverIndex(input.repo, source, control),
    typeAliases: collectTypeAliasIndex(source, control),
    classHeritages: collectClassHeritageIndex(source, control),
    diProviders: collectDiProviderIndex(input.repo, source, control),
    diInjections: collectDiInjectionIndex(input.repo, source, control),
  };
}
