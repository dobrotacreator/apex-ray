package analyzer

import (
	"path"
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
	slashPath := strings.ReplaceAll(normalized, "\\", "/")
	cleaned := path.Clean(slashPath)
	return cleaned != "." &&
		cleaned != ".." &&
		!strings.HasPrefix(cleaned, "../") &&
		!strings.HasPrefix(cleaned, "/") &&
		!hasWindowsDrivePrefix(cleaned) &&
		path.Ext(cleaned) == ".go"
}

func hasWindowsDrivePrefix(value string) bool {
	if len(value) < 2 || value[1] != ':' {
		return false
	}
	letter := value[0]
	return (letter >= 'A' && letter <= 'Z') || (letter >= 'a' && letter <= 'z')
}
