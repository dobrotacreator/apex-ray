import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

export function writeFile(root: string, relativePath: string, content: string): void {
  const target = path.join(root, relativePath);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, "utf8");
}

function normalizePath(value: string): string {
  return value.replaceAll("\\", "/");
}

export function assertIncludesPath(candidates: string[], expectedPath: string): void {
  const normalized = normalizePath(path.resolve(expectedPath));
  assert.ok(candidates.includes(normalized), `Expected ${normalized} in ${JSON.stringify(candidates)}`);
}
