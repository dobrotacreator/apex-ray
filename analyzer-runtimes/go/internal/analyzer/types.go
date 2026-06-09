package analyzer

type Args struct {
	Repo                 string
	Changed              []string
	ChangedRanges        map[string][]LineRange
	DeletedLines         map[string][]DeletedLine
	AnalysisTimeBudgetMS int
}

type LineRange struct {
	Start int
	End   int
}

type DeletedLine struct {
	Line int
	Text string
}

type AnalyzerResult struct {
	Language      string          `json:"language"`
	ProjectRoot   string          `json:"projectRoot"`
	TSConfigPath  *string         `json:"tsconfigPath"`
	Files         []FileAnalysis  `json:"files"`
	Warnings      []string        `json:"warnings"`
	IndexCache    *IndexCacheStat `json:"indexCache"`
	Partial       bool            `json:"partial"`
	FailedFiles   []string        `json:"failedFiles"`
	ShardFailures []ShardFailure  `json:"shardFailures"`
}

type FileAnalysis struct {
	Path           string           `json:"path"`
	TSConfigPath   *string          `json:"tsconfigPath"`
	Symbols        []AnalyzerSymbol `json:"symbols"`
	Imports        []string         `json:"imports"`
	Exports        []string         `json:"exports"`
	RelatedTests   []string         `json:"relatedTests"`
	ChangedSymbols []AnalyzerSymbol `json:"changedSymbols"`
}

type AnalyzerSymbol struct {
	Name       string              `json:"name"`
	Kind       string              `json:"kind"`
	StartLine  int                 `json:"startLine"`
	EndLine    int                 `json:"endLine"`
	Exported   bool                `json:"exported"`
	Signature  string              `json:"signature"`
	References []AnalyzerReference `json:"references"`
	Callees    []AnalyzerReference `json:"callees"`
	Contracts  []AnalyzerReference `json:"contracts"`
	Metadata   []AnalyzerReference `json:"metadata"`
}

type AnalyzerReference struct {
	File    string `json:"file"`
	Line    int    `json:"line"`
	EndLine int    `json:"endLine,omitempty"`
	Text    string `json:"text"`
	Kind    string `json:"kind"`
}

type IndexCacheStat struct {
	Path    string `json:"path"`
	Files   int    `json:"files"`
	Hits    int    `json:"hits"`
	Misses  int    `json:"misses"`
	Written bool   `json:"written"`
}

type ShardFailure struct {
	Index  int      `json:"index"`
	Total  int      `json:"total"`
	Files  []string `json:"files"`
	Reason string   `json:"reason"`
	Status string   `json:"status"`
}
