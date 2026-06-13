package analyzer

import (
	"path/filepath"
	"sort"
	"strings"
)

func validChangedPaths(args Args) []string {
	seen := map[string]struct{}{}
	var paths []string
	for _, path := range args.Changed {
		normalized := normalizeRelPath(path)
		if !validGoPath(normalized) {
			continue
		}
		if _, ok := seen[normalized]; ok {
			continue
		}
		seen[normalized] = struct{}{}
		paths = append(paths, normalized)
	}
	sort.Strings(paths)
	return paths
}

func validDeletedOnlyLines(args Args, changed []string) map[string][]DeletedLine {
	changedSet := map[string]struct{}{}
	for _, path := range changed {
		changedSet[path] = struct{}{}
	}
	deletedOnly := map[string][]DeletedLine{}
	for path, lines := range args.DeletedLines {
		normalized := normalizeRelPath(path)
		if !validGoPath(normalized) || len(lines) == 0 {
			continue
		}
		if _, ok := changedSet[normalized]; ok {
			continue
		}
		deletedOnly[normalized] = append(deletedOnly[normalized], lines...)
	}
	return deletedOnly
}

func sortedDeletedPaths(deleted map[string][]DeletedLine) []string {
	paths := make([]string, 0, len(deleted))
	for path := range deleted {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}

func validGoPath(normalized string) bool {
	return normalized != "." &&
		!strings.HasPrefix(normalized, "../") &&
		!filepath.IsAbs(normalized) &&
		filepath.Ext(normalized) == ".go"
}
