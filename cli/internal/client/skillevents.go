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

// SkillEventNotificationsStreamPath returns the per-profile notifications
// stream. Optional `since` is a millis cursor for resuming.
func (c *Client) SkillEventNotificationsStreamPath(since int64) string {
	if since > 0 {
		return "/api/skill-events/notifications/stream?since=" + strconv.FormatInt(since, 10)
	}
	return "/api/skill-events/notifications/stream"
}
