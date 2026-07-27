import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { parseArgs } from "../dist/cli.js";
import { buildRepoIndex } from "../dist/indexes/repo.js";
import { repoIndexCachePath } from "../dist/indexes/repo-cache.js";
import { loadRepoFileInventory } from "../dist/workspace/inventory.js";
import { writeFile } from "./helpers.js";

test("repo index cold build and warm cache produce equivalent semantic output", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-"));
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-cache-"));
  try {
    writeFile(
      repo,
      "src/cart.ts",
      [
        "export class CartService {",
        "  total(price: number): number {",
        "    return price;",
        "  }",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/cart.test.ts",
      [
        "import { CartService } from './cart.js';",
        "",
        "const service = new CartService();",
        "service.total(1);",
      ].join("\n"),
    );

    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/cart.ts",
      "--index-cache-dir",
      cacheDir,
    ]);
    const first = buildRepoIndex(args);
    const second = buildRepoIndex(args);
    const testEntry = first.files.find((entry) => entry.relPath === "src/cart.test.ts");

    assert.ok(testEntry);
    assert.ok(testEntry.imports.some((entry) => entry.moduleSpecifier === "./cart.js"));
    assert.ok(testEntry.identifiers.some((entry) => entry.name === "CartService"));
    assert.ok(testEntry.receivers.some((entry) => entry.receiverName === "service" && entry.typeName === "CartService"));
    assert.equal(first.cacheStats?.misses, 2);
    assert.equal(first.cacheStats?.written, true);
    assert.equal(second.cacheStats?.hits, 2);
    assert.equal(second.cacheStats?.misses, 0);
    assert.deepEqual(second.files, first.files);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo index builder limits indexing to a file manifest", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-"));
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-cache-"));
  try {
    writeFile(repo, "src/cart.ts", "export const cart = true;\n");
    writeFile(repo, "src/consumer.ts", "export const consumer = true;\n");
    writeFile(repo, ".venv/vendor.min.js", "const ignored = true;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/cart.ts", "src/consumer.ts"] }),
      "utf8",
    );

    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/cart.ts",
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const first = buildRepoIndex(args);
    const second = buildRepoIndex(args);

    assert.deepEqual(
      first.files.map((entry) => entry.relPath),
      ["src/cart.ts", "src/consumer.ts"],
    );
    assert.equal(first.cacheStats?.misses, 2);
    assert.equal(first.cacheStats?.written, true);
    assert.equal(second.cacheStats?.hits, 2);
    assert.equal(second.cacheStats?.misses, 0);

    fs.renameSync(
      path.join(repo, "src", "consumer.ts"),
      path.join(repo, "src", "renamed.ts"),
    );
    writeFile(repo, "src/new.ts", "export const added = true;\n");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/cart.ts", "src/renamed.ts", "src/new.ts"],
      }),
      "utf8",
    );
    const reconciled = buildRepoIndex(args);
    assert.deepEqual(
      reconciled.files.map((entry) => entry.relPath),
      ["src/cart.ts", "src/new.ts", "src/renamed.ts"],
    );
    assert.equal(reconciled.cacheStats?.hits, 1);
    assert.equal(reconciled.cacheStats?.misses, 2);
    assert.equal(reconciled.cacheStats?.written, true);

    fs.writeFileSync(manifestPath, JSON.stringify({ version: 2, files: ["src/cart.ts"] }), "utf8");
    const narrowed = buildRepoIndex(args);
    assert.deepEqual(narrowed.files.map((entry) => entry.relPath), ["src/cart.ts"]);
    assert.equal(narrowed.cacheStats?.hits, 1);
    assert.equal(narrowed.cacheStats?.misses, 0);
    assert.equal(narrowed.cacheStats?.written, true);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo index cache rejects a replaced file with restored size and mtime", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-cache-identity-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-cache-identity-cache-"),
  );
  try {
    const sourcePath = path.join(repo, "src", "changed.ts");
    writeFile(repo, "src/changed.ts", "export const alphaName = 1;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);

    const first = buildRepoIndex(args);
    const originalStat = fs.statSync(sourcePath);
    const replacementPath = path.join(repo, "src", "replacement.ts");
    fs.writeFileSync(
      replacementPath,
      "export const bravoName = 1;\n",
      "utf8",
    );
    fs.renameSync(replacementPath, sourcePath);
    fs.utimesSync(sourcePath, originalStat.atime, originalStat.mtime);

    const second = buildRepoIndex(args);
    const identifierNames = second.files.flatMap((entry) =>
      entry.identifiers.map((identifier) => identifier.name)
    );

    assert.equal(first.cacheStats?.written, true);
    assert.equal(second.cacheStats?.hits, 0);
    assert.equal(second.cacheStats?.misses, 1);
    assert.ok(identifierNames.includes("bravoName"));
    assert.equal(identifierNames.includes("alphaName"), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo index builder rejects manifest paths outside the repository", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-boundary-"));
  try {
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(manifestPath, JSON.stringify({ version: 2, files: ["../outside.ts"] }), "utf8");
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/cart.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.throws(() => buildRepoIndex(args), /outside the repository/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo inventory rejects obsolete version-one manifests", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-manifest-version-"),
  );
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 1, files: ["src/changed.ts"] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.throws(
      () => loadRepoFileInventory(args),
      /Invalid TypeScript file manifest/,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo inventory skips manifest symlinks outside the repository and reports partial context", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-symlink-"));
  const outsideRoot = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-outside-"));
  try {
    writeFile(repo, "src/cart.ts", "export const cart = true;\n");
    writeFile(outsideRoot, "outside.d.ts", "declare const outsideSecret: string;\n");
    fs.mkdirSync(path.join(repo, "src"), { recursive: true });
    fs.symlinkSync(path.join(outsideRoot, "outside.d.ts"), path.join(repo, "src", "outside.d.ts"));
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/outside.d.ts"] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/cart.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args);
    const index = buildRepoIndex(args, [], inventory);

    assert.equal(inventory.partial, true);
    assert.match(inventory.partialReason ?? "", /could not be read safely/);
    assert.equal(
      inventory.absPaths.includes(path.join(repo, "src", "outside.d.ts")),
      false,
    );
    assert.equal(
      inventory.pathKeys.has(path.join(outsideRoot, "outside.d.ts")),
      false,
    );
    assert.deepEqual(index.files.map((entry) => entry.relPath), ["src/cart.ts"]);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outsideRoot, { recursive: true, force: true });
  }
});

