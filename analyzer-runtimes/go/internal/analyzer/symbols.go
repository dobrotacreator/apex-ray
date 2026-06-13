package analyzer

import (
	"bytes"
	"go/ast"
	"go/format"
	"go/token"
	"go/types"
	"sort"
	"strconv"
	"strings"
)

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
	for index, line := range deleted {
		name, kind, ok := deletedSymbolName(line.Text)
		if !ok {
			continue
		}
		endLine := deletedSymbolEndLine(deleted, index, line.Line)
		symbols = append(symbols, &symbolInfo{
			file: info,
			analysis: AnalyzerSymbol{
				Name:       name,
				Kind:       kind,
				StartLine:  line.Line,
				EndLine:    endLine,
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

func deletedSymbolEndLine(deleted []DeletedLine, startIndex int, startLine int) int {
	endLine := startLine
	for index := startIndex + 1; index < len(deleted); index++ {
		line := deleted[index]
		if line.Line != endLine+1 {
			break
		}
		if _, _, ok := deletedSymbolName(line.Text); ok {
			break
		}
		endLine = line.Line
	}
	return endLine
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
