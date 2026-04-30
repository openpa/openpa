// Package output centralizes how command results are written to stdout.
// Lists render as ASCII tables on a TTY and as tab-separated values when
// piped, and any command can be flipped to JSON via --json or OPA_OUTPUT=json.
package output

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

	"github.com/jedib0t/go-pretty/v6/table"
	"github.com/jedib0t/go-pretty/v6/text"
	"golang.org/x/term"

	"openpa.local/cli/internal/config"
)

// Mode is the user's selected output mode (table or json), already resolved
// from --json flag + OPA_OUTPUT env.
type Mode struct {
	JSON    bool
	NoColor bool
}

// FromConfig translates a Config into a Mode. If `jsonFlag` is true it forces
// JSON regardless of the env-derived default.
func FromConfig(cfg *config.Config, jsonFlag bool) Mode {
	return Mode{
		JSON:    jsonFlag || cfg.Output == config.OutputJSON,
		NoColor: cfg.NoColor,
	}
}

// PrintJSON writes v as pretty-printed JSON.
func PrintJSON(v any) error {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

// PrintJSONLine writes v as a single compact JSON line (for streamed events).
func PrintJSONLine(v any) error {
	return json.NewEncoder(os.Stdout).Encode(v)
}

// PrintRaw writes raw bytes (e.g. a JSON payload received verbatim from the
// server) followed by a newline.
func PrintRaw(b []byte) {
	os.Stdout.Write(b)
	if len(b) == 0 || b[len(b)-1] != '\n' {
		fmt.Println()
	}
}

// Table renders a header row + data rows. On a TTY it draws an ASCII table;
// when stdout is piped, it emits tab-separated values without decoration so
// the output is grep/awk-friendly.
type Table struct {
	mode    Mode
	headers []string
	rows    [][]string
}

func NewTable(mode Mode, headers ...string) *Table {
	return &Table{mode: mode, headers: headers}
}

func (t *Table) AddRow(cells ...any) {
	row := make([]string, len(cells))
	for i, c := range cells {
		row[i] = fmt.Sprintf("%v", c)
	}
	t.rows = append(t.rows, row)
}

func (t *Table) Render() {
	if isTTY(os.Stdout) && !t.mode.NoColor {
		tw := table.NewWriter()
		tw.SetOutputMirror(os.Stdout)
		tw.SetStyle(table.StyleLight)
		header := make(table.Row, len(t.headers))
		for i, h := range t.headers {
			header[i] = h
		}
		tw.AppendHeader(header)
		for _, r := range t.rows {
			row := make(table.Row, len(r))
			for i, c := range r {
				row[i] = c
			}
			tw.AppendRow(row)
		}
		tw.Style().Format.Header = text.FormatDefault
		tw.Render()
		return
	}
	// Pipe-friendly TSV (no header underline, no borders).
	fmt.Println(strings.Join(t.headers, "\t"))
	for _, r := range t.rows {
		fmt.Println(strings.Join(r, "\t"))
	}
}

// PrintKV writes a key/value block, aligned. Keys are rendered in insertion
// order. Use this for single-object views (`opa me`, `opa tools get`).
func PrintKV(items [][2]string) {
	width := 0
	for _, it := range items {
		if len(it[0]) > width {
			width = len(it[0])
		}
	}
	for _, it := range items {
		fmt.Printf("%-*s  %s\n", width, it[0]+":", it[1])
	}
}

// PrintMap writes a map as sorted KV pairs.
func PrintMap(m map[string]any) {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	pairs := make([][2]string, 0, len(keys))
	for _, k := range keys {
		pairs = append(pairs, [2]string{k, fmt.Sprintf("%v", m[k])})
	}
	PrintKV(pairs)
}

func isTTY(f *os.File) bool {
	return term.IsTerminal(int(f.Fd()))
}

// Stderr writes a formatted message to stderr with a trailing newline.
func Stderr(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
}

// Println writes to stdout with a trailing newline.
func Println(s string) { fmt.Fprintln(os.Stdout, s) }

// Stdout returns os.Stdout for callers that need direct access (streaming).
func Stdout() io.Writer { return os.Stdout }
