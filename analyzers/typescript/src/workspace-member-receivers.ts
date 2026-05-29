import ts from "typescript";

import { expressionNameText } from "./ast-utils.js";
import type {
  CollectedSymbol,
  IdentifierIndexEntry,
  ImportedBindingsForTarget,
  ReceiverIndexEntry,
  Reference,
  RepoFileIndexEntry,
  RepoIndex,
} from "./types.js";

export function inferredMemberIdentifiers(reference: Reference, memberName: string): IdentifierIndexEntry[] {
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

export function isMemberReferenceForTarget(
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

export function memberIdentifierHasValidReceiver(
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

export function memberReceiverTypeNames(repoIndex: RepoIndex, target: CollectedSymbol): Set<string> {
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

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
