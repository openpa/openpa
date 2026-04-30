package client

import (
	"context"
	"net/url"
)

// ListProfiles returns the visible profile names.
func (c *Client) ListProfiles(ctx context.Context) ([]string, error) {
	var resp struct {
		Profiles []string `json:"profiles"`
	}
	if err := c.GetJSON(ctx, "/api/profiles", &resp); err != nil {
		return nil, err
	}
	return resp.Profiles, nil
}

// CreateProfile creates a new profile.
func (c *Client) CreateProfile(ctx context.Context, name string) error {
	return c.PostJSON(ctx, "/api/profiles", map[string]string{"name": name}, nil)
}

// DeleteProfile removes a profile and cascades its conversations/tools/skills.
func (c *Client) DeleteProfile(ctx context.Context, name string) error {
	return c.Delete(ctx, "/api/profiles/"+url.PathEscape(name), nil)
}

// GetPersona returns the markdown persona for a profile.
func (c *Client) GetPersona(ctx context.Context, name string) (string, error) {
	var resp struct {
		Content string `json:"content"`
	}
	if err := c.GetJSON(ctx, "/api/profiles/"+url.PathEscape(name)+"/persona", &resp); err != nil {
		return "", err
	}
	return resp.Content, nil
}

// SetPersona overwrites the persona text for a profile.
func (c *Client) SetPersona(ctx context.Context, name, content string) error {
	return c.PutJSON(ctx, "/api/profiles/"+url.PathEscape(name)+"/persona",
		map[string]string{"content": content}, nil)
}

// GetSkillMode returns "manual" or "automatic".
func (c *Client) GetSkillMode(ctx context.Context, name string) (string, error) {
	var resp struct {
		Mode string `json:"mode"`
	}
	if err := c.GetJSON(ctx, "/api/profiles/"+url.PathEscape(name)+"/skill-mode", &resp); err != nil {
		return "", err
	}
	return resp.Mode, nil
}

// SetSkillMode flips a profile between manual and automatic skill mode.
func (c *Client) SetSkillMode(ctx context.Context, name, mode string) error {
	return c.PutJSON(ctx, "/api/profiles/"+url.PathEscape(name)+"/skill-mode",
		map[string]string{"mode": mode}, nil)
}
