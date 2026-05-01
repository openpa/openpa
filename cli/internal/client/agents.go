package client

import (
	"context"
	"net/url"
)

// ListAgents returns the A2A and MCP tools registered in the registry,
// each annotated with the active profile's enabled state and OAuth status.
func (c *Client) ListAgents(ctx context.Context) ([]map[string]any, error) {
	var resp struct {
		Agents []map[string]any `json:"agents"`
	}
	if err := c.GetJSON(ctx, "/api/agents", &resp); err != nil {
		return nil, err
	}
	return resp.Agents, nil
}

// AddAgent registers a new A2A or MCP server. body must include "type"
// ("a2a" | "mcp") and either "url" or (for mcp) "json_config".
func (c *Client) AddAgent(ctx context.Context, body map[string]any) (map[string]any, error) {
	var resp struct {
		Success bool           `json:"success"`
		Agent   map[string]any `json:"agent"`
	}
	if err := c.PostJSON(ctx, "/api/agents", body, &resp); err != nil {
		return nil, err
	}
	return resp.Agent, nil
}

// RemoveAgent unregisters an A2A or MCP tool by id.
func (c *Client) RemoveAgent(ctx context.Context, toolID string) error {
	return c.Delete(ctx, "/api/agents/"+url.PathEscape(toolID), nil)
}

// SetAgentEnabled toggles per-profile visibility for an A2A/MCP tool.
func (c *Client) SetAgentEnabled(ctx context.Context, toolID string, enabled bool) error {
	return c.PutJSON(ctx, "/api/agents/"+url.PathEscape(toolID)+"/enabled",
		map[string]bool{"enabled": enabled}, nil)
}

// ReconnectAgent retries a stub tool's connection.
func (c *Client) ReconnectAgent(ctx context.Context, toolID string) error {
	return c.PostJSON(ctx, "/api/agents/"+url.PathEscape(toolID)+"/reconnect", nil, nil)
}

// GetAgentAuthURL fetches the OAuth authorize URL for a tool. returnURL is
// optional and is stashed server-side so the user is redirected back after
// the OAuth roundtrip.
func (c *Client) GetAgentAuthURL(ctx context.Context, toolID, returnURL string) (string, error) {
	path := "/api/agents/" + url.PathEscape(toolID) + "/auth-url"
	if returnURL != "" {
		path += "?return_url=" + url.QueryEscape(returnURL)
	}
	var resp struct {
		AuthURL string `json:"auth_url"`
	}
	if err := c.GetJSON(ctx, path, &resp); err != nil {
		return "", err
	}
	return resp.AuthURL, nil
}

// UnlinkAgent drops the active profile's stored OAuth token for a tool.
func (c *Client) UnlinkAgent(ctx context.Context, toolID string) error {
	return c.PostJSON(ctx, "/api/agents/"+url.PathEscape(toolID)+"/unlink", nil, nil)
}

// GetAgentConfig returns an MCP/built-in tool's per-profile LLM + meta config
// (llm_provider, llm_model, reasoning_effort, full_reasoning, system_prompt,
// description, url).
func (c *Client) GetAgentConfig(ctx context.Context, toolID string) (map[string]any, error) {
	var resp struct {
		Config map[string]any `json:"config"`
	}
	if err := c.GetJSON(ctx, "/api/agents/"+url.PathEscape(toolID)+"/config", &resp); err != nil {
		return nil, err
	}
	return resp.Config, nil
}

// UpdateAgentConfig partially updates an MCP/built-in tool's config (only
// keys present in body are written).
func (c *Client) UpdateAgentConfig(ctx context.Context, toolID string, body map[string]any) error {
	return c.PutJSON(ctx, "/api/agents/"+url.PathEscape(toolID)+"/config", body, nil)
}
