package analyzer

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"golang.org/x/tools/go/packages"
	"os"
	"path/filepath"
	"strings"
)

func fallbackFileAnalysis(repo string, path string, ranges []LineRange, deleted []DeletedLine) (FileAnalysis, string, bool) {
	abs, ok := safeRepoPath(repo, path)
	if !ok {
		return FileAnalysis{}, "Unsafe Go file path " + path + "; using diff-only fallback context.", true
	}
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, abs, nil, parser.ParseComments)
	if err != nil {
		return FileAnalysis{}, fmt.Sprintf("Unable to parse Go file %s: %s", path, err), true
	}
	source, err := os.ReadFile(abs)
	if err != nil {
		return FileAnalysis{}, fmt.Sprintf("Unable to read Go file %s: %s", path, err), true
	}
	info := &fileInfo{
		path:        path,
		absPath:     abs,
		source:      source,
		fset:        fset,
		file:        file,
		packageName: file.Name.Name,
	}
	info.imports = collectImports(file)
	ws := &workspace{
		repo:       repo,
		files:      map[string]*fileInfo{},
		objects:    map[types.Object]*symbolInfo{},
		objectKeys: map[string]*symbolInfo{},
	}
	collectFallbackSymbols(ws, info)
	changed := changedSymbolsForFile(info, ranges, deleted)
	return fileAnalysis(info, changed, relatedTests(ws, info, changed)),
		"Go analyzer used syntax-only fallback for " + path + " because semantic package loading did not include it.", false
}

func safeRepoPath(repo string, rel string) (string, bool) {
	if rel == "" || filepath.IsAbs(rel) || strings.HasPrefix(rel, "../") {
		return "", false
	}
	repoAbs, err := filepath.Abs(repo)
	if err != nil {
		return "", false
	}
	candidateAbs, err := filepath.Abs(filepath.Join(repoAbs, filepath.FromSlash(rel)))
	if err != nil {
		return "", false
	}
	repoResolved, err := filepath.EvalSymlinks(repoAbs)
	if err != nil {
		return "", false
	}
	candidateResolved, err := filepath.EvalSymlinks(candidateAbs)
	if err != nil {
		return "", false
	}
	relBack, err := filepath.Rel(repoResolved, candidateResolved)
	if err != nil || relBack == ".." || strings.HasPrefix(relBack, ".."+string(filepath.Separator)) {
		return "", false
	}
	return candidateResolved, true
}

func collectFallbackSymbols(ws *workspace, info *fileInfo) {
	info.pkg = &packages.Package{TypesInfo: &types.Info{Defs: map[*ast.Ident]types.Object{}}}
	collectSymbols(ws, info)
}
