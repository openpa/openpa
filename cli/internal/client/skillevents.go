package client

import (
	"context"
	"net/url"
	"strconv"
)

// ListSkillEventSubscriptions returns the active profile's subscriptions.
func (c *Client) ListSkillEventSubscriptions(ctx context.Context) ([]map[string]any, error) {
	var resp struct {
		Subscriptions []map[string]any `json:"subscriptions"`
	}
	if err := c.GetJSON(ctx, "/api/skill-events", &resp); err != nil {
		return nil, err
	}
	return resp.Subscriptions, nil
}

// DeleteSkillEventSubscription removes a single subscription.
func (c *Client) DeleteSkillEventSubscription(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/skill-events/"+url.PathEscape(id), nil)
}

// SimulateSkillEvent drops a markdown file into the watched events folder so
// the listener fires as if a real event arrived.
func (c *Client) SimulateSkillEvent(ctx context.Context, id, content, filename string) error {
	body := map[string]string{"content": content}
	if filename != "" {
		body["filename"] = filename
	}
	return c.PostJSON(ctx, "/api/skill-events/"+url.PathEscape(id)+"/simulate", body, nil)
}

// SkillEventsAdminStreamPath returns the path for the admin SSE snapshot.
func (c *Client) SkillEventsAdminStreamPath() string {
	return "/api/skill-events/admin/stream"
}

// ListSkillEvents returns the events declared by a skill (from its SKILL.md).
func (c *Client) ListSkillEvents(ctx context.Context, skill string) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/skills/"+url.PathEscape(skill)+"/events", &out); err != nil {
		return nil, err
	}
	return out, nil
}

// GetListenerStatus returns the heartbeat-derived liveness for a skill's
// listener daemon (running, last_heartbeat, autostart_id, command).
func (c *Client) GetListenerStatus(ctx context.Context, skill string) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/skills/"+url.PathEscape(skill)+"/listener-status", &out); err != nil {
		return nil, err
	}
	return out, nil
}

// StartListener spawns a skill's listener daemon as an autostart process.
func (c *Client) StartListener(ctx context.Context, skill string) (map[string]any, error) {
	var out map[string]any
	if err := c.PostJSON(ctx, "/api/skills/"+url.PathEscape(skill)+"/listener-start", nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// SkillEventNotificationsStreamPath returns the per-profile notifications
// stream. Optional `since` is a millis cursor for resuming.
func (c *Client) SkillEventNotificationsStreamPath(since int64) string {
	if since > 0 {
		return "/api/skill-events/notifications/stream?since=" + strconv.FormatInt(since, 10)
	}
	return "/api/skill-events/notifications/stream"
}
