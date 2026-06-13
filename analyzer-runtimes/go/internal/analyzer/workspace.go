package analyzer

import (
	"context"
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"golang.org/x/tools/go/packages"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type workspace struct {
	repo        string
	files       map[string]*fileInfo
	symbols     []*symbolInfo
	objects     map[types.Object]*symbolInfo
	objectKeys  map[string]*symbolInfo
	typeSymbols []*symbolInfo
}

type fileInfo struct {
	path        string
	absPath     string
	source      []byte
	fset        *token.FileSet
	file        *ast.File
	pkg         *packages.Package
	symbols     []*symbolInfo
	imports     []string
	exports     []string
	packageName string
}

type symbolInfo struct {
	file     *fileInfo
	node     ast.Node
	object   types.Object
	analysis AnalyzerSymbol
}

type refSet map[string]struct{}

func loadPackages(args Args) ([]*packages.Package, []string) {
	budget := args.AnalysisTimeBudgetMS
	if budget <= 0 {
		budget = defaultBudgetMS
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(budget)*time.Millisecond)
	defer cancel()

	cfg := &packages.Config{
		Context: ctx,
		Dir:     args.Repo,
		Env:     goEnvWithReadonlyModules(),
		Mode: packages.NeedName |
			packages.NeedFiles |
			packages.NeedCompiledGoFiles |
			packages.NeedImports |
			packages.NeedDeps |
			packages.NeedSyntax |
			packages.NeedTypes |
			packages.NeedTypesInfo,
		Tests: true,
	}
	pkgs, err := packages.Load(cfg, "./...")
	var warnings []string
	if err != nil {
		warnings = append(warnings, "Go analyzer package load failed: "+err.Error())
	}
	if ctx.Err() != nil {
		warnings = append(warnings, "Go analyzer package load timed out.")
	}
	for _, pkg := range pkgs {
		for _, pkgErr := range pkg.Errors {
			warnings = append(warnings, fmt.Sprintf("Go package load warning for %s: %s", packageLabel(pkg), pkgErr.Msg))
		}
	}
	return pkgs, warnings
}

func goEnvWithReadonlyModules() []string {
	env := os.Environ()
	goFlags := os.Getenv("GOFLAGS")
	if !strings.Contains(goFlags, "-mod=") {
		goFlags = strings.TrimSpace(strings.TrimSpace(goFlags) + " -mod=readonly")
	}
	out := make([]string, 0, len(env)+1)
	for _, value := range env {
		if !strings.HasPrefix(value, "GOFLAGS=") {
			out = append(out, value)
		}
	}
	return append(out, "GOFLAGS="+goFlags)
}

func packageLabel(pkg *packages.Package) string {
	if pkg == nil {
		return "<unknown>"
	}
	if pkg.PkgPath != "" {
		return pkg.PkgPath
	}
	if pkg.ID != "" {
		return pkg.ID
	}
	return pkg.Name
}

func buildWorkspace(repo string, pkgs []*packages.Package, result *AnalyzerResult) *workspace {
	ws := &workspace{
		repo:       repo,
		files:      map[string]*fileInfo{},
		objects:    map[types.Object]*symbolInfo{},
		objectKeys: map[string]*symbolInfo{},
	}
	for _, pkg := range pkgs {
		for index, astFile := range pkg.Syntax {
			absPath := compiledGoFilePath(pkg, astFile, index)
			relPath, ok := repoRelPath(repo, absPath)
			if !ok || shouldSkipWorkspacePath(relPath) {
				continue
			}
			if _, exists := ws.files[relPath]; exists {
				continue
			}
			source, err := os.ReadFile(absPath)
			if err != nil {
				result.Warnings = append(result.Warnings, fmt.Sprintf("Unable to read Go file %s: %s", relPath, err))
				continue
			}
			info := &fileInfo{
				path:        relPath,
				absPath:     absPath,
				source:      source,
				fset:        pkg.Fset,
				file:        astFile,
				pkg:         pkg,
				packageName: astFile.Name.Name,
			}
			info.imports = collectImports(astFile)
			collectSymbols(ws, info)
			ws.files[relPath] = info
		}
	}
	sort.Slice(ws.symbols, func(i, j int) bool {
		if ws.symbols[i].file.path == ws.symbols[j].file.path {
			return ws.symbols[i].analysis.StartLine < ws.symbols[j].analysis.StartLine
		}
		return ws.symbols[i].file.path < ws.symbols[j].file.path
	})
	return ws
}

func compiledGoFilePath(pkg *packages.Package, astFile *ast.File, index int) string {
	if index < len(pkg.CompiledGoFiles) {
		return pkg.CompiledGoFiles[index]
	}
	if pkg.Fset != nil {
		return pkg.Fset.Position(astFile.Package).Filename
	}
	return ""
}

func repoRelPath(repo string, absPath string) (string, bool) {
	if absPath == "" {
		return "", false
	}
	abs, err := filepath.Abs(absPath)
	if err != nil {
		return "", false
	}
	rel, err := filepath.Rel(repo, abs)
	if err != nil || rel == "." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || rel == ".." {
		return "", false
	}
	return normalizeRelPath(rel), true
}

func shouldSkipWorkspacePath(path string) bool {
	parts := strings.Split(path, "/")
	for _, part := range parts {
		switch part {
		case ".git", "vendor", "node_modules", ".venv", "dist", "build", "coverage":
			return true
		}
	}
	return false
}
