package client

import "context"

// MeResponse is the decoded payload from GET /api/me.
type MeResponse struct {
	Subject        string `json:"sub"`
	Profile        string `json:"profile"`
	IssuedAt       int64  `json:"iat"`
	ExpiresAt      int64  `json:"exp"`
	WorkingDir     string `json:"working_dir"`
	UserWorkingDir string `json:"user_working_dir"`
}

// Me returns identity info derived from the current bearer token.
func (c *Client) Me(ctx context.Context) (*MeResponse, error) {
	var out MeResponse
	if err := c.GetJSON(ctx, "/api/me", &out); err != nil {
		return nil, err
	}
	return &out, nil
}
