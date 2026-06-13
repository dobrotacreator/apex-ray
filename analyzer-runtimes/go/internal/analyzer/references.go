package analyzer

import (
	"fmt"
	"go/ast"
	"go/types"
	"strings"
)

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
