package analyzer

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
)

func relatedTests(ws *workspace, info *fileInfo, changed []*symbolInfo) []string {
	if strings.HasSuffix(info.path, "_test.go") {
		return []string{}
	}
	symbolNames := map[string]struct{}{}
	for _, symbol := range changed {
		symbolNames[strings.TrimPrefix(symbol.analysis.Name, receiverNamePrefix(symbol.analysis.Name))] = struct{}{}
	}
	sourceStem := strings.TrimSuffix(filepath.Base(info.path), ".go")
	sourceDir := pathDir(info.path)
	type scored struct {
		path  string
		score int
	}
	var candidates []scored
	for _, candidate := range goTestFiles(ws.repo, ws.files) {
		score := 0
		if pathDir(candidate.path) == sourceDir {
			score += 100
		}
		stem := strings.TrimSuffix(filepath.Base(candidate.path), "_test.go")
		if stem == sourceStem {
			score += 80
		}
		content := string(candidate.source)
		for name := range symbolNames {
			if name != "" && strings.Contains(content, name) {
				score += 40
			}
		}
		if score > 0 {
			candidates = append(candidates, scored{path: candidate.path, score: score})
		}
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].score == candidates[j].score {
			return candidates[i].path < candidates[j].path
		}
		return candidates[i].score > candidates[j].score
	})
	limit := relatedTestLimit
	if len(candidates) < limit {
		limit = len(candidates)
	}
	out := make([]string, 0, limit)
	for _, candidate := range candidates[:limit] {
		out = append(out, candidate.path)
	}
	sort.Strings(out)
	return out
}

func receiverNamePrefix(name string) string {
	if index := strings.LastIndex(name, "."); index >= 0 {
		return name[:index+1]
	}
	return ""
}

func pathDir(path string) string {
	dir := filepath.ToSlash(filepath.Dir(path))
	if dir == "." {
		return ""
	}
	return dir
}

func goTestFiles(repo string, loaded map[string]*fileInfo) []*fileInfo {
	seen := map[string]*fileInfo{}
	for path, info := range loaded {
		if strings.HasSuffix(path, "_test.go") {
			seen[path] = info
		}
	}
	_ = filepath.WalkDir(repo, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if entry.IsDir() {
			if shouldSkipWorkspacePath(normalizeRelPath(path)) && path != repo {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(entry.Name(), "_test.go") {
			return nil
		}
		rel, ok := repoRelPath(repo, path)
		if !ok || shouldSkipWorkspacePath(rel) {
			return nil
		}
		if _, ok := seen[rel]; ok {
			return nil
		}
		source, readErr := os.ReadFile(path)
		if readErr != nil {
			return nil
		}
		seen[rel] = &fileInfo{path: rel, absPath: path, source: source}
		return nil
	})
	out := make([]*fileInfo, 0, len(seen))
	for _, info := range seen {
		out = append(out, info)
	}
	return out
}