test("repo inventory excludes a changed source symlink from readable paths", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-manifest-internal-symlink-"),
  );
  try {
    writeFile(repo, "src/real.ts", "export const value = true;\n");
    fs.symlinkSync("real.ts", path.join(repo, "src", "link.ts"));
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/link.ts"] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/link.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args);
    const index = buildRepoIndex(args, [], inventory);

    assert.equal(inventory.partial, true);
    assert.match(inventory.partialReason ?? "", /could not be read safely/);
    assert.equal(
      inventory.absPaths.includes(path.join(repo, "src", "link.ts")),
      false,
    );
    assert.equal(
      inventory.pathKeys.has(path.join(repo, "src", "link.ts")),
      false,
    );
    assert.equal(
      inventory.pathKeys.has(path.join(repo, "src", "real.ts")),
      false,
    );
    assert.deepEqual(index.files, []);
    assert.equal(index.partial, true);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo inventory skips symlinked package and config metadata without aborting", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-manifest-metadata-symlink-"),
  );
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "metadata/package-real.json", JSON.stringify({ name: "private-package" }));
    writeFile(repo, "metadata/config-real.json", JSON.stringify({ compilerOptions: { strict: true } }));
    fs.symlinkSync("package-real.json", path.join(repo, "metadata", "package.json"));
    fs.symlinkSync("config-real.json", path.join(repo, "metadata", "base.config"));
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/changed.ts"],
        package_files: ["metadata/package.json"],
        config_files: ["metadata/base.config"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args);

    assert.equal(inventory.partial, true);
    assert.deepEqual(inventory.packageJsonAbsPaths, []);
    assert.deepEqual(inventory.configJsonAbsPaths, []);
    assert.equal(
      inventory.packagePathKeys.has(path.join(repo, "metadata", "package-real.json")),
      false,
    );
    assert.equal(
      inventory.configPathKeys.has(path.join(repo, "metadata", "config-real.json")),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo inventory accepts explicitly bounded config metadata with arbitrary extensions", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-manifest-config-extension-"),
  );
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "configs/base.jsonc", '{\n  // shared\n  "compilerOptions": {}\n}\n');
    writeFile(repo, "configs/strict", '{"compilerOptions":{"strict":true}}\n');
    writeFile(repo, "configs/browser.config", '{"extends":"./strict"}\n');
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/changed.ts"],
        config_files: [
          "configs/base.jsonc",
          "configs/strict",
          "configs/browser.config",
        ],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args);

    assert.equal(inventory.partial, false);
    assert.deepEqual(
      inventory.configJsonAbsPaths.map((fileName) =>
        path.relative(repo, fileName).replaceAll("\\", "/")
      ),
      ["configs/base.jsonc", "configs/browser.config", "configs/strict"],
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("manifest validation resolves each unique inventory path at most once", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-manifest-realpath-cache-"),
  );
  const originalRealpathSync = fs.realpathSync;
  try {
    const sourceFiles: string[] = [];
    const packageFiles: string[] = [];
    for (let index = 0; index < 30; index += 1) {
      const sourceFile = `packages/pkg-${index}/src/index.ts`;
      const packageFile = `packages/pkg-${index}/package.json`;
      writeFile(repo, sourceFile, `export const value${index} = ${index};\n`);
      writeFile(repo, packageFile, JSON.stringify({ name: `pkg-${index}` }));
      sourceFiles.push(sourceFile);
      packageFiles.push(packageFile);
    }
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: sourceFiles,
        package_files: packageFiles,
        config_files: packageFiles,
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      sourceFiles[0]!,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    let realpathCalls = 0;
    fs.realpathSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      realpathCalls += 1;
      return (originalRealpathSync as (...args: unknown[]) => fs.PathLike)(
        candidate,
        ...rest,
      );
    }) as typeof fs.realpathSync;

    const inventory = loadRepoFileInventory(args);

    assert.equal(inventory.partial, false);
    assert.ok(
      realpathCalls <= sourceFiles.length + packageFiles.length + 2,
      `expected unique-path realpath validation, got ${realpathCalls} calls`,
    );
  } finally {
    fs.realpathSync = originalRealpathSync;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test(
  "repo inventory path keys follow the TypeScript filesystem casing rules",
  { skip: ts.sys.useCaseSensitiveFileNames },
  () => {
    const repo = fs.mkdtempSync(
      path.join(os.tmpdir(), "apex-ray-ts-index-manifest-case-"),
    );
    try {
      writeFile(repo, "src/ZConsumer.ts", "export const consumer = true;\n");
      const manifestPath = path.join(repo, "typescript-files.json");
      fs.writeFileSync(
        manifestPath,
        JSON.stringify({ version: 2, files: ["src/ZConsumer.ts"] }),
        "utf8",
      );
      const args = parseArgs([
        "--repo",
        repo,
        "--changed",
        "src/ZConsumer.ts",
        "--file-manifest",
        manifestPath,
        "--no-index-cache",
      ]);

      const inventory = loadRepoFileInventory(args);

      assert.equal(
        inventory.pathKeys.has(
          path.resolve(repo, "src/zconsumer.ts").replaceAll("\\", "/").toLowerCase(),
        ),
        true,
      );
    } finally {
      fs.rmSync(repo, { recursive: true, force: true });
    }
  },
);

test("fallback repo inventory is bounded and retains changed source files", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-fallback-boundary-"));
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-fallback-cache-"));
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "src/other.ts", "export const other = true;\n");
    writeFile(repo, "src/types.d.mts", "declare const ambient: string;\n");
    writeFile(repo, "assets/large.bin", "irrelevant\n");
    writeFile(repo, "assets/readme.txt", "irrelevant\n");
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--no-index-cache",
    ]);

    const fileLimitedInventory = loadRepoFileInventory(args, {
      maxEntries: 100,
      maxFiles: 1,
    });
    const entryLimitedInventory = loadRepoFileInventory(args, {
      maxEntries: 1,
      maxFiles: 100,
    });

    assert.equal(fileLimitedInventory.partial, true);
    assert.match(fileLimitedInventory.partialReason ?? "", /1 source files/);
    assert.equal(entryLimitedInventory.partial, true);
    assert.match(entryLimitedInventory.partialReason ?? "", /1 filesystem entries/);
    assert.ok(
      [fileLimitedInventory, entryLimitedInventory].every((inventory) =>
        inventory.absPaths.includes(path.join(repo, "src/changed.ts")),
      ),
    );
    assert.ok(
      fileLimitedInventory.absPaths.every((fileName) =>
        /\.(?:[cm]?[jt]s|[jt]sx)$/i.test(fileName),
      ),
    );
    assert.ok(fileLimitedInventory.absPaths.length <= 2);

    const cachedArgs = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--index-cache-dir",
      cacheDir,
    ]);
    const partialIndex = buildRepoIndex(cachedArgs, [], fileLimitedInventory);
    assert.equal(partialIndex.cacheStats?.written, false);
    assert.equal(fs.existsSync(repoIndexCachePath(repo, cacheDir)), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("fallback repo inventory is partial when a source directory cannot be opened", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-fallback-unreadable-"),
  );
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "hidden/private.ts", "export const privateValue = true;\n");
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args, {
      openDirectory: (directoryPath) => {
        if (path.basename(directoryPath) === "hidden") {
          throw new Error("simulated directory read failure");
        }
        return fs.opendirSync(directoryPath);
      },
    });

    assert.equal(inventory.partial, true);
    assert.match(inventory.partialReason ?? "", /could not read directory hidden/);
    assert.equal(
      inventory.absPaths.some((fileName) => fileName.endsWith("private.ts")),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("fallback repo inventory is partial after a mid-scan directory read failure", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-fallback-read-failure-"),
  );
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "hidden/private.ts", "export const privateValue = true;\n");
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args, {
      openDirectory: (directoryPath) => {
        if (path.basename(directoryPath) === "hidden") {
          return {
            readSync: () => {
              throw new Error("simulated mid-scan failure");
            },
            closeSync: () => undefined,
          } as unknown as fs.Dir;
        }
        return fs.opendirSync(directoryPath);
      },
    });

    assert.equal(inventory.partial, true);
    assert.match(
      inventory.partialReason ?? "",
      /could not finish reading directory hidden/,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("fallback repo inventory reports changed external symlinks as partial without reading them", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-fallback-symlink-"));
  const outsideRoot = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-fallback-outside-"));
  try {
    writeFile(outsideRoot, "secret.ts", 'export const token = "TOP_SECRET_TOKEN";\n');
    fs.mkdirSync(path.join(repo, "src"), { recursive: true });
    fs.symlinkSync(path.join(outsideRoot, "secret.ts"), path.join(repo, "src", "changed.ts"));
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args);
    const earlyStoppedInventory = loadRepoFileInventory(args, {
      shouldStop: () => true,
    });
    let stopChecks = 0;
    const midScanStoppedInventory = loadRepoFileInventory(args, {
      shouldStop: () => {
        stopChecks += 1;
        return stopChecks >= 2;
      },
    });
    const warnings: string[] = [];
    const index = buildRepoIndex(args, warnings, inventory);

    assert.equal(inventory.partial, true);
    assert.equal(
      inventory.absPaths.includes(path.join(repo, "src", "changed.ts")),
      false,
    );
    assert.equal(
      inventory.pathKeys.has(path.join(repo, "src", "changed.ts")),
      false,
    );
    assert.equal(
      inventory.pathKeys.has(path.join(outsideRoot, "secret.ts")),
      false,
    );
    assert.equal(JSON.stringify(index).includes("TOP_SECRET_TOKEN"), false);
    assert.equal(index.partial, true);
    for (const stoppedInventory of [
      earlyStoppedInventory,
      midScanStoppedInventory,
    ]) {
      assert.equal(stoppedInventory.absPaths.length, 0);
      assert.match(
        stoppedInventory.partialReason ?? "",
        /could not be read safely/,
      );
      assert.match(
        stoppedInventory.partialReason ?? "",
        /analysis time budget/,
      );
    }
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outsideRoot, { recursive: true, force: true });
  }
});

