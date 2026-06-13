package analyzer

import (
	"bytes"
	"go/ast"
	"go/format"
	"strings"
)

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
