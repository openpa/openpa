package stream

import (
	"encoding/json"
	"fmt"
	"os"

	"openpa.local/cli/internal/client"
)

// RawRenderer prints only the assistant text from `text` events. Errors are
// surfaced as messages on stderr but do not abort the loop until `complete`.
type RawRenderer struct{}

func (RawRenderer) Render(ev client.Event) bool {
	switch ev.Type {
	case "text":
		var p struct {
			Data struct {
				Text string `json:"text"`
			} `json:"data"`
		}
		if err := json.Unmarshal(ev.Raw, &p); err == nil && p.Data.Text != "" {
			fmt.Print(p.Data.Text)
		}
	case "complete", "error":
		// Newline so a piped consumer always ends with one.
		fmt.Println()
		return false
	}
	return true
}

func (RawRenderer) Stop(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
}

// JSONRenderer dumps each SSE event verbatim as a JSONL line. Stops on
// `complete` or `error` so the consumer doesn't hang.
type JSONRenderer struct{}

func (JSONRenderer) Render(ev client.Event) bool {
	os.Stdout.Write(ev.Raw)
	os.Stdout.Write([]byte{'\n'})
	return ev.Type != "complete" && ev.Type != "error"
}

func (JSONRenderer) Stop(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
}