test("manifest early partial inventory excludes unsafe changed symlinks", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-manifest-early-symlink-"),
  );
  const outsideRoot = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-manifest-early-target-"),
  );
  try {
    writeFile(
      outsideRoot,
      "secret.ts",
      'export const PRIVATE_EARLY_NAME = "PRIVATE_EARLY_LITERAL";\n',
    );
    fs.mkdirSync(path.join(repo, "src"), { recursive: true });
    fs.symlinkSync(
      path.join(outsideRoot, "secret.ts"),
      path.join(repo, "src", "changed.ts"),
    );
    writeFile(repo, "src/first.ts", "export const first = true;\n");
    writeFile(repo, "src/second.ts", "export const second = true;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/first.ts", "src/second.ts"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args, {
      shouldStop: () => true,
    });
    const limitedInventory = loadRepoFileInventory(args, {
      maxFiles: 1,
    });

    assert.equal(inventory.partial, true);
    assert.match(inventory.partialReason ?? "", /could not be read safely/);
    assert.equal(inventory.absPaths.length, 0);
    assert.equal(
      inventory.pathKeys.has(path.join(repo, "src", "changed.ts")),
      false,
    );
    assert.equal(
      inventory.pathKeys.has(path.join(outsideRoot, "secret.ts")),
      false,
    );
    assert.match(
      limitedInventory.partialReason ?? "",
      /could not be read safely/,
    );
    assert.match(limitedInventory.partialReason ?? "", /1 source files/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outsideRoot, { recursive: true, force: true });
  }
});

