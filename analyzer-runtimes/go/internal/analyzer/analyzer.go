package analyzer

import (
	"bytes"
	"context"
	"fmt"
	"go/ast"
	"go/format"
	"go/parser"
	"go/token"
	"go/types"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/tools/go/packages"
)

const (
	referenceLimit   = 24
	calleeLimit      = 24
	contractLimit    = 24
	metadataLimit    = 24
	relatedTestLimit = 4
	defaultBudgetMS  = 95000
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
	deletedOnly := validDeletedOnlyPaths(args, changed)
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
	for _, path := range deletedOnly {
		result.Files = append(result.Files, deletedFileAnalysis(path, args.DeletedLines[path]))
	}
	if len(result.Warnings) > 0 || len(result.FailedFiles) > 0 {
		result.Partial = true
	}
	sort.Slice(result.Files, func(i, j int) bool { return result.Files[i].Path < result.Files[j].Path })
	return result
}

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

func validDeletedOnlyPaths(args Args, changed []string) []string {
	changedSet := map[string]struct{}{}
	for _, path := range changed {
		changedSet[path] = struct{}{}
	}
	seen := map[string]struct{}{}
	var paths []string
	for path, lines := range args.DeletedLines {
		normalized := normalizeRelPath(path)
		if !validGoPath(normalized) || len(lines) == 0 {
			continue
		}
		if _, ok := changedSet[normalized]; ok {
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

func validGoPath(normalized string) bool {
	return normalized != "." &&
		!strings.HasPrefix(normalized, "../") &&
		!filepath.IsAbs(normalized) &&
		filepath.Ext(normalized) == ".go"
}

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

func collectImports(file *ast.File) []string {
	seen := map[string]struct{}{}
	var imports []string
	for _, spec := range file.Imports {
		value, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			value = strings.Trim(spec.Path.Value, `"`)
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		imports = append(imports, value)
	}
	sort.Strings(imports)
	return imports
}

func collectSymbols(ws *workspace, info *fileInfo) {
	exported := map[string]struct{}{}
	for _, decl := range info.file.Decls {
		switch node := decl.(type) {
		case *ast.FuncDecl:
			name := node.Name.Name
			kind := "function"
			if node.Recv != nil && len(node.Recv.List) > 0 {
				name = receiverName(node.Recv.List[0].Type) + "." + node.Name.Name
				kind = "method"
			}
			symbol := newSymbol(info, node, info.pkg.TypesInfo.Defs[node.Name], name, kind, node.Name.IsExported(), funcSignature(info.fset, node))
			addSymbol(ws, info, symbol)
			if symbol.analysis.Exported {
				exported[symbol.analysis.Name] = struct{}{}
			}
		case *ast.GenDecl:
			for _, spec := range node.Specs {
				switch typed := spec.(type) {
				case *ast.TypeSpec:
					kind := "type"
					switch typed.Type.(type) {
					case *ast.InterfaceType:
						kind = "interface"
					case *ast.StructType:
						kind = "struct"
					}
					symbol := newSymbol(info, typed, info.pkg.TypesInfo.Defs[typed.Name], typed.Name.Name, kind, typed.Name.IsExported(), nodeLineText(info, typed.Pos()))
					addSymbol(ws, info, symbol)
					if symbol.analysis.Exported {
						exported[symbol.analysis.Name] = struct{}{}
					}
					if interfaceNode, ok := typed.Type.(*ast.InterfaceType); ok {
						collectInterfaceMethodSymbols(ws, info, typed.Name.Name, interfaceNode)
					}
				case *ast.ValueSpec:
					for _, name := range typed.Names {
						symbol := newSymbol(info, typed, info.pkg.TypesInfo.Defs[name], name.Name, "variable", name.IsExported(), nodeLineText(info, typed.Pos()))
						addSymbol(ws, info, symbol)
						if symbol.analysis.Exported {
							exported[symbol.analysis.Name] = struct{}{}
						}
					}
				}
			}
		}
	}
	for name := range exported {
		info.exports = append(info.exports, name)
	}
	sort.Strings(info.exports)
	sort.Slice(info.symbols, func(i, j int) bool {
		return info.symbols[i].analysis.StartLine < info.symbols[j].analysis.StartLine
	})
}

func collectInterfaceMethodSymbols(ws *workspace, info *fileInfo, interfaceName string, node *ast.InterfaceType) {
	if node.Methods == nil {
		return
	}
	for _, method := range node.Methods.List {
		for _, name := range method.Names {
			symbol := newSymbol(
				info,
				method,
				info.pkg.TypesInfo.Defs[name],
				interfaceName+"."+name.Name,
				"method",
				name.IsExported(),
				nodeLineText(info, method.Pos()),
			)
			addSymbol(ws, info, symbol)
		}
	}
}

func newSymbol(info *fileInfo, node ast.Node, object types.Object, name string, kind string, exported bool, signature string) *symbolInfo {
	start := info.fset.Position(node.Pos()).Line
	end := info.fset.Position(node.End()).Line
	return &symbolInfo{
		file:   info,
		node:   node,
		object: object,
		analysis: AnalyzerSymbol{
			Name:       name,
			Kind:       kind,
			StartLine:  start,
			EndLine:    end,
			Exported:   exported,
			Signature:  strings.TrimSpace(signature),
			References: []AnalyzerReference{},
			Callees:    []AnalyzerReference{},
			Contracts:  []AnalyzerReference{},
			Metadata:   []AnalyzerReference{},
		},
	}
}

func addSymbol(ws *workspace, info *fileInfo, symbol *symbolInfo) {
	info.symbols = append(info.symbols, symbol)
	ws.symbols = append(ws.symbols, symbol)
	if symbol.object != nil {
		ws.objects[symbol.object] = symbol
		if key := objectIdentity(symbol.object); key != "" {
			ws.objectKeys[key] = symbol
		}
		if _, ok := symbol.object.(*types.TypeName); ok {
			ws.typeSymbols = append(ws.typeSymbols, symbol)
		}
	}
}

func receiverName(expr ast.Expr) string {
	switch typed := expr.(type) {
	case *ast.Ident:
		return typed.Name
	case *ast.StarExpr:
		return receiverName(typed.X)
	case *ast.SelectorExpr:
		return typed.Sel.Name
	case *ast.IndexExpr:
		return receiverName(typed.X)
	case *ast.IndexListExpr:
		return receiverName(typed.X)
	default:
		return "receiver"
	}
}

func funcSignature(fset *token.FileSet, node *ast.FuncDecl) string {
	clone := *node
	clone.Body = nil
	var buffer bytes.Buffer
	if err := format.Node(&buffer, fset, &clone); err == nil {
		return strings.ReplaceAll(buffer.String(), "\n", " ")
	}
	return node.Name.Name
}

func nodeLineText(info *fileInfo, pos token.Pos) string {
	position := info.fset.Position(pos)
	if position.Line <= 0 {
		return ""
	}
	lines := strings.Split(string(info.source), "\n")
	if position.Line > len(lines) {
		return ""
	}
	return strings.TrimSpace(lines[position.Line-1])
}

func changedSymbolsForFile(info *fileInfo, ranges []LineRange, deleted []DeletedLine) []*symbolInfo {
	var changed []*symbolInfo
	if len(ranges) > 0 {
		for _, symbol := range info.symbols {
			for _, lineRange := range ranges {
				if rangesOverlap(symbol.analysis.StartLine, symbol.analysis.EndLine, lineRange.Start, lineRange.End) {
					changed = append(changed, symbol)
					break
				}
			}
		}
	}
	changed = append(changed, deletedSymbols(info, deleted)...)
	sort.SliceStable(changed, func(i, j int) bool {
		return changed[i].analysis.StartLine < changed[j].analysis.StartLine
	})
	return changed
}

func rangesOverlap(startA, endA, startB, endB int) bool {
	return startA <= endB && startB <= endA
}

func deletedSymbols(info *fileInfo, deleted []DeletedLine) []*symbolInfo {
	var symbols []*symbolInfo
	for _, line := range deleted {
		name, kind, ok := deletedSymbolName(line.Text)
		if !ok {
			continue
		}
		symbols = append(symbols, &symbolInfo{
			file: info,
			analysis: AnalyzerSymbol{
				Name:       name,
				Kind:       kind,
				StartLine:  line.Line,
				EndLine:    line.Line,
				Exported:   ast.IsExported(name),
				Signature:  "removed Go " + kind + ": " + strings.TrimSpace(line.Text),
				References: []AnalyzerReference{},
				Callees:    []AnalyzerReference{},
				Contracts:  []AnalyzerReference{},
				Metadata:   []AnalyzerReference{},
			},
		})
	}
	return symbols
}

func deletedSymbolName(text string) (string, string, bool) {
	trimmed := strings.TrimSpace(text)
	if strings.HasPrefix(trimmed, "func ") {
		name := strings.TrimSpace(strings.TrimPrefix(trimmed, "func "))
		if strings.HasPrefix(name, "(") {
			afterReceiver := strings.TrimSpace(name[strings.Index(name, ")")+1:])
			name = afterReceiver
		}
		name = nameUntilDelimiter(name)
		if name != "" {
			return name, "function", true
		}
	}
	if strings.HasPrefix(trimmed, "type ") {
		name := nameUntilDelimiter(strings.TrimSpace(strings.TrimPrefix(trimmed, "type ")))
		if name != "" {
			return name, "type", true
		}
	}
	return "", "", false
}

func nameUntilDelimiter(value string) string {
	end := len(value)
	for index, char := range value {
		if char == '(' || char == '[' || char == ' ' || char == '\t' || char == '{' {
			end = index
			break
		}
	}
	return strings.TrimSpace(value[:end])
}

func enrichChangedSymbols(ws *workspace, changedByPath map[string][]*symbolInfo) {
	changedSet := map[*symbolInfo]struct{}{}
	targetObjects := map[types.Object]*symbolInfo{}
	targetKeys := map[string]*symbolInfo{}
	for _, symbols := range changedByPath {
		for _, symbol := range symbols {
			changedSet[symbol] = struct{}{}
			if symbol.object != nil {
				targetObjects[symbol.object] = symbol
				if key := objectIdentity(symbol.object); key != "" {
					targetKeys[key] = symbol
				}
			}
		}
	}
	referenceSeen := map[*symbolInfo]refSet{}
	calleeSeen := map[*symbolInfo]refSet{}
	for symbol := range changedSet {
		referenceSeen[symbol] = refSet{}
		calleeSeen[symbol] = refSet{}
	}
	for _, info := range ws.files {
		if info.pkg == nil || info.pkg.TypesInfo == nil {
			continue
		}
		ast.Inspect(info.file, func(node ast.Node) bool {
			call, ok := node.(*ast.CallExpr)
			if !ok {
				return true
			}
			object := calleeObject(info.pkg.TypesInfo, call.Fun)
			if object == nil {
				return true
			}
			line := info.fset.Position(call.Pos()).Line
			target := targetObjects[object]
			if target == nil {
				target = targetKeys[objectIdentity(object)]
			}
			if target != nil && !insideSymbol(target, info.path, line) {
				addReference(&target.analysis.References, referenceSeen[target], callReference(info, call, "call"), referenceLimit)
			}
			if owner := enclosingChangedSymbol(changedSet, info.path, line); owner != nil {
				callee := ws.objects[object]
				if callee == nil {
					callee = ws.objectKeys[objectIdentity(object)]
				}
				if callee != nil && callee != owner {
					addReference(&owner.analysis.Callees, calleeSeen[owner], definitionReference(callee, "callee"), calleeLimit)
				}
			}
			return true
		})
	}
	for symbol := range changedSet {
		collectContracts(ws, symbol)
		collectMetadata(symbol)
	}
}

func calleeObject(info *types.Info, expr ast.Expr) types.Object {
	switch node := unwrapCallTarget(expr).(type) {
	case *ast.Ident:
		return info.Uses[node]
	case *ast.SelectorExpr:
		if selection := info.Selections[node]; selection != nil {
			return selection.Obj()
		}
		return info.Uses[node.Sel]
	default:
		return nil
	}
}

func objectIdentity(object types.Object) string {
	if object == nil || object.Pkg() == nil {
		return ""
	}
	pkgPath := object.Pkg().Path()
	if index := strings.Index(pkgPath, " ["); index >= 0 {
		pkgPath = pkgPath[:index]
	}
	if fn, ok := object.(*types.Func); ok {
		if signature, ok := fn.Type().(*types.Signature); ok && signature.Recv() != nil {
			return pkgPath + "." + normalizeTypeIdentity(signature.Recv().Type().String()) + "." + object.Name()
		}
	}
	return pkgPath + "." + object.Name()
}

func normalizeTypeIdentity(value string) string {
	value = strings.TrimPrefix(value, "*")
	if index := strings.Index(value, " ["); index >= 0 {
		value = value[:index]
	}
	return value
}

func unwrapCallTarget(expr ast.Expr) ast.Expr {
	switch node := expr.(type) {
	case *ast.ParenExpr:
		return unwrapCallTarget(node.X)
	case *ast.IndexExpr:
		return unwrapCallTarget(node.X)
	case *ast.IndexListExpr:
		return unwrapCallTarget(node.X)
	default:
		return expr
	}
}

func insideSymbol(symbol *symbolInfo, path string, line int) bool {
	return symbol.file.path == path && line >= symbol.analysis.StartLine && line <= symbol.analysis.EndLine
}

func enclosingChangedSymbol(changed map[*symbolInfo]struct{}, path string, line int) *symbolInfo {
	var best *symbolInfo
	for symbol := range changed {
		if !insideSymbol(symbol, path, line) {
			continue
		}
		if best == nil || (symbol.analysis.EndLine-symbol.analysis.StartLine) < (best.analysis.EndLine-best.analysis.StartLine) {
			best = symbol
		}
	}
	return best
}

func callReference(info *fileInfo, call *ast.CallExpr, kind string) AnalyzerReference {
	return AnalyzerReference{
		File:    info.path,
		Line:    info.fset.Position(call.Pos()).Line,
		EndLine: info.fset.Position(call.End()).Line,
		Text:    nodeText(info, call),
		Kind:    kind,
	}
}

func definitionReference(symbol *symbolInfo, kind string) AnalyzerReference {
	return AnalyzerReference{
		File:    symbol.file.path,
		Line:    symbol.analysis.StartLine,
		EndLine: symbol.analysis.EndLine,
		Text:    symbol.analysis.Signature,
		Kind:    kind,
	}
}

func addReference(refs *[]AnalyzerReference, seen refSet, ref AnalyzerReference, limit int) {
	if len(*refs) >= limit {
		return
	}
	key := fmt.Sprintf("%s:%d:%d:%s:%s", ref.File, ref.Line, ref.EndLine, ref.Kind, ref.Text)
	if _, ok := seen[key]; ok {
		return
	}
	seen[key] = struct{}{}
	*refs = append(*refs, ref)
}

func collectContracts(ws *workspace, symbol *symbolInfo) {
	seen := refSet{}
	addObjectContract := func(object types.Object) {
		if object == nil {
			return
		}
		if target := ws.objects[object]; target != nil && target != symbol {
			addReference(&symbol.analysis.Contracts, seen, definitionReference(target, "contract"), contractLimit)
		}
	}
	if symbol.file.pkg != nil && symbol.file.pkg.TypesInfo != nil && symbol.node != nil {
		switch node := symbol.node.(type) {
		case *ast.FuncDecl:
			if node.Recv != nil {
				collectFieldListContracts(symbol.file.pkg.TypesInfo, node.Recv, addObjectContract)
			}
			if node.Type.Params != nil {
				collectFieldListContracts(symbol.file.pkg.TypesInfo, node.Type.Params, addObjectContract)
			}
			if node.Type.Results != nil {
				collectFieldListContracts(symbol.file.pkg.TypesInfo, node.Type.Results, addObjectContract)
			}
		case *ast.TypeSpec:
			collectTypeExprContracts(symbol.file.pkg.TypesInfo, node.Type, addObjectContract)
			collectInterfaceImplementationContracts(ws, symbol, seen)
		case *ast.ValueSpec:
			if node.Type != nil {
				collectTypeExprContracts(symbol.file.pkg.TypesInfo, node.Type, addObjectContract)
			}
		}
	}
}

func collectFieldListContracts(info *types.Info, fields *ast.FieldList, add func(types.Object)) {
	if fields == nil {
		return
	}
	for _, field := range fields.List {
		collectTypeExprContracts(info, field.Type, add)
	}
}

func collectTypeExprContracts(info *types.Info, expr ast.Expr, add func(types.Object)) {
	ast.Inspect(expr, func(node ast.Node) bool {
		switch typed := node.(type) {
		case *ast.Ident:
			add(info.Uses[typed])
		case *ast.SelectorExpr:
			add(info.Uses[typed.Sel])
		}
		return true
	})
}

func collectInterfaceImplementationContracts(ws *workspace, symbol *symbolInfo, seen refSet) {
	typeName, ok := symbol.object.(*types.TypeName)
	if !ok {
		return
	}
	named, ok := typeName.Type().(*types.Named)
	if !ok {
		return
	}
	iface, isInterface := named.Underlying().(*types.Interface)
	for _, candidate := range ws.typeSymbols {
		if candidate == symbol {
			continue
		}
		candidateType, ok := candidate.object.(*types.TypeName)
		if !ok {
			continue
		}
		candidateNamed, ok := candidateType.Type().(*types.Named)
		if !ok {
			continue
		}
		candidateIface, candidateIsInterface := candidateNamed.Underlying().(*types.Interface)
		if isInterface && !candidateIsInterface {
			if types.Implements(candidateNamed, iface) || types.Implements(types.NewPointer(candidateNamed), iface) {
				addReference(&symbol.analysis.Contracts, seen, definitionReference(candidate, "contract"), contractLimit)
			}
			continue
		}
		if !isInterface && candidateIsInterface {
			if types.Implements(named, candidateIface) || types.Implements(types.NewPointer(named), candidateIface) {
				addReference(&symbol.analysis.Contracts, seen, definitionReference(candidate, "contract"), contractLimit)
			}
		}
	}
}

func collectMetadata(symbol *symbolInfo) {
	if symbol.node == nil || symbol.file == nil {
		return
	}
	seen := refSet{}
	if fn, ok := symbol.node.(*ast.FuncDecl); ok && fn.Type != nil {
		addContextMetadata(symbol, fn.Type.Params, seen)
	}
	ast.Inspect(symbol.node, func(node ast.Node) bool {
		if len(symbol.analysis.Metadata) >= metadataLimit {
			return false
		}
		switch typed := node.(type) {
		case *ast.GoStmt:
			addMetadata(symbol, seen, typed, "concurrency boundary: "+nodeText(symbol.file, typed))
		case *ast.DeferStmt:
			addMetadata(symbol, seen, typed, "deferred cleanup: "+nodeText(symbol.file, typed))
		case *ast.SendStmt:
			addMetadata(symbol, seen, typed, "channel send: "+nodeText(symbol.file, typed))
		case *ast.UnaryExpr:
			if typed.Op == token.ARROW {
				addMetadata(symbol, seen, typed, "channel receive: "+nodeText(symbol.file, typed))
			}
		case *ast.CallExpr:
			label := boundaryCallLabel(symbol.file, typed)
			if label != "" {
				addMetadata(symbol, seen, typed, label+": "+nodeText(symbol.file, typed))
			}
		}
		return true
	})
}

func addContextMetadata(symbol *symbolInfo, fields *ast.FieldList, seen refSet) {
	if fields == nil {
		return
	}
	for _, field := range fields.List {
		if strings.Contains(nodeText(symbol.file, field.Type), "context.Context") {
			addMetadata(symbol, seen, field, "context boundary: context.Context")
		}
	}
}

func boundaryCallLabel(info *fileInfo, call *ast.CallExpr) string {
	text := nodeText(info, call)
	lower := strings.ToLower(text)
	object := calleeObject(info.pkg.TypesInfo, call.Fun)
	pkgPath := ""
	if object != nil && object.Pkg() != nil {
		pkgPath = object.Pkg().Path()
	}
	switch {
	case pkgPath == "net/http" || strings.HasPrefix(text, "http."):
		return "network boundary"
	case pkgPath == "os" || pkgPath == "io/fs" || strings.HasPrefix(text, "os."):
		return "filesystem boundary"
	case pkgPath == "os/exec" || strings.Contains(text, "exec.Command"):
		return "process boundary"
	case pkgPath == "database/sql" || strings.Contains(lower, ".begin") || strings.Contains(lower, ".commit") || strings.Contains(lower, ".rollback"):
		return "transaction boundary"
	case strings.Contains(text, ".Lock()") || strings.Contains(text, ".Unlock()") || strings.Contains(text, ".RLock()") || strings.Contains(text, ".RUnlock()"):
		return "mutex boundary"
	case strings.Contains(text, "fmt.Errorf") && strings.Contains(text, "%w"):
		return "error wrapping"
	case strings.Contains(text, "errors.Join"):
		return "error aggregation"
	default:
		return ""
	}
}

func addMetadata(symbol *symbolInfo, seen refSet, node ast.Node, text string) {
	ref := AnalyzerReference{
		File:    symbol.file.path,
		Line:    symbol.file.fset.Position(node.Pos()).Line,
		EndLine: symbol.file.fset.Position(node.End()).Line,
		Text:    text,
		Kind:    "metadata",
	}
	addReference(&symbol.analysis.Metadata, seen, ref, metadataLimit)
}

func nodeText(info *fileInfo, node ast.Node) string {
	if info == nil || node == nil {
		return ""
	}
	start := info.fset.Position(node.Pos()).Offset
	end := info.fset.Position(node.End()).Offset
	if start >= 0 && end >= start && end <= len(info.source) {
		return strings.TrimSpace(string(info.source[start:end]))
	}
	var buffer bytes.Buffer
	if err := format.Node(&buffer, info.fset, node); err == nil {
		return strings.TrimSpace(buffer.String())
	}
	return ""
}

func fileAnalysis(info *fileInfo, changed []*symbolInfo, related []string) FileAnalysis {
	return FileAnalysis{
		Path:           info.path,
		Symbols:        cloneSymbols(info.symbols),
		Imports:        append([]string{}, info.imports...),
		Exports:        append([]string{}, info.exports...),
		RelatedTests:   related,
		ChangedSymbols: cloneSymbols(changed),
	}
}

func deletedFileAnalysis(path string, deleted []DeletedLine) FileAnalysis {
	info := &fileInfo{path: path}
	return fileAnalysis(info, changedSymbolsForFile(info, nil, deleted), nil)
}

func cloneSymbols(symbols []*symbolInfo) []AnalyzerSymbol {
	out := make([]AnalyzerSymbol, 0, len(symbols))
	for _, symbol := range symbols {
		out = append(out, symbol.analysis)
	}
	return out
}

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
	abs := filepath.Join(repo, filepath.FromSlash(rel))
	resolved, err := filepath.Abs(abs)
	if err != nil {
		return "", false
	}
	relBack, err := filepath.Rel(repo, resolved)
	if err != nil || relBack == ".." || strings.HasPrefix(relBack, ".."+string(filepath.Separator)) {
		return "", false
	}
	return resolved, true
}

func collectFallbackSymbols(ws *workspace, info *fileInfo) {
	info.pkg = &packages.Package{TypesInfo: &types.Info{Defs: map[*ast.Ident]types.Object{}}}
	collectSymbols(ws, info)
}
