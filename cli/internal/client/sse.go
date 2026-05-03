package client

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// Event is a single SSE frame's decoded JSON payload from the OpenPA server.
// All known endpoints emit `data: {"type": "...", ...}\n\n` frames; we expose
// the type and keep the rest as raw JSON for the caller to parse based on
// context.
type Event struct {
	Type string
	Raw  json.RawMessage
}

// Stream opens an SSE stream against the given path and returns a channel of
// frames plus an error channel. The caller must drain both until the events
// channel is closed (which happens when the context is cancelled or the
// connection ends).
//
// Heartbeat comment lines (`: keepalive`) are silently dropped. Frames whose
// `data:` line is not valid JSON are also dropped — the OpenPA server never
// emits those, so this is defensive.
func (c *Client) Stream(ctx context.Context, path string) (<-chan Event, <-chan error) {
	events := make(chan Event, 32)
	errs := make(chan error, 1)

	go func() {
		defer close(events)
		defer close(errs)

		req, err := c.NewRequest(ctx, http.MethodGet, path, nil)
		if err != nil {
			errs <- err
			return
		}
		req.Header.Set("Accept", "text/event-stream")
		req.Header.Set("Cache-Control", "no-cache")

		resp, err := c.streamHTTP.Do(req)
		if err != nil {
			errs <- err
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode >= 400 {
			raw, _ := io.ReadAll(resp.Body)
			apiErr := &APIError{Status: resp.StatusCode, Raw: raw}
			var e struct {
				Error string `json:"error"`
			}
			if json.Unmarshal(raw, &e) == nil && e.Error != "" {
				apiErr.Body = e.Error
			} else {
				apiErr.Body = strings.TrimSpace(string(raw))
			}
			errs <- apiErr
			return
		}

		scanner := bufio.NewScanner(resp.Body)
		// Allow large frames — agent thinking blocks can be hefty.
		scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
		scanner.Split(splitSSEFrames)

		for scanner.Scan() {
			frame := scanner.Bytes()
			ev, ok := parseSSEFrame(frame)
			if !ok {
				continue
			}
			select {
			case events <- ev:
			case <-ctx.Done():
				return
			}
		}
		if err := scanner.Err(); err != nil && ctx.Err() == nil {
			errs <- fmt.Errorf("sse read: %w", err)
		}
	}()

	return events, errs
}

// splitSSEFrames is a bufio.SplitFunc that splits on `\n\n` (or `\r\n\r\n`).
func splitSSEFrames(data []byte, atEOF bool) (advance int, token []byte, err error) {
	if atEOF && len(data) == 0 {
		return 0, nil, nil
	}
	if i := bytes.Index(data, []byte("\r\n\r\n")); i >= 0 {
		return i + 4, data[:i], nil
	}
	if i := bytes.Index(data, []byte("\n\n")); i >= 0 {
		return i + 2, data[:i], nil
	}
	if atEOF {
		return len(data), data, nil
	}
	return 0, nil, nil
}

// parseSSEFrame extracts the JSON payload from a single frame. Returns
// (event, true) on success; (zero, false) for comments, blank frames, or
// non-JSON data lines.
func parseSSEFrame(frame []byte) (Event, bool) {
	var dataLine string
	for _, raw := range bytes.Split(frame, []byte{'\n'}) {
		line := strings.TrimRight(string(raw), "\r")
		if line == "" || strings.HasPrefix(line, ":") {
			continue
		}
		if strings.HasPrefix(line, "data:") {
			payload := strings.TrimPrefix(line, "data:")
			payload = strings.TrimPrefix(payload, " ")
			if dataLine == "" {
				dataLine = payload
			} else {
				dataLine += "\n" + payload
			}
		}
		// Other SSE field types (event:, id:, retry:) are unused by OpenPA
		// and intentionally ignored.
	}
	if dataLine == "" {
		return Event{}, false
	}
	var head struct {
		Type string `json:"type"`
	}
	raw := json.RawMessage(dataLine)
	if err := json.Unmarshal(raw, &head); err != nil {
		return Event{}, false
	}
	return Event{Type: head.Type, Raw: raw}, true
}
