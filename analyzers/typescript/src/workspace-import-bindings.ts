import { isModuleSpecifierRelatedToPath, moduleSpecifierCandidatePaths } from "./module-resolution.js";
import type {
  CollectedSymbol,
  ExportedNamesForTarget,
  IdentifierIndexEntry,
  ImportedBindingsForTarget,
  PackageInfo,
  Reference,
  RepoFileIndexEntry,
} from "./types.js";

export function emptyImportedBindings(): ImportedBindingsForTarget {
  return {
    localNames: new Map(),
    namespaceLocalNames: new Map(),
    namespaceExportNames: new Map(),
  };
}

export function importedBindingsForTarget(
  entry: RepoFileIndexEntry,
  repo: string,
  target: CollectedSymbol,
  targetPackage: PackageInfo,
  exportedNames: ExportedNamesForTarget,
): ImportedBindingsForTarget {
  const bindings = emptyImportedBindings();
  for (const importEntry of entry.imports) {
    const importExportNames = exportedNamesForImport(importEntry.moduleSpecifier, entry.absPath, repo, targetPackage, exportedNames);
    const importNamespaceExportNames = exportedNamespaceNamesForImport(
      importEntry.moduleSpecifier,
      entry.absPath,
      repo,
      targetPackage,
      exportedNames,
    );
    if (
      isModuleSpecifierRelatedToPath(importEntry.moduleSpecifier, entry.absPath, target.node.getSourceFile().fileName, targetPackage)
    ) {
      for (const name of exportedNames.allNames) {
        importExportNames.add(name);
      }
    }
    if (importExportNames.size === 0 && importNamespaceExportNames.size === 0) {
      continue;
    }

    if (importEntry.defaultImport && importExportNames.has("default")) {
      bindings.localNames.set(importEntry.defaultImport.localName, importEntry.defaultImport.reference);
    }

    if (importEntry.namespaceImport) {
      bindings.namespaceLocalNames.set(importEntry.namespaceImport.localName, importEntry.namespaceImport.reference);
      bindings.namespaceExportNames.set(importEntry.namespaceImport.localName, importExportNames);
      for (const [namespaceName, memberNames] of importNamespaceExportNames.entries()) {
        const localNamespaceName = `${importEntry.namespaceImport.localName}.${namespaceName}`;
        bindings.namespaceLocalNames.set(localNamespaceName, importEntry.namespaceImport.reference);
        bindings.namespaceExportNames.set(localNamespaceName, memberNames);
      }
    }

    for (const namedImport of importEntry.namedImports) {
      const namespaceMemberNames = importNamespaceExportNames.get(namedImport.importedName);
      if (namespaceMemberNames) {
        bindings.namespaceLocalNames.set(namedImport.localName, namedImport.reference);
        bindings.namespaceExportNames.set(namedImport.localName, namespaceMemberNames);
        continue;
      }
      if (importExportNames.has(namedImport.importedName)) {
        bindings.localNames.set(namedImport.localName, namedImport.reference);
      }
    }
  }
  return bindings;
}

function exportedNamesForImport(
  specifier: string,
  importerPath: string,
  repo: string,
  targetPackage: PackageInfo,
  exportedNames: ExportedNamesForTarget,
): Set<string> {
  const names = new Set<string>();
  for (const candidate of moduleSpecifierCandidatePaths(specifier, importerPath, repo, targetPackage)) {
    const candidateNames = exportedNames.byFile.get(candidate);
    if (!candidateNames) continue;
    for (const name of candidateNames) {
      names.add(name);
    }
  }
  return names;
}

function exportedNamespaceNamesForImport(
  specifier: string,
  importerPath: string,
  repo: string,
  targetPackage: PackageInfo,
  exportedNames: ExportedNamesForTarget,
): Map<string, Set<string>> {
  const namespaces = new Map<string, Set<string>>();
  for (const candidate of moduleSpecifierCandidatePaths(specifier, importerPath, repo, targetPackage)) {
    const candidateNamespaces = exportedNames.namespacesByFile.get(candidate);
    if (!candidateNamespaces) continue;
    for (const [namespaceName, memberNames] of candidateNamespaces.entries()) {
      const names = namespaces.get(namespaceName) ?? new Set<string>();
      for (const memberName of memberNames) {
        names.add(memberName);
      }
      namespaces.set(namespaceName, names);
    }
  }
  return namespaces;
}

export function isIdentifierMatchedByImportedBindings(
  identifier: IdentifierIndexEntry,
  bindings: ImportedBindingsForTarget,
): boolean {
  if (bindings.localNames.has(identifier.name)) return true;
  if (identifier.namespaceQualifier === null || !bindings.namespaceLocalNames.has(identifier.namespaceQualifier)) {
    return false;
  }
  return bindings.namespaceExportNames.get(identifier.namespaceQualifier)?.has(identifier.name) ?? false;
}
