package analyzer

import (
	"go/ast"
	"go/token"
	"strings"
)

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
