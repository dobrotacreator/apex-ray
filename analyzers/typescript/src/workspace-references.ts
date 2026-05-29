import path from "node:path";

import ts from "typescript";

import { expressionNameText, identifierFromExpression, propertyAssignmentNamed } from "./ast-utils.js";
import { NAMESPACE_EXPORT_LOCAL_NAME, STAR_EXPORT_LOCAL_NAME } from "./constants.js";
import {
  findIndexedPackageForFile,
  isModuleSpecifierRelatedToPath,
  moduleSpecifierCandidatePaths,
} from "./module-resolution.js";
import { addReference } from "./reference-merge.js";
import type {
  CollectedSymbol,
  ExportedNamesForTarget,
  ExportIndexEntry,
  IdentifierIndexEntry,
  ImportedBindingsForTarget,
  PackageInfo,
  ReceiverIndexEntry,
  Reference,
  RepoFileIndexEntry,
  RepoIndex,
} from "./types.js";
import { normalizeRelPath } from "./utils.js";

export function collectWorkspaceImportReferences(repo: string, repoIndex: RepoIndex, target: CollectedSymbol, limit: number): Reference[] {
  if (!target.analysis.exported) return [];

  const targetSource = target.node.getSourceFile();
  const targetFile = path.resolve(targetSource.fileName);
  const targetPackage = findIndexedPackageForFile(repo, repoIndex, targetFile);
  if (!targetPackage) return [];
  const exportedNames = exportedNamesForTarget(repoIndex, target, targetPackage);

  const refs: Reference[] = [];
  const seen = new Set<string>();
  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    if (entry.absPath === targetFile) continue;

    const importedBindings = importedBindingsForTarget(entry, repo, target, targetPackage, exportedNames);
    if (importedBindings.localNames.size === 0 && importedBindings.namespaceLocalNames.size === 0) continue;

    for (const importReference of [
      ...importedBindings.localNames.values(),
      ...importedBindings.namespaceLocalNames.values(),
    ]) {
      addReference(refs, seen, importReference, limit);
    }

    for (const identifier of entry.identifiers) {
      if (refs.length >= limit) break;
      if (isIdentifierMatchedByImportedBindings(identifier, importedBindings)) {
        addReference(refs, seen, identifier.reference, limit);
      }
    }
  }

  return refs;
}

export function collectWorkspaceMemberReferences(repo: string, repoIndex: RepoIndex, target: CollectedSymbol, limit: number): Reference[] {
  if (!target.exportContainer) return [];

  const targetSource = target.node.getSourceFile();
  const targetFile = path.resolve(targetSource.fileName);
  const targetPackage = findIndexedPackageForFile(repo, repoIndex, targetFile);
  if (!targetPackage) return [];

  const containerTarget: CollectedSymbol = {
    ...target,
    analysis: {
      ...target.analysis,
      name: target.exportContainer.name,
      exported: true,
    },
    defaultExported: target.exportContainer.defaultExported,
    exportContainer: null,
  };
  const exportedNames = exportedNamesForTarget(repoIndex, containerTarget, targetPackage);
  const refs: Reference[] = [];
  const seen = new Set<string>();

  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    if (entry.absPath === targetFile) continue;

    const importedBindings =
      targetPackage && exportedNames
        ? importedBindingsForTarget(entry, repo, containerTarget, targetPackage, exportedNames)
        : emptyImportedBindings();
    if (importedBindings.localNames.size === 0 && importedBindings.namespaceLocalNames.size === 0) continue;

    for (const identifier of entry.identifiers) {
      if (refs.length >= limit) break;
      if (isMemberReferenceForTarget(identifier, target.analysis.name, entry, importedBindings)) {
        addReference(refs, seen, identifier.reference, limit);
      }
    }
  }

  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    if (entry.absPath === targetFile) continue;

    const importedBindings =
      targetPackage && exportedNames
        ? importedBindingsForTarget(entry, repo, containerTarget, targetPackage, exportedNames)
        : emptyImportedBindings();
    if (importedBindings.localNames.size === 0 && importedBindings.namespaceLocalNames.size === 0) continue;

    for (const identifier of entry.identifiers) {
      if (refs.length >= limit) break;
      if (isIdentifierMatchedByImportedBindings(identifier, importedBindings)) {
        addReference(refs, seen, identifier.reference, limit);
      }
    }

    for (const importReference of [
      ...importedBindings.localNames.values(),
      ...importedBindings.namespaceLocalNames.values(),
    ]) {
      addReference(refs, seen, importReference, limit);
    }
  }

  return refs;
}

