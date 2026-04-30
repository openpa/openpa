package client

import (
	"context"
	"net/url"
)

// ListLLMProviders returns the provider catalog with config status.
func (c *Client) ListLLMProviders(ctx context.Context) ([]map[string]any, error) {
	var resp struct {
		Providers []map[string]any `json:"providers"`
	}
	if err := c.GetJSON(ctx, "/api/llm/providers", &resp); err != nil {
		return nil, err
	}
	return resp.Providers, nil
}

// GetProviderModels returns the model catalog for a single provider.
func (c *Client) GetProviderModels(ctx context.Context, provider string) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/llm/providers/"+url.PathEscape(provider)+"/models", &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ConfigureProvider writes provider config keys (api_key, auth_method, …).
// Each value is sent as-is; the server flags secret keys based on the catalog.
func (c *Client) ConfigureProvider(ctx context.Context, provider string, kv map[string]any) error {
	return c.PutJSON(ctx, "/api/llm/providers/"+url.PathEscape(provider), kv, nil)
}

// DeleteProviderConfig removes all stored config for a provider.
func (c *Client) DeleteProviderConfig(ctx context.Context, provider string) error {
	return c.Delete(ctx, "/api/llm/providers/"+url.PathEscape(provider)+"/config", nil)
}

// GetModelGroups returns current model_group + reasoning_effort assignments.
func (c *Client) GetModelGroups(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/llm/model-groups", &out); err != nil {
		return nil, err
	}
	return out, nil
}

// UpdateModelGroups patches model_group / reasoning_efforts / default_provider.
func (c *Client) UpdateModelGroups(ctx context.Context, body map[string]any) error {
	return c.PutJSON(ctx, "/api/llm/model-groups", body, nil)
}

// DeviceCodeStartResponse is the GitHub device-code start payload.
type DeviceCodeStartResponse struct {
	VerificationURI string `json:"verification_uri"`
	UserCode        string `json:"user_code"`
	DeviceCode      string `json:"device_code"`
	ExpiresIn       int    `json:"expires_in"`
	Interval        int    `json:"interval"`
}

// DeviceCodeStart kicks off the GitHub Copilot device-code flow.
func (c *Client) DeviceCodeStart(ctx context.Context) (*DeviceCodeStartResponse, error) {
	var out DeviceCodeStartResponse
	if err := c.PostJSON(ctx, "/api/llm/auth/device-code/start", nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// DeviceCodePollResponse mirrors the poll endpoint reply. Status is one of
// "pending", "expired", "complete", "error". When complete-and-unauthenticated,
// AccessToken is populated; otherwise the server stores the token under the
// active profile.
type DeviceCodePollResponse struct {
	Status      string `json:"status"`
	SlowDown    bool   `json:"slow_down,omitempty"`
	AccessToken string `json:"access_token,omitempty"`
	Error       string `json:"error,omitempty"`
}

// DeviceCodePoll polls GitHub for a token using a previously-issued device_code.
func (c *Client) DeviceCodePoll(ctx context.Context, deviceCode string) (*DeviceCodePollResponse, error) {
	var out DeviceCodePollResponse
	body := map[string]string{"device_code": deviceCode}
	if err := c.PostJSON(ctx, "/api/llm/auth/device-code/poll", body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
