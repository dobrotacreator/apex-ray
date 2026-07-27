export const IGNORED_DIRECTORY_NAMES = new Set([
  "node_modules",
  ".git",
  ".pnpm-store",
  ".worktrees",
  ".next",
  ".turbo",
  ".sim-data",
  "dist",
  "build",
  "out",
  "coverage",
  "sim-results",
]);
export const REFERENCE_LIMIT = 12;
export const REFERENCE_COLLECTION_LIMIT = REFERENCE_LIMIT * 2;
export const CLASS_HERITAGE_CONTRACT_DEPTH_LIMIT = 4;
export const CONTRACT_DEPENDENCY_DEPTH_LIMIT = 3;
export const ANALYZER_SOURCE_FILE_LIMIT = 512;
export const ANALYZER_SOURCE_BYTE_LIMIT = 8 * 1024 * 1024;
export const ANALYZER_METADATA_BYTE_LIMIT = 4 * 1024 * 1024;
export const ANALYZER_METADATA_FILE_LIMIT = 512;
export const ANALYZER_PACKAGE_INFO_LIMIT = 512;
export const REPO_INDEX_SEMANTIC_ENTRY_LIMIT = 50_000;
export const REPO_INDEX_CACHE_BYTE_LIMIT = 16 * 1024 * 1024;
export const FOCUSED_PROGRAM_CHANGED_FILE_THRESHOLD = 40;
export const FOCUSED_PROGRAM_DECLARATION_ROOT_LIMIT = 128;
export const REFLECTOR_METADATA_METHOD_NAMES = new Set(["get", "getAll", "getAllAndMerge", "getAllAndOverride"]);
export const REPO_INDEX_CACHE_VERSION = 22;
export const REPO_INDEX_CACHE_FILE = `typescript-repo-index-v${REPO_INDEX_CACHE_VERSION}.json`;
export const NAMESPACE_EXPORT_LOCAL_NAME = "*";
export const STAR_EXPORT_LOCAL_NAME = "**";
export const ARRAY_OBJECT_ENTRY_ID_PROPERTY_NAMES = [
  "key",
  "id",
  "name",
  "type",
  "eventType",
  "target",
  "path",
  "template",
  "code",
  "action",
  "permission",
];
export const IGNORED_CONTRACT_DEPENDENCY_NAMES = new Set([
  "Array",
  "Boolean",
  "Date",
  "Error",
  "Map",
  "Number",
  "Object",
  "Promise",
  "Record",
  "ReturnType",
  "Set",
  "String",
  "undefined",
  "z",
]);
