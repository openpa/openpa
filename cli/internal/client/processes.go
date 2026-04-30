package client

import (
	"context"
	"net/url"
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
