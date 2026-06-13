package analyzer

import (
	"errors"
	"path/filepath"
	"strconv"
	"strings"
)

func ParseArgs(argv []string) (Args, error) {
	args := Args{
		ChangedRanges: map[string][]LineRange{},
		DeletedLines:  map[string][]DeletedLine{},
	}
	for index := 0; index < len(argv); index++ {
		arg := argv[index]
		switch arg {
		case "--repo":
			index++
			if index >= len(argv) || argv[index] == "" {
				return args, errors.New("missing --repo value")
			}
			repo, err := filepath.Abs(argv[index])
			if err != nil {
				return args, err
			}
			args.Repo = repo
		case "--changed":
			for index+1 < len(argv) && !strings.HasPrefix(argv[index+1], "--") {
				index++
				args.Changed = append(args.Changed, normalizeRelPath(argv[index]))
			}
		case "--range":
			index++
			if index >= len(argv) {
				return args, errors.New("missing --range value")
			}
			file, lineRange, ok := parseRange(argv[index])
			if ok {
				args.ChangedRanges[file] = append(args.ChangedRanges[file], lineRange)
			}
		case "--deleted-line":
			if index+3 >= len(argv) {
				return args, errors.New("missing --deleted-line values")
			}
			file := normalizeRelPath(argv[index+1])
			line, err := strconv.Atoi(argv[index+2])
			if err == nil && file != "" && line > 0 {
				args.DeletedLines[file] = append(args.DeletedLines[file], DeletedLine{Line: line, Text: argv[index+3]})
			}
			index += 3
		case "--analysis-time-budget-ms":
			index++
			if index >= len(argv) {
				return args, errors.New("missing --analysis-time-budget-ms value")
			}
			value, err := strconv.Atoi(argv[index])
			if err == nil && value > 0 {
				args.AnalysisTimeBudgetMS = value
			}
		default:
			return args, errors.New("unknown argument: " + arg)
		}
	}
	if args.Repo == "" {
		return args, errors.New("missing --repo")
	}
	return args, nil
}

func parseRange(value string) (string, LineRange, bool) {
	colon := strings.LastIndex(value, ":")
	dash := strings.LastIndex(value, "-")
	if colon <= 0 || dash <= colon+1 || dash == len(value)-1 {
		return "", LineRange{}, false
	}
	start, startErr := strconv.Atoi(value[colon+1 : dash])
	end, endErr := strconv.Atoi(value[dash+1:])
	if startErr != nil || endErr != nil || start <= 0 || end < start {
		return "", LineRange{}, false
	}
	return normalizeRelPath(value[:colon]), LineRange{Start: start, End: end}, true
}

func normalizeRelPath(value string) string {
	return filepath.ToSlash(filepath.Clean(strings.ReplaceAll(value, "\\", "/")))
}
