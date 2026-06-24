// Command print-bambu-3mf extracts printer, process, and filament settings from
// Bambu Studio .3mf files as structured JSON.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/hagmonk/print-bambu-3mf/internal/bambu"
)

// version is set at release time via -ldflags "-X main.version=...".
var version = "dev"

func main() {
	fs := flag.NewFlagSet("print-bambu-3mf", flag.ContinueOnError)
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "Extract Bambu Studio profiles from 3MF files")
		fmt.Fprintln(os.Stderr, "\nUsage: print-bambu-3mf [options] [files...]")
		fs.PrintDefaults()
	}
	noInheritance := fs.Bool("no-inheritance", false, "Do not resolve profile inheritance")
	output := fs.String("output", "", "Output file (default: stdout)")
	fs.StringVar(output, "o", "", "Output file (default: stdout)")
	plate := fs.Int("plate", -1, "Plate number to extract")
	list := fs.Bool("list", false, "List plates and objects in the file")
	object := fs.String("object", "", "Only dump settings for specific object by name or index")
	full := fs.Bool("full", false, "Include gcode settings (excluded by default)")
	selectFlag := fs.String("select", "machine,filament,process", "Comma-separated sections to include (machine,filament,process)")
	friendly := fs.Bool("friendly-names", false, "Replace setting keys with Bambu Studio labels")
	showVersion := fs.Bool("version", false, "Print version and exit")

	// Interleave flags and positionals like argparse (stdlib flag stops at the
	// first non-flag): parse, peel one positional, repeat.
	var positionals []string
	rest := os.Args[1:]
	for {
		if err := fs.Parse(rest); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		if fs.NArg() == 0 {
			break
		}
		positionals = append(positionals, fs.Arg(0))
		rest = fs.Args()[1:]
	}

	if *showVersion {
		fmt.Println(version)
		return
	}

	var platePtr *int
	if isFlagSet(fs, "plate") {
		platePtr = plate
	}

	// Resolve files: explicit args (existing only), else *.3mf in cwd.
	var files []string
	if len(positionals) > 0 {
		for _, f := range positionals {
			if fileExists(f) {
				files = append(files, f)
			}
		}
	} else {
		matches, _ := filepath.Glob("*.3mf")
		sort.Strings(matches)
		files = matches
	}

	if len(files) == 0 {
		fmt.Fprintln(os.Stderr, "No .3mf files found")
		return
	}

	ext := bambu.NewExtractor()

	// --list mode
	if *list {
		for _, file := range files {
			printList(ext, file)
		}
		return
	}

	// --object mode
	if *object != "" {
		if len(files) != 1 {
			fmt.Fprintln(os.Stderr, "Error: --object requires exactly one 3MF file")
			os.Exit(1)
		}
		result := ext.GetObjectSettings(files[0], *object, *full, platePtr, *friendly)

		selectTypes := parseSelect(*selectFlag)
		filtered := map[string]any{}
		for _, key := range []string{"machine", "filament", "process"} {
			if selectTypes[key] {
				if v, ok := result[key]; ok {
					filtered[key] = v
				}
			}
		}
		if len(filtered) > 0 {
			result = filtered
		}

		if errVal, ok := result["error"]; ok {
			fmt.Fprintf(os.Stderr, "Error: %v\n", errVal)
			os.Exit(1)
		}

		emit(result, *output)
		return
	}

	// Structured mode (all plates or a specific plate)
	allProfiles := map[string]any{}
	for _, file := range files {
		profiles := ext.ExtractStructured(file, !*noInheritance, platePtr, *full, *friendly)
		if len(profiles) == 0 {
			continue
		}
		selectTypes := parseSelect(*selectFlag)
		toEmit := map[string]any{}
		if v, ok := profiles["plates"]; ok {
			toEmit["plates"] = v
		}
		if v, ok := profiles["machine"]; ok && (selectTypes["machine"] || len(selectTypes) == 0) {
			toEmit["machine"] = v
		}
		if v, ok := profiles["process"]; ok && (selectTypes["process"] || len(selectTypes) == 0) {
			toEmit["process"] = v
		}
		if v, ok := profiles["filaments"]; ok && (selectTypes["filament"] || len(selectTypes) == 0) {
			toEmit["filaments"] = v
		}
		allProfiles[filepath.Base(file)] = toEmit
	}

	emit(allProfiles, *output)
}

func parseSelect(s string) map[string]bool {
	out := map[string]bool{}
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part != "" {
			out[part] = true
		}
	}
	return out
}

// encodeJSON matches Python's json.dumps(indent=2, sort_keys=True,
// ensure_ascii=False): sorted keys (Go marshals map keys sorted), two-space
// indent, and no HTML escaping. The encoder appends a trailing newline.
func encodeJSON(v any) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
	return buf.Bytes()
}

// emit writes JSON to a file (no trailing newline, "Saved to" to stderr) or to
// stdout (with trailing newline), mirroring the Python CLI.
func emit(v any, output string) {
	data := encodeJSON(v) // ends with "\n"
	if output != "" {
		trimmed := bytes.TrimRight(data, "\n")
		if err := os.WriteFile(output, trimmed, 0o644); err != nil {
			fmt.Fprintf(os.Stderr, "Error writing %s: %v\n", output, err)
			os.Exit(1)
		}
		fmt.Fprintf(os.Stderr, "Saved to %s\n", output)
		return
	}
	os.Stdout.Write(data)
}

func printList(ext *bambu.Extractor, file string) {
	info := ext.ListObjects(file)

	fmt.Printf("\n%s:\n", filepath.Base(file))
	fmt.Println(strings.Repeat("-", 40))

	if len(info.Plates) > 0 {
		fmt.Println("\nPlates:")
		ids := make([]int, 0, len(info.Plates))
		for id := range info.Plates {
			ids = append(ids, id)
		}
		sort.Ints(ids)
		for _, id := range ids {
			objects := info.Plates[id]
			if len(objects) > 0 {
				fmt.Printf("  Plate %d:\n", id)
				for _, obj := range objects {
					fmt.Printf("    - %s\n", obj)
				}
			} else {
				fmt.Printf("  Plate %d: (empty)\n", id)
			}
		}
	}

	if len(info.Objects) > 0 {
		fmt.Println("\nObjects:")
		for _, obj := range info.Objects {
			fmt.Printf("  [%d] %s\n", obj.Index, obj.Name)
			fmt.Printf("      ID: %s, Extruder: %d\n", obj.ID, obj.Extruder)
		}
	}
}

func isFlagSet(fs *flag.FlagSet, name string) bool {
	found := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == name {
			found = true
		}
	})
	return found
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}