export function filterInvalidWorkspaceMemberReferences(
  repo: string,
  repoIndex: RepoIndex,
  target: CollectedSymbol,
  references: Reference[],
): Reference[] {
  if (!target.exportContainer && target.analysis.kind !== "method") return references;
  const targetSource = target.node.getSourceFile();
  const targetFile = path.resolve(targetSource.fileName);
  const targetPackage = findIndexedPackageForFile(repo, repoIndex, targetFile);
  const containerTarget = containerTargetForMemberTarget(target);
  const exportedNames = targetPackage ? exportedNamesForTarget(repoIndex, containerTarget, targetPackage) : null;
  const validReceiverTypeNames = memberReceiverTypeNames(repoIndex, target);

  return references.filter((reference) => {
    if (!["call", "read", "write"].includes(reference.kind)) return true;
    const entry = repoIndex.files.find((candidate) => candidate.relPath === reference.file);
    if (!entry) return true;
    const importedBindings =
      targetPackage && exportedNames
        ? importedBindingsForTarget(entry, repo, containerTarget, targetPackage, exportedNames)
        : emptyImportedBindings();
    const indexedIdentifiers = entry.identifiers.filter(
      (identifier) =>
        identifier.name === target.analysis.name &&
        identifier.namespaceQualifier !== null &&
        identifier.reference.line === reference.line &&
        identifier.reference.text === reference.text,
    );
    const identifiers =
      indexedIdentifiers.length > 0
        ? indexedIdentifiers
        : inferredMemberIdentifiers(reference, target.analysis.name);
    if (identifiers.length === 0) return true;
    return identifiers.some((identifier) =>
      memberIdentifierHasValidReceiver(identifier, entry, importedBindings, validReceiverTypeNames, target.analysis.name),
    );
  });
}

function emptyImportedBindings(): ImportedBindingsForTarget {
  return {
    localNames: new Map(),
    namespaceLocalNames: new Map(),
    namespaceExportNames: new Map(),
  };
}

