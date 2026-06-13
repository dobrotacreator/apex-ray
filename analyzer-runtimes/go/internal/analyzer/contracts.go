package analyzer

import (
	"go/ast"
	"go/types"
)

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
