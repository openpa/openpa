package client

import "context"

// GetSetupStatus returns {setup_complete, profile_exists?, has_profiles?}.
// Optionally include a profile to check whether it already exists. This
// endpoint is intentionally unauthenticated.
func (c *Client) GetSetupStatus(ctx context.Context, profile string) (map[string]any, error) {
	path := "/api/config/setup-status"
	if profile != "" {
		path += "?profile=" + profile
	}
	var out map[string]any
	if err := c.GetJSON(ctx, path, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// SetupResponse mirrors POST /api/config/setup.
type SetupResponse struct {
	Success   bool   `json:"success"`
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
	Profile   string `json:"profile"`
}

// CompleteSetup POSTs the setup payload (unauthenticated). The body shape
// matches the openpa-ui wizard:
//
//	{
//	  "profile": "admin",
//	  "server_config": { ... },
//	  "llm_config": { ... },
//	  "tool_configs": { "<tool_id>": { ... } },
//	  "agent_configs": { "<tool_id>": { ... } }
//	}
func (c *Client) CompleteSetup(ctx context.Context, body map[string]any) (*SetupResponse, error) {
	var out SetupResponse
	if err := c.PostJSON(ctx, "/api/config/setup", body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ResetOrphanedSetup clears setup_complete when no profiles exist. No auth.
func (c *Client) ResetOrphanedSetup(ctx context.Context) error {
	return c.PostJSON(ctx, "/api/config/reset-orphaned-setup", nil, nil)
}

// Reconfigure resets setup_complete (admin auth required).
func (c *Client) Reconfigure(ctx context.Context) error {
	return c.PostJSON(ctx, "/api/config/reconfigure", nil, nil)
}

// GetServerConfig returns non-secret server-wide settings.
func (c *Client) GetServerConfig(ctx context.Context) (map[string]any, error) {
	var resp struct {
		Config map[string]any `json:"config"`
	}
	if err := c.GetJSON(ctx, "/api/config/server", &resp); err != nil {
		return nil, err
	}
	return resp.Config, nil
}

// UpdateServerConfig partially writes server-wide settings. Each value is
// stringified server-side; the special key "jwt_secret" is stored as a secret.
func (c *Client) UpdateServerConfig(ctx context.Context, values map[string]any) error {
	body := map[string]any{"config": values}
	return c.PutJSON(ctx, "/api/config/server", body, nil)
}
