import path from "node:path";

import { analyze, type Args } from "./analyzer.js";

export function runAnalyzerCli(argv = process.argv.slice(2), stdout = process.stdout): void {
  const args = parseArgs(argv);
  const result = analyze(args);
  stdout.write(JSON.stringify(result, null, 2));
}

export function parseArgs(argv: string[]): Args {
  let repo: string | null = null;
  const changed: string[] = [];
  const changedRanges = new Map<string, Array<[number, number]>>();
  const deletedLines = new Map<string, Array<{ line: number; text: string }>>();
  let indexCacheEnabled = true;
  let indexCacheDir: string | null = null;
  let refreshIndexCache = false;
  let largeChangeSetSize: number | null = null;
  let analysisTimeBudgetMs: number | null = null;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--repo") {
      repo = argv[++i] ?? null;
    } else if (arg === "--changed") {
      while (i + 1 < argv.length && !argv[i + 1].startsWith("--")) {
        changed.push(normalizeRelPath(argv[++i]));
      }
    } else if (arg === "--range") {
      const value = argv[++i];
      const parsed = parseRange(value);
      if (parsed) {
        const ranges = changedRanges.get(parsed.file) ?? [];
        ranges.push([parsed.start, parsed.end]);
        changedRanges.set(parsed.file, ranges);
      }
    } else if (arg === "--deleted-line") {
      const file = normalizeRelPath(argv[++i] ?? "");
      const line = Number(argv[++i] ?? "0");
      const text = argv[++i] ?? "";
      if (file && Number.isFinite(line) && line > 0) {
        const lines = deletedLines.get(file) ?? [];
        lines.push({ line, text });
        deletedLines.set(file, lines);
      }
    } else if (arg === "--no-index-cache") {
      indexCacheEnabled = false;
    } else if (arg === "--index-cache-dir") {
      indexCacheDir = argv[++i] ?? null;
    } else if (arg === "--refresh-index-cache") {
      refreshIndexCache = true;
    } else if (arg === "--large-change-set-size") {
      const value = Number(argv[++i] ?? "0");
      largeChangeSetSize = Number.isFinite(value) && value > 0 ? value : null;
    } else if (arg === "--analysis-time-budget-ms") {
      const value = Number(argv[++i] ?? "0");
      analysisTimeBudgetMs = Number.isFinite(value) && value >= 0 ? value : null;
    }
  }

  if (!repo) {
    throw new Error("Missing --repo");
  }

  return {
    repo: path.resolve(repo),
    changed,
    changedRanges,
    deletedLines,
    indexCacheEnabled,
    indexCacheDir,
    refreshIndexCache,
    largeChangeSetSize,
    analysisTimeBudgetMs,
  };
}

function parseRange(value: string | undefined): { file: string; start: number; end: number } | null {
  if (!value) return null;
  const match = /^(.*):(\d+)-(\d+)$/.exec(value);
  if (!match) return null;
  return {
    file: normalizeRelPath(match[1]),
    start: Number(match[2]),
    end: Number(match[3]),
  };
}

function normalizeRelPath(value: string): string {
  return value.replaceAll("\\", "/");
}
