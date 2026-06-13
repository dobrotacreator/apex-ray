package analyzer

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
