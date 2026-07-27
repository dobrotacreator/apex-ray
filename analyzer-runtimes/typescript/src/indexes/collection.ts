import type { RepoFileIndexEntry } from "../types.js";

export interface IndexCollectionControl {
  shouldStop(): boolean;
  reserveEntry(): boolean;
  reserveEntries(count: number): boolean;
}

export function collectionShouldStop(
  control: IndexCollectionControl | undefined,
): boolean {
  return control?.shouldStop() ?? false;
}

export function appendIndexEntry<T>(
  entries: T[],
  entry: T,
  control?: IndexCollectionControl,
): boolean {
  if (control && !control.reserveEntry()) return false;
  entries.push(entry);
  return true;
}

export function semanticEntryCountForFile(
  file: Pick<
    RepoFileIndexEntry,
    | "imports"
    | "exports"
    | "identifiers"
    | "receivers"
    | "typeAliases"
    | "classHeritages"
    | "diProviders"
    | "diInjections"
  >,
): number {
  return (
    file.imports.length +
    file.imports.reduce(
      (total, entry) => total + entry.namedImports.length,
      0,
    ) +
    file.exports.length +
    file.identifiers.length +
    file.receivers.length +
    file.typeAliases.length +
    file.classHeritages.length +
    file.classHeritages.reduce(
      (total, entry) => total + entry.baseNames.length,
      0,
    ) +
    file.diProviders.length +
    file.diInjections.length
  );
}