test("fallback inventory rejects a source whose parent is swapped outside during validation", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-fallback-parent-swap-"),
  );
  const outsideRoot = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-fallback-parent-target-"),
  );
  const originalRealpathSync = fs.realpathSync;
  try {
    const dependencyPath = path.join(repo, "lib", "dependency.ts");
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "lib/dependency.ts", "export const publicName = true;\n");
    writeFile(
      outsideRoot,
      "dependency.ts",
      'export const PRIVATE_FALLBACK_NAME = "PRIVATE_FALLBACK_LITERAL";\n',
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--no-index-cache",
    ]);
    let swapped = false;
    fs.realpathSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      if (
        !swapped &&
        path.resolve(String(candidate)) === dependencyPath
      ) {
        swapped = true;
        fs.rmSync(path.dirname(dependencyPath), { recursive: true });
        fs.symlinkSync(outsideRoot, path.dirname(dependencyPath));
      }
      return (originalRealpathSync as (...args: unknown[]) => fs.PathLike)(
        candidate,
        ...rest,
      );
    }) as typeof fs.realpathSync;

    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    const index = buildRepoIndex(args, warnings, inventory);

    assert.equal(swapped, true);
    assert.equal(JSON.stringify(index).includes("PRIVATE_FALLBACK"), false);
    assert.equal(index.partial, true);
    assert.equal(inventory.partial, true);
  } finally {
    fs.realpathSync = originalRealpathSync;
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outsideRoot, { recursive: true, force: true });
  }
});

