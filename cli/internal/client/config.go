package client

import (
	"context"
	"net/url"
)

// GetConfigSchema returns the user_config schema (groups → fields with types,
// defaults, descriptions, enums, min/max).
func (c *Client) GetConfigSchema(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/config/schema", &out); err != nil {
		return nil, err
	}
	return out, nil
}

// GetUserConfig returns current values + defaults for the authenticated profile.
// The server resolves the profile from the JWT in the Authorization header.
func (c *Client) GetUserConfig(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/config/user", &out); err != nil {
		return nil, err
	}
	return out, nil
}

// UpdateUserConfig writes a partial set of user_config values for the
// authenticated profile (resolved server-side from the JWT). The server coerces
// and validates each value against the schema.
func (c *Client) UpdateUserConfig(ctx context.Context, values map[string]any) error {
	body := map[string]any{"values": values}
	return c.PutJSON(ctx, "/api/config/user", body, nil)
}

// ResetUserConfigKey reverts a single key to its declared default.
func (c *Client) ResetUserConfigKey(ctx context.Context, key string) error {
	return c.Delete(ctx, "/api/config/user/"+url.PathEscape(key), nil)
}
