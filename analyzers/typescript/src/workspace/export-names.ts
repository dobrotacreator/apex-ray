import path from "node:path";

import { NAMESPACE_EXPORT_LOCAL_NAME, STAR_EXPORT_LOCAL_NAME } from "../constants.js";
import { isModuleSpecifierRelatedToPath } from "../module-resolution.js";
import type {
  CollectedSymbol,
  ExportedNamesForTarget,
  ExportIndexEntry,
  PackageInfo,
  RepoIndex,
} from "../types.js";
import { normalizeRelPath } from "../utils.js";

export function exportedNamesForTarget(repoIndex: RepoIndex, target: CollectedSymbol, targetPackage: PackageInfo): ExportedNamesForTarget {
  const targetPath = path.resolve(target.node.getSourceFile().fileName);
  const allNames = new Set<string>();
  const byFile = new Map<string, Set<string>>();
  const namespacesByFile = new Map<string, Map<string, Set<string>>>();
  const queue = [{ filePath: targetPath, exportName: target.analysis.name }];
  if (target.defaultExported) {
    queue.push({ filePath: targetPath, exportName: "default" });
  }

  const seen = new Set<string>();
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index];
    const key = `${current.filePath}:${current.exportName}`;
    if (seen.has(key)) continue;
    seen.add(key);
    allNames.add(current.exportName);
    addExportedNameForFile(byFile, current.filePath, current.exportName);

    for (const entry of repoIndex.files) {
      for (const exportEntry of entry.exports) {
        if (exportEntry.localName === STAR_EXPORT_LOCAL_NAME) {
          if (current.exportName === "default") continue;
          if (!isExportEntryRelatedToPath(exportEntry, entry.absPath, current.filePath, targetPackage)) continue;
          queue.push({ filePath: path.resolve(entry.absPath), exportName: current.exportName });
          continue;
        }
        if (exportEntry.localName === NAMESPACE_EXPORT_LOCAL_NAME) {
          if (!isExportEntryRelatedToPath(exportEntry, entry.absPath, current.filePath, targetPackage)) continue;
          addNamespaceExportedNameForFile(namespacesByFile, entry.absPath, exportEntry.exportedName, current.exportName);
          continue;
        }
        if (exportEntry.localName !== current.exportName) continue;
        if (!isExportEntryRelatedToPath(exportEntry, entry.absPath, current.filePath, targetPackage)) continue;
        queue.push({ filePath: path.resolve(entry.absPath), exportName: exportEntry.exportedName });
      }
    }
  }

  return { allNames, byFile, namespacesByFile };
}

function addExportedNameForFile(namesByFile: Map<string, Set<string>>, filePath: string, name: string): void {
  const key = normalizeRelPath(path.resolve(filePath));
  const names = namesByFile.get(key) ?? new Set<string>();
  names.add(name);
  namesByFile.set(key, names);
}

function addNamespaceExportedNameForFile(
  namespacesByFile: Map<string, Map<string, Set<string>>>,
  filePath: string,
  namespaceName: string,
  memberName: string,
): void {
  const key = normalizeRelPath(path.resolve(filePath));
  const namespaces = namespacesByFile.get(key) ?? new Map<string, Set<string>>();
  const memberNames = namespaces.get(namespaceName) ?? new Set<string>();
  memberNames.add(memberName);
  namespaces.set(namespaceName, memberNames);
  namespacesByFile.set(key, namespaces);
}

function isExportEntryRelatedToPath(
  exportEntry: ExportIndexEntry,
  exporterPath: string,
  sourcePath: string,
  targetPackage: PackageInfo,
): boolean {
  if (exportEntry.moduleSpecifier === null) {
    return path.resolve(exporterPath) === path.resolve(sourcePath);
  }
  return isModuleSpecifierRelatedToPath(exportEntry.moduleSpecifier, exporterPath, sourcePath, targetPackage);
}