test("fallback inventory does not traverse a queued directory swapped outside before open", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-fallback-directory-swap-"),
  );
  const outsideRoot = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-fallback-directory-target-"),
  );
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "queued/public.ts", "export const publicName = true;\n");
    writeFile(
      outsideRoot,
      "private.ts",
      'export const PRIVATE_DIRECTORY_NAME = "PRIVATE_DIRECTORY_LITERAL";\n',
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--no-index-cache",
    ]);
    let outsideReads = 0;
    let swapped = false;

    const inventory = loadRepoFileInventory(args, {
      openDirectory: (directoryPath) => {
        if (!swapped && path.basename(directoryPath) === "queued") {
          swapped = true;
          fs.rmSync(directoryPath, { recursive: true });
          fs.symlinkSync(outsideRoot, directoryPath);
          const outsideDirectory = fs.opendirSync(directoryPath);
          return {
            readSync: () => {
              outsideReads += 1;
              return outsideDirectory.readSync();
            },
            closeSync: () => outsideDirectory.closeSync(),
          } as unknown as fs.Dir;
        }
        return fs.opendirSync(directoryPath);
      },
    });

    assert.equal(swapped, true);
    assert.equal(outsideReads, 0);
    assert.equal(inventory.partial, true);
    assert.equal(
      inventory.absPaths.some((fileName) =>
        fileName.includes("PRIVATE_DIRECTORY")
      ),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outsideRoot, { recursive: true, force: true });
  }
});

