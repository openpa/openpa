package client

import (
	"context"
	"net/url"
	"strings"
)

// ListProcesses returns currently running processes for the active profile.
func (c *Client) ListProcesses(ctx context.Context) ([]map[string]any, error) {
	var resp struct {
		Processes []map[string]any `json:"processes"`
	}
	if err := c.GetJSON(ctx, "/api/processes", &resp); err != nil {
		return nil, err
	}
	return resp.Processes, nil
}

// GetProcess returns details for a single process.
func (c *Client) GetProcess(ctx context.Context, pid string) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/processes/"+url.PathEscape(pid), &out); err != nil {
		return nil, err
	}
	return out, nil
}

// StopProcess terminates a process.
func (c *Client) StopProcess(ctx context.Context, pid string) error {
	return c.PostJSON(ctx, "/api/processes/"+url.PathEscape(pid)+"/stop", nil, nil)
}

// ProcessesStreamPath returns the SSE stream path for live process updates.
func (c *Client) ProcessesStreamPath() string { return "/api/processes/stream" }

// SendProcessStdin writes input to a running process. Either input_text (a
// raw string written verbatim, with the optional line_ending appended) or
// keys (a list of named keys like "Enter", "Up") may be supplied. The caller
// chooses which by populating only the relevant fields in body.
func (c *Client) SendProcessStdin(ctx context.Context, pid string, body map[string]any) (map[string]any, error) {
	var out map[string]any
	if err := c.PostJSON(ctx, "/api/processes/"+url.PathEscape(pid)+"/stdin", body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ResizeProcessPTY sends a PTY resize for a process.
func (c *Client) ResizeProcessPTY(ctx context.Context, pid string, cols, rows int) error {
	body := map[string]int{"cols": cols, "rows": rows}
	return c.PostJSON(ctx, "/api/processes/"+url.PathEscape(pid)+"/resize", body, nil)
}

// ProcessWebSocketURL returns the ws:// (or wss://) URL for the live PTY
// stream of a process. Bearer auth is passed via Sec-WebSocket-Protocol
// (the WebSocket handshake doesn't accept Authorization headers from
// browsers, and the server matches that protocol for parity).
func (c *Client) ProcessWebSocketURL(pid string) string {
	base := c.cfg.Server
	if strings.HasPrefix(base, "https://") {
		base = "wss://" + strings.TrimPrefix(base, "https://")
	} else if strings.HasPrefix(base, "http://") {
		base = "ws://" + strings.TrimPrefix(base, "http://")
	}
	return base + "/api/processes/" + url.PathEscape(pid) + "/ws"
}

// ListAutostartProcesses returns the active profile's autostart registrations.
func (c *Client) ListAutostartProcesses(ctx context.Context) ([]map[string]any, error) {
	var resp struct {
		Autostart []map[string]any `json:"autostart"`
	}
	if err := c.GetJSON(ctx, "/api/autostart-processes", &resp); err != nil {
		return nil, err
	}
	return resp.Autostart, nil
}

// CreateAutostartFromProcess registers a live process as autostart so it
// re-launches at server boot. force=true bypasses the duplicate check.
func (c *Client) CreateAutostartFromProcess(ctx context.Context, pid string, force bool) (map[string]any, error) {
	body := map[string]any{"process_id": pid}
	if force {
		body["force"] = true
	}
	var out map[string]any
	if err := c.PostJSON(ctx, "/api/autostart-processes", body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// DeleteAutostartProcess removes an autostart registration.
func (c *Client) DeleteAutostartProcess(ctx context.Context, autostartID string) error {
	return c.Delete(ctx, "/api/autostart-processes/"+url.PathEscape(autostartID), nil)
}

// RunAutostartProcess immediately spawns the command from an autostart row.
// Returns {process_id} on success.
func (c *Client) RunAutostartProcess(ctx context.Context, autostartID string) (map[string]any, error) {
	var out map[string]any
	if err := c.PostJSON(ctx, "/api/autostart-processes/"+url.PathEscape(autostartID)+"/run", nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}
