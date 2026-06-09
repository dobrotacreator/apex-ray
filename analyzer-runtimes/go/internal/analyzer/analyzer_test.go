package analyzer

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAnalyzeCollectsSemanticGoContext(t *testing.T) {
	repo := t.TempDir()
	writeFile(t, repo, "go.mod", "module example.com/review\n\ngo 1.24\n")
	authSource := "" +
		"package auth\n\n" +
		"import (\n" +
		"    \"context\"\n" +
		"    \"fmt\"\n" +
		")\n\n" +
		"type Repository interface {\n" +
		"    Save(context.Context, string) error\n" +
		"}\n\n" +
		"type Service struct {\n" +
		"    repo Repository\n" +
		"}\n\n" +
		"func (s *Service) Authorize(ctx context.Context, id string) error {\n" +
		"    if err := s.repo.Save(ctx, id); err != nil {\n" +
		"        return fmt.Errorf(\"save auth: %w\", err)\n" +
		"    }\n" +
		"    return nil\n" +
		"}\n"
	writeFile(t, repo, "internal/auth/service.go", authSource)
	writeFile(t, repo, "internal/api/handler.go", ""+
		"package api\n\n"+
		"import (\n"+
		"    \"net/http\"\n\n"+
		"    \"example.com/review/internal/auth\"\n"+
		")\n\n"+
		"func Register(mux *http.ServeMux, svc *auth.Service) {\n"+
		"    mux.HandleFunc(\"/resource\", func(w http.ResponseWriter, r *http.Request) {\n"+
		"        _ = svc.Authorize(r.Context(), \"resource-id\")\n"+
		"    })\n"+
		"}\n")
	writeFile(t, repo, "internal/auth/service_test.go", ""+
		"package auth\n\n"+
		"import (\n"+
		"    \"context\"\n"+
		"    \"testing\"\n"+
		")\n\n"+
		"type fakeRepo struct{}\n\n"+
		"func (fakeRepo) Save(context.Context, string) error { return nil }\n\n"+
		"func TestAuthorize(t *testing.T) {\n"+
		"    service := &Service{repo: fakeRepo{}}\n"+
		"    if err := service.Authorize(context.Background(), \"id\"); err != nil {\n"+
		"        t.Fatal(err)\n"+
		"    }\n"+
		"}\n")

	result := Analyze(Args{
		Repo:    repo,
		Changed: []string{"internal/auth/service.go"},
		ChangedRanges: map[string][]LineRange{
			"internal/auth/service.go": {{Start: 17, End: 17}},
		},
		DeletedLines:         map[string][]DeletedLine{},
		AnalysisTimeBudgetMS: 30000,
	})

	if len(result.Files) != 1 {
		t.Fatalf("expected one file, got %d: %#v", len(result.Files), result.Warnings)
	}
	file := result.Files[0]
	if len(file.ChangedSymbols) != 1 || file.ChangedSymbols[0].Name != "Service.Authorize" {
		t.Fatalf("unexpected changed symbols: %#v", file.ChangedSymbols)
	}
	symbol := file.ChangedSymbols[0]
	if !hasReference(symbol.References, "internal/api/handler.go", "call") {
		t.Fatalf("missing handler reference: %#v", symbol.References)
	}
	if !hasReference(symbol.References, "internal/auth/service_test.go", "call") {
		t.Fatalf("missing test reference: %#v", symbol.References)
	}
	if !hasText(symbol.Callees, "Save(context.Context, string) error") {
		t.Fatalf("missing interface method callee: %#v", symbol.Callees)
	}
	if !hasMetadata(symbol.Metadata, "context boundary: context.Context") {
		t.Fatalf("missing context metadata: %#v", symbol.Metadata)
	}
	if len(file.RelatedTests) != 1 || file.RelatedTests[0] != "internal/auth/service_test.go" {
		t.Fatalf("unexpected related tests: %#v", file.RelatedTests)
	}
}

func TestAnalyzeCollectsDeletedOnlyGoFileFromDiffLines(t *testing.T) {
	repo := t.TempDir()
	writeFile(t, repo, "go.mod", "module example.com/review\n\ngo 1.24\n")

	result := Analyze(Args{
		Repo:          repo,
		Changed:       []string{},
		ChangedRanges: map[string][]LineRange{},
		DeletedLines: map[string][]DeletedLine{
			"./internal/auth/removed.go": {
				{Line: 1, Text: "func Removed() error {"},
				{Line: 2, Text: "    return nil"},
			},
		},
		AnalysisTimeBudgetMS: 30000,
	})

	if len(result.Files) != 1 {
		t.Fatalf("expected one deleted-only file, got %d: %#v", len(result.Files), result.Warnings)
	}
	file := result.Files[0]
	if file.Path != "internal/auth/removed.go" {
		t.Fatalf("unexpected file path: %s", file.Path)
	}
	if len(file.ChangedSymbols) != 1 {
		t.Fatalf("unexpected changed symbols: %#v", file.ChangedSymbols)
	}
	symbol := file.ChangedSymbols[0]
	if symbol.Name != "Removed" || symbol.Signature != "removed Go function: func Removed() error {" {
		t.Fatalf("unexpected deleted symbol: %#v", symbol)
	}
	if symbol.StartLine != 1 || symbol.EndLine != 2 {
		t.Fatalf("unexpected deleted symbol range: %#v", symbol)
	}
	if result.Partial || len(result.FailedFiles) != 0 {
		t.Fatalf("deleted-only diff analysis should not be partial: %#v", result)
	}
}

func writeFile(t *testing.T, root string, rel string, content string) {
	t.Helper()
	path := filepath.Join(root, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func hasReference(refs []AnalyzerReference, file string, kind string) bool {
	for _, ref := range refs {
		if ref.File == file && ref.Kind == kind {
			return true
		}
	}
	return false
}

func hasMetadata(refs []AnalyzerReference, text string) bool {
	return hasText(refs, text)
}

func hasText(refs []AnalyzerReference, text string) bool {
	for _, ref := range refs {
		if ref.Text == text {
			return true
		}
	}
	return false
}
