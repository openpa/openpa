package cmd

import (
	"encoding/json"
	"os"

	"golang.org/x/term"
)

func isTTYStdout() bool {
	return term.IsTerminal(int(os.Stdout.Fd()))
}

func jsonUnmarshalString(body string, out any) error {
	return json.Unmarshal([]byte(body), out)
}
