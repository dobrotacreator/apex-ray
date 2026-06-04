import fs from "node:fs";

import type { PackageInfo } from "./types.js";

export function readPackageInfo(root: string, packageJsonPath: string): PackageInfo | null {
  try {
    const parsed = JSON.parse(fs.readFileSync(packageJsonPath, "utf-8")) as {
      name?: unknown;
      exports?: unknown;
      main?: unknown;
      module?: unknown;
      types?: unknown;
      typings?: unknown;
    };
    if (typeof parsed.name !== "string" || parsed.name.length === 0) return null;
    return {
      root,
      name: parsed.name,
      exports: parsed.exports,
      main: typeof parsed.main === "string" ? parsed.main : null,
      module: typeof parsed.module === "string" ? parsed.module : null,
      types: typeof parsed.types === "string" ? parsed.types : null,
      typings: typeof parsed.typings === "string" ? parsed.typings : null,
    };
  } catch {
    return null;
  }
}
