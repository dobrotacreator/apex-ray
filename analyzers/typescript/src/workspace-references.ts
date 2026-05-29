import path from "node:path";

import ts from "typescript";

import { identifierFromExpression, propertyAssignmentNamed } from "./ast-utils.js";
import { findIndexedPackageForFile } from "./module-resolution.js";
import { addReference } from "./reference-merge.js";
import {
  inferredMemberIdentifiers,
  isMemberReferenceForTarget,
  memberIdentifierHasValidReceiver,
  memberReceiverTypeNames,
} from "./workspace-member-receivers.js";
import {
  emptyImportedBindings,
  importedBindingsForTarget,
  isIdentifierMatchedByImportedBindings,
} from "./workspace-import-bindings.js";
import { exportedNamesForTarget } from "./workspace-export-names.js";
import type { CollectedSymbol, Reference, RepoIndex } from "./types.js";

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
