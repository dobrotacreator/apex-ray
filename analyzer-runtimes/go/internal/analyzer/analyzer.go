package analyzer

import (
	"sort"
)

func Analyze(args Args) AnalyzerResult {
	result := AnalyzerResult{
		Language:      "go",
		ProjectRoot:   args.Repo,
		Files:         []FileAnalysis{},
		Warnings:      []string{},
		FailedFiles:   []string{},
		ShardFailures: []ShardFailure{},
	}
	changed := validChangedPaths(args)
	deletedOnly := validDeletedOnlyLines(args, changed)
	if len(changed) == 0 && len(deletedOnly) == 0 {
		return result
	}

	if len(changed) > 0 {
		pkgs, loadWarnings := loadPackages(args)
		result.Warnings = append(result.Warnings, loadWarnings...)
		ws := buildWorkspace(args.Repo, pkgs, &result)
		changedByPath := map[string][]*symbolInfo{}
		for _, path := range changed {
			info := ws.files[path]
			if info == nil {
				continue
			}
			changedByPath[path] = changedSymbolsForFile(info, args.ChangedRanges[path], args.DeletedLines[path])
		}
		enrichChangedSymbols(ws, changedByPath)

		for _, path := range changed {
			if info := ws.files[path]; info != nil {
				result.Files = append(result.Files, fileAnalysis(info, changedByPath[path], relatedTests(ws, info, changedByPath[path])))
				continue
			}
			fallback, warning, failed := fallbackFileAnalysis(args.Repo, path, args.ChangedRanges[path], args.DeletedLines[path])
			if warning != "" {
				result.Warnings = append(result.Warnings, warning)
				result.Partial = true
			}
			if failed {
				result.FailedFiles = append(result.FailedFiles, path)
				continue
			}
			result.Files = append(result.Files, fallback)
			result.Partial = true
		}
	}
	for _, path := range sortedDeletedPaths(deletedOnly) {
		result.Files = append(result.Files, deletedFileAnalysis(path, deletedOnly[path]))
	}
	if len(result.Warnings) > 0 || len(result.FailedFiles) > 0 {
		result.Partial = true
	}
	sort.Slice(result.Files, func(i, j int) bool { return result.Files[i].Path < result.Files[j].Path })
	return result
}
