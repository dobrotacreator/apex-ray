import ts from "typescript";

import type { StableFileIdentity } from "./utils.js";

export type SymbolKind =
  | "function"
  | "class"
  | "method"
  | "interface"
  | "type"
  | "variable"
  | "enum"
  | "enum-member"
  | "unknown";
export type ReferenceKind = "call" | "callee" | "contract" | "import" | "metadata" | "read" | "write" | "type" | "unknown";
export type SourceFilePredicate = (source: ts.SourceFile) => boolean;

export interface AnalyzerSymbol {
  name: string;
  kind: SymbolKind;
  startLine: number;
  endLine: number;
  exported: boolean;
  signature: string;
  references: Reference[];
  callees: Reference[];
  contracts: Reference[];
  metadata: Reference[];
}

export interface Reference {
  file: string;
  line: number;
  endLine?: number;
  text: string;
  kind: ReferenceKind;
}

export interface IndexedReference {
  file: string;
  line: number;
  endLine?: number;
  kind: ReferenceKind;
}

export interface MetadataKeyIdentity {
  symbol: ts.Symbol | null;
  text: string | null;
}

export interface CollectedSymbol {
  analysis: AnalyzerSymbol;
  node: ts.Node;
  tsSymbol: ts.Symbol | null;
  defaultExported: boolean;
  exportContainer: ExportContainer | null;
  containerNode?: ts.Node;
}

export interface ExportContainer {
  name: string;
  defaultExported: boolean;
}

export interface FileAnalysis {
  path: string;
  tsconfigPath: string | null;
  symbols: AnalyzerSymbol[];
  imports: string[];
  exports: string[];
  relatedTests: string[];
  changedSymbols: AnalyzerSymbol[];
}

export interface AnalyzerResult {
  language: "typescript";
  projectRoot: string;
  tsconfigPath: string | null;
  files: FileAnalysis[];
  warnings: string[];
  indexCache: RepoIndexCacheStats | null;
  partial: boolean;
  failedFiles: string[];
  shardFailures: AnalyzerShardFailure[];
}

export interface AnalyzerShardFailure {
  index: number;
  total: number;
  files: string[];
  reason: string;
  status: "failed" | "timeout" | "skipped";
}

export interface Args {
  repo: string;
  changed: string[];
  changedRanges: Map<string, Array<[number, number]>>;
  deletedLines: Map<string, DeletedLine[]>;
  indexCacheEnabled: boolean;
  indexCacheDir: string | null;
  refreshIndexCache: boolean;
  largeChangeSetSize: number | null;
  analysisTimeBudgetMs: number | null;
  fileManifestPath?: string | null;
}

export interface DeletedLine {
  line: number;
  text: string;
}

export interface ProgramContext {
  program: ts.Program;
  checker: ts.TypeChecker;
  tsconfigPath: string | null;
}

export interface PackageInfo {
  root: string;
  name: string;
  exports: unknown;
  main: string | null;
  module: string | null;
  tsconfig: string | null;
  types: string | null;
  typings: string | null;
}

export interface RepoIndex {
  files: RepoFileIndexEntry[];
  packageByFile: Map<string, PackageInfo | null>;
  fileIdentities?: Map<string, StableFileIdentity>;
  cacheStats: RepoIndexCacheStats | null;
  partial?: boolean;
}

export interface RepoFileIndexEntry {
  absPath: string;
  relPath: string;
  relLower: string;
  dev: number;
  ino: number;
  size: number;
  mtimeMs: number;
  ctimeMs: number;
  imports: ImportIndexEntry[];
  exports: ExportIndexEntry[];
  identifiers: IdentifierIndexEntry[];
  receivers: ReceiverIndexEntry[];
  typeAliases: TypeAliasIndexEntry[];
  classHeritages: ClassHeritageIndexEntry[];
  diProviders: DiProviderIndexEntry[];
  diInjections: DiInjectionIndexEntry[];
}

export interface ImportIndexEntry {
  moduleSpecifier: string;
  defaultImport: DefaultImportIndexEntry | null;
  namespaceImport: NamespaceImportIndexEntry | null;
  namedImports: NamedImportIndexEntry[];
}

export interface DefaultImportIndexEntry {
  localName: string;
  reference: Reference;
}

export interface NamespaceImportIndexEntry {
  localName: string;
  reference: Reference;
}

export interface NamedImportIndexEntry {
  importedName: string;
  localName: string;
  reference: Reference;
}

export interface ExportIndexEntry {
  moduleSpecifier: string | null;
  localName: string;
  exportedName: string;
  reference: Reference;
}

export interface CommonJsExportEntry {
  localName: string;
  exportedName: string;
  defaultExported: boolean;
  referenceNode: ts.Node;
}

export interface IdentifierIndexEntry {
  name: string;
  namespaceQualifier: string | null;
  reference: IndexedReference;
}

export interface ReceiverIndexEntry {
  receiverName: string;
  typeName: string | null;
  startLine: number;
  endLine: number;
  reference: Reference;
}

export interface TypeAliasIndexEntry {
  name: string;
  targetName: string;
}

export interface ClassHeritageIndexEntry {
  className: string;
  baseNames: string[];
}

export interface DiProviderIndexEntry {
  tokenName: string;
  implementationName: string;
  reference: Reference;
  sourceArrayName?: string;
}

export interface DiInjectionIndexEntry {
  tokenName: string;
  reference: Reference;
}

export interface ImportedBindingsForTarget {
  localNames: Map<string, Reference>;
  namespaceLocalNames: Map<string, Reference>;
  namespaceExportNames: Map<string, Set<string>>;
}

export interface ExportedNamesForTarget {
  allNames: Set<string>;
  byFile: Map<string, Set<string>>;
  namespacesByFile: Map<string, Map<string, Set<string>>>;
}

export interface TsConfigPathAliases {
  basePath: string;
  mappings: TsConfigPathMapping[];
}

export interface TsConfigPathMapping {
  pattern: string;
  targets: string[];
}

export interface VitestTestConfig {
  root: string;
  include: RegExp[];
  exclude: RegExp[];
}

export interface RelatedTestCandidate {
  relPath: string;
  priority: number;
}

export interface ExportedSymbolInfo {
  named: Set<string>;
  defaultNames: Set<string>;
}

export interface RepoIndexCacheStats {
  path: string;
  files: number;
  hits: number;
  misses: number;
  written: boolean;
}

export interface RepoIndexCacheFile {
  version: number;
  inventoryFingerprint: string | null;
  files: RepoIndexCacheFileEntry[];
}

export interface RepoIndexCacheWriteResult {
  written: boolean;
  error: string | null;
}

export interface RepoIndexCacheFileEntry {
  relPath: string;
  dev: number;
  ino: number;
  size: number;
  mtimeMs: number;
  ctimeMs: number;
  imports: ImportIndexEntry[];
  exports: ExportIndexEntry[];
  identifiers: IdentifierIndexEntry[];
  receivers: ReceiverIndexEntry[];
  typeAliases: TypeAliasIndexEntry[];
  classHeritages: ClassHeritageIndexEntry[];
  diProviders: DiProviderIndexEntry[];
  diInjections: DiInjectionIndexEntry[];
}
