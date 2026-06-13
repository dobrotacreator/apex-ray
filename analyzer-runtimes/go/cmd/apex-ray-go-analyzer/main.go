package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/dobrotacreator/apex-ray/analyzer-runtimes/go/internal/analyzer"
)

func main() {
	args, err := analyzer.ParseArgs(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}

	result := analyzer.Analyze(args)
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