test("fallback validation resolves each unique inventory path at most once", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-fallback-realpath-cache-"),
  );
  const originalRealpathSync = fs.realpathSync;
  try {
    const sourceCount = 30;
    const packageCount = 30;
    for (let index = 0; index < sourceCount; index += 1) {
      writeFile(
        repo,
        `src/file-${index}.ts`,
        `export const value${index} = ${index};\n`,
      );
    }
    for (let index = 0; index < packageCount; index += 1) {
      writeFile(
        repo,
        `packages/pkg-${index}/package.json`,
        JSON.stringify({ name: `pkg-${index}` }),
      );
    }
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/file-0.ts",
      "--no-index-cache",
    ]);
    let realpathCalls = 0;
    fs.realpathSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      realpathCalls += 1;
      return (originalRealpathSync as (...args: unknown[]) => fs.PathLike)(
        candidate,
        ...rest,
      );
    }) as typeof fs.realpathSync;

    const inventory = loadRepoFileInventory(args);

    assert.equal(inventory.partial, false);
    assert.ok(
      realpathCalls <= sourceCount + packageCount * 2 + 6,
      `expected one validation per file and opened directory, got ${realpathCalls} calls`,
    );
  } finally {
    fs.realpathSync = originalRealpathSync;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("manifest repo inventory honors budget, byte, and source-file limits", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-limits-"));
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "src/other.ts", "export const other = true;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/changed.ts", "src/other.ts"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const budgetLimited = loadRepoFileInventory(args, {
      shouldStop: () => true,
    });
    const byteLimited = loadRepoFileInventory(args, {
      maxManifestBytes: 16,
    });
    const fileLimited = loadRepoFileInventory(args, {
      maxFiles: 1,
    });

    for (const inventory of [budgetLimited, byteLimited, fileLimited]) {
      assert.equal(inventory.partial, true);
      assert.ok(inventory.absPaths.includes(path.join(repo, "src/changed.ts")));
    }
    assert.match(budgetLimited.partialReason ?? "", /analysis time budget/);
    assert.match(byteLimited.partialReason ?? "", /manifest byte safety limit/);
    assert.match(fileLimited.partialReason ?? "", /1 source files/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo indexing rejects an oversized source before reading or caching it", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-oversized-source-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-oversized-source-cache-"),
  );
  const originalReadFileSync = fs.readFileSync;
  try {
    const oversizedPath = "src/oversized.ts";
    writeFile(
      repo,
      oversizedPath,
      `/*${"x".repeat(9 * 1024 * 1024)}*/\nexport const oversized = true;\n`,
    );
    writeFile(repo, "src/small.ts", "export const small = true;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [oversizedPath, "src/small.ts"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      oversizedPath,
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let oversizedReadAttempted = false;
    fs.readFileSync = ((
      candidate: fs.PathOrFileDescriptor,
      ...rest: unknown[]
    ) => {
      if (
        typeof candidate === "number" &&
        fs.fstatSync(candidate).size > 8 * 1024 * 1024
      ) {
        oversizedReadAttempted = true;
      }
      return (originalReadFileSync as (...args: unknown[]) => string | Buffer)(
        candidate,
        ...rest,
      );
    }) as typeof fs.readFileSync;

    const index = buildRepoIndex(args, warnings, inventory);

    assert.equal(oversizedReadAttempted, false);
    assert.deepEqual(
      index.files.map((entry) => entry.relPath),
      ["src/small.ts"],
    );
    assert.equal(index.partial, true);
    assert.equal(index.cacheStats?.written, false);
    assert.equal(fs.existsSync(repoIndexCachePath(repo, cacheDir)), false);
    assert.ok(
      warnings.some(
        (warning) =>
          warning.includes("repo index source budget reached") &&
          warning.includes("workspace references are partial"),
      ),
    );
  } finally {
    fs.readFileSync = originalReadFileSync;
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo indexing enforces the aggregate source byte ceiling before reads", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-aggregate-source-bytes-"),
  );
  const originalReadFileSync = fs.readFileSync;
  try {
    const sourcePaths = ["src/first.ts", "src/second.ts", "src/third.ts"];
    for (const [index, sourcePath] of sourcePaths.entries()) {
      writeFile(
        repo,
        sourcePath,
        `/*${String(index).repeat(3 * 1024 * 1024)}*/\nexport const value${index} = ${index};\n`,
      );
    }
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: sourcePaths,
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      sourcePaths[0],
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let sourceReadCount = 0;
    fs.readFileSync = ((
      candidate: fs.PathOrFileDescriptor,
      ...rest: unknown[]
    ) => {
      if (
        typeof candidate === "number" &&
        fs.fstatSync(candidate).size > 1024 * 1024
      ) {
        sourceReadCount += 1;
      }
      return (originalReadFileSync as (...args: unknown[]) => string | Buffer)(
        candidate,
        ...rest,
      );
    }) as typeof fs.readFileSync;

    const index = buildRepoIndex(args, warnings, inventory);

    assert.equal(sourceReadCount, 2);
    assert.deepEqual(
      index.files.map((entry) => entry.relPath),
      sourcePaths.slice(0, 2),
    );
    assert.ok(
      index.files.reduce((total, entry) => total + entry.size, 0) <=
        8 * 1024 * 1024,
    );
    assert.equal(index.partial, true);
    assert.ok(
      warnings.some((warning) =>
        warning.includes("repo index source budget reached"),
      ),
    );
  } finally {
    fs.readFileSync = originalReadFileSync;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo indexing caps semantic entries and suppresses oversized cache output", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-semantic-budget-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-semantic-budget-cache-"),
  );
  try {
    const changedPath = "src/dense.ts";
    writeFile(repo, changedPath, "dense;\n".repeat(60_000));
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: [changedPath] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];

    const index = buildRepoIndex(args, warnings, inventory);

    assert.equal(index.files.length, 1);
    assert.ok(index.files[0].identifiers.length <= 50_000);
    assert.equal(index.partial, true);
    assert.equal(index.cacheStats?.written, false);
    assert.equal(fs.existsSync(repoIndexCachePath(repo, cacheDir)), false);
    assert.ok(
      warnings.some(
        (warning) =>
          warning.includes("repo index semantic entry safety limit") &&
          warning.includes("workspace references are partial"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo indexing cancels collection inside a dense source file", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-collection-cancel-"),
  );
  try {
    const changedPath = "src/dense.ts";
    writeFile(repo, changedPath, "dense;\n".repeat(10_000));
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: [changedPath] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let checks = 0;

    const index = buildRepoIndex(
      args,
      warnings,
      inventory,
      () => ++checks > 100,
    );

    assert.ok(checks > 100);
    assert.equal(index.partial, true);
    assert.ok(index.files[0].identifiers.length < 10_000);
    assert.ok(
      warnings.some((warning) =>
        warning.includes(
          "repo index stopped because the analysis time budget was exhausted",
        ),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo indexing checks the deadline before reading a reusable cache", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-cache-deadline-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-cache-deadline-home-"),
  );
  const originalReadFileSync = fs.readFileSync;
  try {
    const changedPath = "src/changed.ts";
    writeFile(repo, changedPath, "export const changed = true;\n");
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const inventory = loadRepoFileInventory(args);
    const cachePath = repoIndexCachePath(repo, cacheDir);
    buildRepoIndex(args, [], inventory);
    assert.equal(fs.existsSync(cachePath), true);
    let cacheContentRead = false;
    fs.readFileSync = ((
      candidate: fs.PathOrFileDescriptor,
      ...rest: unknown[]
    ) => {
      if (
        candidate === cachePath ||
        (typeof candidate === "number" &&
          fs.fstatSync(candidate).ino === fs.statSync(cachePath).ino)
      ) {
        cacheContentRead = true;
      }
      return (originalReadFileSync as (...args: unknown[]) => string | Buffer)(
        candidate,
        ...rest,
      );
    }) as typeof fs.readFileSync;
    const warnings: string[] = [];

    const index = buildRepoIndex(args, warnings, inventory, () => true);

    assert.equal(cacheContentRead, false);
    assert.equal(index.partial, true);
    assert.ok(
      warnings.some((warning) =>
        warning.includes(
          "repo index stopped because the analysis time budget was exhausted",
        ),
      ),
    );
  } finally {
    fs.readFileSync = originalReadFileSync;
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo indexing prioritizes changed files, importers, tests, and config roots before broad inventory", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-relevance-order-"),
  );
  try {
    const broadPaths = Array.from(
      { length: 520 },
      (_, index) => `aa/file-${String(index).padStart(3, "0")}.ts`,
    );
    for (const [index, broadPath] of broadPaths.entries()) {
      writeFile(repo, broadPath, `export const broad${index} = ${index};\n`);
    }
    const changedPath = "zz/changed.ts";
    const importerPath = "zz/consumer.ts";
    const testPath = "zz/changed.test.ts";
    const configRootPath = "zz/config-root.ts";
    writeFile(repo, changedPath, "export const changed = true;\n");
    writeFile(
      repo,
      importerPath,
      'import { changed } from "./changed.js";\nexport { changed };\n',
    );
    writeFile(
      repo,
      testPath,
      'import { changed } from "./changed.js";\nvoid changed;\n',
    );
    writeFile(repo, configRootPath, "export const configRoot = true;\n");
    writeFile(
      repo,
      "zz/tsconfig.json",
      JSON.stringify({ files: ["config-root.ts"] }),
    );
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [
          ...broadPaths,
          changedPath,
          importerPath,
          testPath,
          configRootPath,
        ],
        config_files: ["zz/tsconfig.json"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];

    const index = buildRepoIndex(args, warnings, inventory);
    const retained = new Set(index.files.map((entry) => entry.relPath));

    assert.equal(index.files.length, 512);
    assert.equal(retained.has(changedPath), true);
    assert.equal(retained.has(importerPath), true);
    assert.equal(retained.has(testPath), true);
    assert.equal(retained.has(configRootPath), true);
    assert.equal(index.partial, true);
    assert.ok(
      warnings.some((warning) =>
        warning.includes("repo index source budget reached"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo indexing prioritizes bounded path-alias and package importers", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-aliased-relevance-"),
  );
  try {
    const broadPaths = Array.from(
      { length: 520 },
      (_, index) => `aa/file-${String(index).padStart(3, "0")}.ts`,
    );
    for (const [index, broadPath] of broadPaths.entries()) {
      writeFile(repo, broadPath, `export const broad${index} = ${index};\n`);
    }
    const changedPath = "zz/target.ts";
    const aliasImporterPath = "zz/alias-consumer.ts";
    const packageImporterPath = "zz/package-consumer.ts";
    writeFile(repo, changedPath, "export const target = true;\n");
    writeFile(
      repo,
      aliasImporterPath,
      'import { target } from "@app/target";\nexport { target };\n',
    );
    writeFile(
      repo,
      packageImporterPath,
      'import { target } from "@workspace/app";\nexport { target };\n',
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          baseUrl: ".",
          paths: { "@app/*": ["zz/*"] },
        },
      }),
    );
    writeFile(
      repo,
      "package.json",
      JSON.stringify({
        name: "@workspace/app",
        module: "zz/target.ts",
      }),
    );
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [
          ...broadPaths,
          changedPath,
          aliasImporterPath,
          packageImporterPath,
        ],
        config_files: ["tsconfig.json"],
        package_files: ["package.json"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);

    const index = buildRepoIndex(args, [], inventory);
    const retained = new Set(index.files.map((entry) => entry.relPath));

    assert.equal(retained.has(changedPath), true);
    assert.equal(retained.has(aliasImporterPath), true);
    assert.equal(retained.has(packageImporterPath), true);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo relevance reports bounded alias expansion as partial", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-alias-expansion-budget-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-alias-expansion-cache-"),
  );
  try {
    const broadPaths = Array.from(
      { length: 512 },
      (_, index) => `aa/file-${String(index).padStart(3, "0")}.ts`,
    );
    for (const [index, broadPath] of broadPaths.entries()) {
      writeFile(repo, broadPath, `export const broad${index} = ${index};\n`);
    }
    const changedPath = "zz/target.ts";
    const importerPath = "zz/consumer.ts";
    const wildcard = "x".repeat(8_192);
    writeFile(repo, changedPath, "export const target = true;\n");
    writeFile(
      repo,
      importerPath,
      `import "@amplified/${wildcard}";\n`,
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          baseUrl: ".",
          paths: {
            "@amplified/*": ["*".repeat(8_192)],
          },
        },
      }),
    );
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [...broadPaths, changedPath, importerPath],
        config_files: ["tsconfig.json"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];

    const index = buildRepoIndex(args, warnings, inventory);

    assert.equal(index.partial, true);
    assert.equal(index.cacheStats?.written, false);
    assert.ok(
      warnings.some(
        (warning) =>
          warning.includes("module target expansion safety limit") &&
          warning.includes("workspace references are partial"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo indexing stops on budget exhaustion without publishing a partial cache", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-budget-"));
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-budget-cache-"));
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "src/consumer.ts", "export const consumer = true;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/changed.ts", "src/consumer.ts"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let checks = 0;

    const index = buildRepoIndex(
      args,
      warnings,
      inventory,
      () => ++checks > 1,
    );

    assert.equal(index.partial, true);
    assert.equal(index.files.length, 1);
    assert.equal(index.cacheStats?.written, false);
    assert.equal(fs.existsSync(repoIndexCachePath(repo, cacheDir)), false);
    assert.ok(
      warnings.some((warning) =>
        warning.includes("repo index stopped because the analysis time budget was exhausted"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo indexing treats files lost after inventory as partial and does not cache", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-race-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-race-cache-"),
  );
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    writeFile(repo, "src/consumer.ts", "export const consumer = true;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/changed.ts", "src/consumer.ts"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const inventory = loadRepoFileInventory(args);
    fs.rmSync(path.join(repo, "src", "consumer.ts"));
    const warnings: string[] = [];

    const index = buildRepoIndex(args, warnings, inventory);

    assert.equal(index.partial, true);
    assert.equal(index.cacheStats?.written, false);
    assert.equal(fs.existsSync(repoIndexCachePath(repo, cacheDir)), false);
    assert.ok(
      warnings.some(
        (warning) =>
          warning.includes("src/consumer.ts") &&
          warning.includes("could not be read safely"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo indexing rejects a source swapped after path validation", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-swap-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-swap-target-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-index-swap-cache-"),
  );
  const originalLstatSync = fs.lstatSync;
  try {
    const indexedPath = path.join(repo, "src", "changed.ts");
    writeFile(
      repo,
      "src/changed.ts",
      "export const publicName = 1;\n",
    );
    const outsideSourcePath = path.join(outside, "private.ts");
    fs.writeFileSync(
      outsideSourcePath,
      'export const PRIVATE_CACHE_NAME = "PRIVATE_CACHE_LITERAL";\n',
      "utf8",
    );
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let swapped = false;

    fs.lstatSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      const stat = (originalLstatSync as (...args: unknown[]) => fs.Stats)(
        candidate,
        ...rest,
      );
      if (
        !swapped &&
        path.resolve(String(candidate)) === indexedPath
      ) {
        swapped = true;
        fs.rmSync(indexedPath);
        fs.symlinkSync(outsideSourcePath, indexedPath);
      }
      return stat;
    }) as typeof fs.lstatSync;

    const index = buildRepoIndex(args, warnings, inventory);

    assert.equal(swapped, true);
    assert.equal(
      JSON.stringify(index.files).includes("PRIVATE_CACHE"),
      false,
    );
    assert.equal(index.partial, true);
    assert.equal(index.cacheStats?.written, false);
    assert.equal(fs.existsSync(repoIndexCachePath(repo, cacheDir)), false);
  } finally {
    fs.lstatSync = originalLstatSync;
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});