function inferredMemberIdentifiers(reference: Reference, memberName: string): IdentifierIndexEntry[] {
  const pattern = new RegExp(`\\b((?:this|[A-Za-z_$][\\w$]*)(?:\\.[A-Za-z_$][\\w$]*)*)\\.${escapeRegExp(memberName)}\\b`, "g");
  const identifiers: IdentifierIndexEntry[] = [];
  for (const match of reference.text.matchAll(pattern)) {
    identifiers.push({
      name: memberName,
      namespaceQualifier: match[1],
      reference,
    });
  }
  return identifiers;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function memberIdentifierHasValidReceiver(
  identifier: IdentifierIndexEntry,
  entry: RepoFileIndexEntry,
  bindings: ImportedBindingsForTarget,
  validReceiverTypeNames: Set<string>,
  memberName: string,
): boolean {
  if (!identifier.namespaceQualifier) return true;
  if (qualifierMatchesImportedContainer(identifier.namespaceQualifier, bindings, memberName)) return true;

  const receiver = receiverForReference(entry, identifier);
  if (!receiver) return false;
  if (!receiver.typeName) return false;

  const typeNames = new Set([receiver.typeName, ...expandTypeAlias(receiver.typeName, entry, bindings)]);
  for (const typeName of typeNames) {
    if (typeNameMatchesImportedContainer(typeName, bindings) || typeNameSetHas(validReceiverTypeNames, typeName)) {
      return true;
    }
  }
  return false;
}

function memberReceiverTypeNames(repoIndex: RepoIndex, target: CollectedSymbol): Set<string> {
  const names = new Set<string>();
  if (target.exportContainer) names.add(target.exportContainer.name);
  const parent = target.node.parent;
  if (parent && ts.isClassDeclaration(parent)) {
    if (parent.name) names.add(parent.name.text);
    for (const clause of parent.heritageClauses ?? []) {
      for (const heritageType of clause.types) {
        const name = expressionNameText(heritageType.expression);
        if (name) names.add(name);
      }
    }
  }
  expandDerivedTypeNames(repoIndex, names);
  return names;
}

function expandDerivedTypeNames(repoIndex: RepoIndex, names: Set<string>): void {
  let changed = true;
  while (changed) {
    changed = false;
    for (const entry of repoIndex.files) {
      for (const heritage of entry.classHeritages) {
        if (typeNameSetHas(names, heritage.className)) continue;
        if (!heritage.baseNames.some((baseName) => typeNameSetHas(names, baseName))) continue;
        names.add(heritage.className);
        changed = true;
      }
    }
  }
}

function typeNameSetHas(typeNames: Set<string>, candidate: string): boolean {
  if (typeNames.has(candidate)) return true;
  const simple = simpleTypeName(candidate);
  return simple !== candidate && typeNames.has(simple);
}

function simpleTypeName(typeName: string): string {
  const dotIndex = typeName.lastIndexOf(".");
  return dotIndex === -1 ? typeName : typeName.slice(dotIndex + 1);
}

export function collectWorkspaceDiReferences(repo: string, repoIndex: RepoIndex, target: CollectedSymbol, limit: number): Reference[] {
  const diTarget = diTargetForSymbol(target);
  if (!diTarget.analysis.exported) return [];

  const targetSource = diTarget.node.getSourceFile();
  const targetFile = path.resolve(targetSource.fileName);
  const targetPackage = findIndexedPackageForFile(repo, repoIndex, targetFile);
  if (!targetPackage) return [];

  const exportedNames = exportedNamesForTarget(repoIndex, diTarget, targetPackage);
  const refs: Reference[] = [];
  const seen = new Set<string>();
  const tokenNames = new Set<string>();
  const providerArrayNames = new Set<string>();

  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    if (entry.absPath === targetFile) continue;

    const importedBindings = importedBindingsForTarget(entry, repo, diTarget, targetPackage, exportedNames);
    if (importedBindings.localNames.size === 0 && importedBindings.namespaceLocalNames.size === 0) continue;

    for (const provider of entry.diProviders) {
      if (refs.length >= limit) break;
      if (!importedBindings.localNames.has(provider.implementationName)) continue;
      tokenNames.add(provider.tokenName);
      if (provider.sourceArrayName) {
        providerArrayNames.add(provider.sourceArrayName);
      }
      addReference(refs, seen, provider.reference, limit);
    }
  }

  if (providerArrayNames.size > 0) {
    for (const entry of repoIndex.files) {
      if (refs.length >= limit) break;
      for (const provider of entry.diProviders) {
        if (refs.length >= limit) break;
        if (!providerArrayNames.has(provider.implementationName)) continue;
        addReference(refs, seen, provider.reference, limit);
      }
    }
  }

  if (tokenNames.size === 0) return refs;

  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    for (const injection of entry.diInjections) {
      if (!tokenNames.has(injection.tokenName)) continue;
      addReference(refs, seen, injection.reference, limit);
    }
  }

  return refs;
}

export function collectProviderTokenInjectionReferences(
  repo: string,
  repoIndex: RepoIndex,
  target: CollectedSymbol,
  limit: number,
): Reference[] {
  const providerObject = diProviderObjectForNode(target.node);
  const provideProperty = providerObject ? propertyAssignmentNamed(providerObject, "provide") : null;
  const token = provideProperty ? identifierFromExpression(provideProperty.initializer) : null;
  if (!token) return [];

  const refs: Reference[] = [];
  const seen = new Set<string>();
  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    for (const injection of entry.diInjections) {
      if (refs.length >= limit) break;
      if (injection.tokenName !== token.text) continue;
      addReference(refs, seen, injection.reference, limit);
    }
  }
  return refs;
}

function diProviderObjectForNode(node: ts.Node): ts.ObjectLiteralExpression | null {
  let current: ts.Node | undefined = node;
  while (current) {
    if (ts.isObjectLiteralExpression(current) && propertyAssignmentNamed(current, "provide")) {
      return current;
    }
    if (ts.isSourceFile(current)) return null;
    current = current.parent;
  }
  return null;
}

function diTargetForSymbol(target: CollectedSymbol): CollectedSymbol {
  return containerTargetForMemberTarget(target);
}

function containerTargetForMemberTarget(target: CollectedSymbol): CollectedSymbol {
  if (!target.exportContainer) return target;
  return {
    ...target,
    analysis: {
      ...target.analysis,
      name: target.exportContainer.name,
      exported: true,
    },
    defaultExported: target.exportContainer.defaultExported,
    exportContainer: null,
  };
}

