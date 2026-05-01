package client

import (
	"context"
	"net/url"
)

// ListTools returns all tools visible to the authenticated profile. Each entry
// is a loose map — the schema is rich and varies by tool type, so callers
// extract just the fields they care about (tool_id, name, tool_type,
// configured, …). The server resolves the profile from the JWT.
func (c *Client) ListTools(ctx context.Context, filterType string) ([]map[string]any, error) {
	var resp struct {
		Tools []map[string]any `json:"tools"`
	}
	if err := c.GetJSON(ctx, "/api/tools", &resp); err != nil {
		return nil, err
	}
	if filterType == "" {
		return resp.Tools, nil
	}
	out := resp.Tools[:0]
	for _, t := range resp.Tools {
		if v, _ := t["tool_type"].(string); v == filterType {
			out = append(out, t)
		}
	}
	return out, nil
}

// GetTool returns a single tool's full configuration + schema.
func (c *Client) GetTool(ctx context.Context, toolID string) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/tools/"+url.PathEscape(toolID), &out); err != nil {
		return nil, err
	}
	return out, nil
}

// SetToolVariables writes Tool Variables (env-style key/value pairs).
func (c *Client) SetToolVariables(ctx context.Context, toolID string, vars map[string]string) error {
	body := map[string]any{"variables": vars}
	return c.PutJSON(ctx, "/api/tools/"+url.PathEscape(toolID)+"/variables", body, nil)
}

// SetToolArguments writes Tool Arguments (JSON-Schema-shaped values).
func (c *Client) SetToolArguments(ctx context.Context, toolID string, args map[string]any) error {
	body := map[string]any{"arguments": args}
	return c.PutJSON(ctx, "/api/tools/"+url.PathEscape(toolID)+"/arguments", body, nil)
}

// SetToolEnabled toggles enable/disable for an A2A or MCP tool.
func (c *Client) SetToolEnabled(ctx context.Context, toolID string, enabled bool) error {
	return c.PutJSON(ctx, "/api/tools/"+url.PathEscape(toolID)+"/enabled",
		map[string]bool{"enabled": enabled}, nil)
}

// SetToolLLMParams updates LLM Parameters. Only keys present in `params` are
// changed — the server treats the body as a partial update.
func (c *Client) SetToolLLMParams(ctx context.Context, toolID string, params map[string]any) error {
	return c.PutJSON(ctx, "/api/tools/"+url.PathEscape(toolID)+"/llm",
		map[string]any{"llm": params}, nil)
}

// RegisterLongRunningApp spawns a skill's declared long_running_app and
// persists it as autostart. Returns {process_id, autostart_id, command,
// working_dir} on success. force=true bypasses the duplicate check.
func (c *Client) RegisterLongRunningApp(ctx context.Context, toolID string, force bool) (map[string]any, error) {
	body := map[string]any{}
	if force {
		body["force"] = true
	}
	var out map[string]any
	if err := c.PostJSON(ctx, "/api/tools/"+url.PathEscape(toolID)+"/long-running-app/register", body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ResetToolLLMParams deletes the listed LLM-parameter override keys, reverting
// them to code defaults.
func (c *Client) ResetToolLLMParams(ctx context.Context, toolID string, keys []string) error {
	body := map[string]any{"keys": keys}
	// DELETE with body — net/http handles this fine; build the request manually
	// because (*Client).Delete doesn't accept a body.
	return c.bodyJSON(ctx, "DELETE", "/api/tools/"+url.PathEscape(toolID)+"/llm", body, nil)
}
