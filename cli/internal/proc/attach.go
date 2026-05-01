// Package proc implements the interactive PTY-attach loop for `opa proc
// attach`. It opens a WebSocket to the server's /api/processes/{pid}/ws
// endpoint, puts the local terminal into raw mode, and pumps:
//
//   - server stdout/stderr → local stdout
//   - local stdin (raw) → server stdin
//   - local terminal resize (polled) → server resize
//
// The loop exits cleanly on a configured detach key (default Ctrl-\) or when
// the server closes the WebSocket (process exit).
package proc

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"time"

	"golang.org/x/term"

	"openpa.local/cli/internal/wsclient"
)

// AttachConfig drives Attach.
type AttachConfig struct {
	URL       string // ws:// or wss:// process WebSocket URL
	Token     string // bearer token for the Sec-WebSocket-Protocol header
	Resize    bool   // forward terminal resize events
	DetachKey byte   // single byte that, when read from local stdin, exits without killing the remote process
}

// Attach blocks until the WebSocket closes, the user hits the detach key, or
// ctx is cancelled. It returns nil on a clean detach / remote exit.
func Attach(ctx context.Context, cfg AttachConfig) error {
	conn, err := wsclient.Dial(ctx, cfg.URL, cfg.Token)
	if err != nil {
		return err
	}
	defer conn.Close()

	stdoutFD := int(os.Stdout.Fd())
	stdinFD := int(os.Stdin.Fd())

	var prevState *term.State
	if term.IsTerminal(stdinFD) {
		st, err := term.MakeRaw(stdinFD)
		if err != nil {
			return fmt.Errorf("enter raw mode: %w", err)
		}
		prevState = st
		defer term.Restore(stdinFD, prevState)
	}

	// Send initial resize so the remote PTY matches the local terminal.
	if cfg.Resize && term.IsTerminal(stdoutFD) {
		if cols, rows, err := term.GetSize(stdoutFD); err == nil {
			_ = sendResize(conn, cols, rows)
		}
	}

	// Use a derived ctx so we can cancel siblings on the first error.
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	errc := make(chan error, 3)

	go func() { errc <- pumpServerToStdout(runCtx, conn) }()
	go func() { errc <- pumpStdinToServer(runCtx, conn, cfg.DetachKey) }()
	if cfg.Resize && term.IsTerminal(stdoutFD) {
		go func() { errc <- pollResize(runCtx, conn, stdoutFD) }()
	}

	// First non-nil/non-EOF result wins; cancel triggers the rest to unwind.
	err = <-errc
	cancel()
	if errors.Is(err, io.EOF) || errors.Is(err, context.Canceled) || errors.Is(err, errDetach) {
		return nil
	}
	return err
}

var errDetach = errors.New("user detached")

func pumpServerToStdout(ctx context.Context, conn *wsclient.Conn) error {
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		text, err := conn.ReadText()
		if err != nil {
			return err
		}
		if err := handleServerMessage(text); err != nil {
			return err
		}
	}
}

// handleServerMessage routes one JSON envelope from the server. The shape
// mirrors app/api/processes.py:handle_ws — types we care about are:
//
//   - {"type":"snapshot","chunks":[{"type":"stdout|stderr","data":"..."}, ...]}
//   - {"type":"status",  "data":{...}}
//   - {"type":"stdout",  "data":"..."}
//   - {"type":"stderr",  "data":"..."}
//   - {"type":"overflow"} → server is closing the connection
func handleServerMessage(text string) error {
	var env struct {
		Type   string          `json:"type"`
		Data   json.RawMessage `json:"data"`
		Chunks []struct {
			Type string `json:"type"`
			Data string `json:"data"`
		} `json:"chunks"`
	}
	if err := json.Unmarshal([]byte(text), &env); err != nil {
		// Non-JSON or malformed — write raw so the user still sees output.
		_, _ = os.Stdout.WriteString(text)
		return nil
	}
	switch env.Type {
	case "snapshot":
		for _, c := range env.Chunks {
			_, _ = os.Stdout.WriteString(c.Data)
		}
	case "stdout", "stderr":
		var s string
		if err := json.Unmarshal(env.Data, &s); err == nil {
			_, _ = os.Stdout.WriteString(s)
		}
	case "status":
		// Silently observed — the user already knows they attached. Could
		// be surfaced via stderr if we ever want a status line.
	case "overflow":
		return fmt.Errorf("server closed connection: output buffer overflowed")
	}
	return nil
}

func pumpStdinToServer(ctx context.Context, conn *wsclient.Conn, detach byte) error {
	buf := make([]byte, 4096)
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		n, err := os.Stdin.Read(buf)
		if n > 0 {
			chunk := buf[:n]
			if detach != 0 {
				for _, b := range chunk {
					if b == detach {
						return errDetach
					}
				}
			}
			payload, _ := json.Marshal(map[string]any{
				"type":        "stdin",
				"data":        string(chunk),
				"line_ending": "none",
			})
			if err := conn.WriteText(string(payload)); err != nil {
				return err
			}
		}
		if err != nil {
			return err
		}
	}
}

func pollResize(ctx context.Context, conn *wsclient.Conn, fd int) error {
	prevCols, prevRows, _ := term.GetSize(fd)
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			cols, rows, err := term.GetSize(fd)
			if err != nil {
				continue
			}
			if cols == prevCols && rows == prevRows {
				continue
			}
			prevCols, prevRows = cols, rows
			if err := sendResize(conn, cols, rows); err != nil {
				return err
			}
		}
	}
}

func sendResize(conn *wsclient.Conn, cols, rows int) error {
	payload, _ := json.Marshal(map[string]any{
		"type": "resize",
		"cols": cols,
		"rows": rows,
	})
	return conn.WriteText(string(payload))
}
