package client

import (
	"context"
	"net/url"
)

// Channel represents one row from /api/channels.
type Channel struct {
	ID            string                 `json:"id"`
	Profile       string                 `json:"profile"`
	ChannelType   string                 `json:"channel_type"`
	Mode          string                 `json:"mode"`
	AuthMode      string                 `json:"auth_mode"`
	ResponseMode  string                 `json:"response_mode"`
	Enabled       bool                   `json:"enabled"`
	Status        string                 `json:"status"`
	Config        map[string]any         `json:"config"`
	State         map[string]any         `json:"state"`
	CreatedAt     float64                `json:"created_at"`
	UpdatedAt     float64                `json:"updated_at"`
}

// CreateChannelRequest is the body for POST /api/channels.
type CreateChannelRequest struct {
	ChannelType  string         `json:"channel_type"`
	Mode         string         `json:"mode,omitempty"`
	AuthMode     string         `json:"auth_mode,omitempty"`
	ResponseMode string         `json:"response_mode,omitempty"`
	Enabled      *bool          `json:"enabled,omitempty"`
	Config       map[string]any `json:"config,omitempty"`
}

// UpdateChannelRequest is the body for PATCH /api/channels/{id}. All fields optional.
type UpdateChannelRequest struct {
	Mode         *string        `json:"mode,omitempty"`
	AuthMode     *string        `json:"auth_mode,omitempty"`
	ResponseMode *string        `json:"response_mode,omitempty"`
	Enabled      *bool          `json:"enabled,omitempty"`
	Config       map[string]any `json:"config,omitempty"`
}

// ListChannels returns all channels for the active profile.
func (c *Client) ListChannels(ctx context.Context) ([]Channel, error) {
	var resp struct {
		Channels []Channel `json:"channels"`
	}
	if err := c.GetJSON(ctx, "/api/channels", &resp); err != nil {
		return nil, err
	}
	return resp.Channels, nil
}

// GetChannelCatalog returns the TOML-defined catalog (per-type metadata).
func (c *Client) GetChannelCatalog(ctx context.Context) (map[string]any, error) {
	var resp struct {
		Channels map[string]any `json:"channels"`
	}
	if err := c.GetJSON(ctx, "/api/channels/catalog", &resp); err != nil {
		return nil, err
	}
	return resp.Channels, nil
}

// CreateChannel registers a new channel; returns the created row.
func (c *Client) CreateChannel(ctx context.Context, req CreateChannelRequest) (*Channel, error) {
	var resp struct {
		Channel Channel `json:"channel"`
	}
	if err := c.PostJSON(ctx, "/api/channels", req, &resp); err != nil {
		return nil, err
	}
	return &resp.Channel, nil
}

// UpdateChannel patches a channel.
func (c *Client) UpdateChannel(ctx context.Context, id string, req UpdateChannelRequest) (*Channel, error) {
	var resp struct {
		Channel Channel `json:"channel"`
	}
	if err := c.PatchJSON(ctx, "/api/channels/"+url.PathEscape(id), req, &resp); err != nil {
		return nil, err
	}
	return &resp.Channel, nil
}

// DeleteChannel removes a channel (cascades conversations + senders).
func (c *Client) DeleteChannel(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/channels/"+url.PathEscape(id), nil)
}

// ChannelAuthEventsPath returns the path for the channel's pairing-event SSE
// stream — used by `opa channels pair` to render the QR / prompt for codes.
func (c *Client) ChannelAuthEventsPath(id string) string {
	return "/api/channels/" + url.PathEscape(id) + "/auth-events"
}

// SubmitChannelAuthInput POSTs a verification code or 2FA password back to a
// channel that's in the middle of an interactive pairing flow.
//
// Either ``code`` or ``password`` should be non-empty. The server returns
// ``409 No auth input expected`` when the adapter isn't waiting for input
// (most often because pairing already completed).
func (c *Client) SubmitChannelAuthInput(ctx context.Context, id, code, password string) error {
	body := map[string]string{}
	if code != "" {
		body["code"] = code
	}
	if password != "" {
		body["password"] = password
	}
	return c.PostJSON(ctx, "/api/channels/"+url.PathEscape(id)+"/auth-input", body, nil)
}