function isMemberReferenceForTarget(
  identifier: IdentifierIndexEntry,
  memberName: string,
  entry: RepoFileIndexEntry,
  bindings: ImportedBindingsForTarget,
): boolean {
  if (identifier.name !== memberName || identifier.namespaceQualifier === null || identifier.reference.kind === "import") {
    return false;
  }
  if (qualifierMatchesImportedContainer(identifier.namespaceQualifier, bindings, memberName)) {
    return true;
  }
  const typeNames = typedReceiverTypesForReference(entry, bindings, identifier);
  for (const typeName of typeNames) {
    if (typeNameMatchesImportedContainer(typeName, bindings)) return true;
  }
  return false;
}

function typedReceiverTypesForReference(
  entry: RepoFileIndexEntry,
  bindings: ImportedBindingsForTarget,
  identifier: IdentifierIndexEntry,
): Set<string> {
  const typeNames = new Set<string>();
  const receiver = receiverForReference(entry, identifier);
  if (!receiver?.typeName) return typeNames;
  typeNames.add(receiver.typeName);
  for (const expanded of expandTypeAlias(receiver.typeName, entry, bindings)) {
    typeNames.add(expanded);
  }
  return typeNames;
}

function receiverForReference(entry: RepoFileIndexEntry, identifier: IdentifierIndexEntry): ReceiverIndexEntry | null {
  const position = identifier.reference.line;
  return (
    entry.receivers
      .filter(
        (receiver) =>
          receiver.receiverName === identifier.namespaceQualifier &&
          receiver.startLine <= position &&
          position <= receiver.endLine,
      )
      .sort((left, right) => right.startLine - left.startLine || left.endLine - right.endLine)[0] ?? null
  );
}

function typeNameMatchesImportedContainer(typeName: string, bindings: ImportedBindingsForTarget): boolean {
  if (bindings.localNames.has(typeName)) return true;
  return qualifiedNameMatchesImportedContainer(typeName, bindings);
}

function expandTypeAlias(typeName: string, entry: RepoFileIndexEntry, bindings: ImportedBindingsForTarget): Set<string> {
  const resolved = new Set<string>();
  const queue = [typeName];
  const seen = new Set<string>();
  while (queue.length > 0) {
    const current = queue.shift()!;
    if (seen.has(current)) continue;
    seen.add(current);
    const alias = entry.typeAliases.find((candidate) => candidate.name === current);
    if (!alias) continue;
    resolved.add(alias.targetName);
    if (!typeNameMatchesImportedContainer(alias.targetName, bindings)) {
      queue.push(alias.targetName);
    }
  }
  return resolved;
}

function qualifierMatchesImportedContainer(
  qualifier: string,
  bindings: ImportedBindingsForTarget,
  memberName: string,
): boolean {
  if (bindings.localNames.has(qualifier)) return true;
  if (bindings.namespaceExportNames.get(qualifier)?.has(memberName)) return true;
  return qualifiedNameMatchesImportedContainer(qualifier, bindings);
}

function qualifiedNameMatchesImportedContainer(value: string, bindings: ImportedBindingsForTarget): boolean {
  const dotIndex = value.lastIndexOf(".");
  if (dotIndex === -1) return false;
  const namespaceName = value.slice(0, dotIndex);
  const exportedName = value.slice(dotIndex + 1);
  return bindings.namespaceExportNames.get(namespaceName)?.has(exportedName) ?? false;
}

function exportedNamesForTarget(repoIndex: RepoIndex, target: CollectedSymbol, targetPackage: PackageInfo): ExportedNamesForTarget {
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

function importedBindingsForTarget(
  entry: RepoFileIndexEntry,
  repo: string,
  target: CollectedSymbol,
  targetPackage: PackageInfo,
  exportedNames: ExportedNamesForTarget,
): ImportedBindingsForTarget {
  const bindings = {
    localNames: new Map<string, Reference>(),
    namespaceLocalNames: new Map<string, Reference>(),
    namespaceExportNames: new Map<string, Set<string>>(),
  };
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

function isIdentifierMatchedByImportedBindings(
  identifier: IdentifierIndexEntry,
  bindings: ImportedBindingsForTarget,
): boolean {
  if (bindings.localNames.has(identifier.name)) return true;
  if (identifier.namespaceQualifier === null || !bindings.namespaceLocalNames.has(identifier.namespaceQualifier)) {
    return false;
  }
  return bindings.namespaceExportNames.get(identifier.namespaceQualifier)?.has(identifier.name) ?? false;
}
