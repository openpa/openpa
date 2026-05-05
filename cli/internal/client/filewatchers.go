package client

import (
	"context"
	"net/url"
)

// ListFileWatchers returns the active profile's file watcher subscriptions.
func (c *Client) ListFileWatchers(ctx context.Context) ([]map[string]any, error) {
	var resp struct {
		Subscriptions []map[string]any `json:"subscriptions"`
	}
	if err := c.GetJSON(ctx, "/api/file-watchers", &resp); err != nil {
		return nil, err
	}
	return resp.Subscriptions, nil
}

// DeleteFileWatcher removes a single file watcher subscription.
func (c *Client) DeleteFileWatcher(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/file-watchers/"+url.PathEscape(id), nil)
}

// CreateFileWatcher registers a new file watcher subscription.
//
// `body` follows the schema accepted by POST /api/file-watchers — see the
// Python tool ``register_file_watcher`` for fields (path, name, triggers,
// target_kind, extensions, recursive, action, conversation_id).
func (c *Client) CreateFileWatcher(ctx context.Context, body map[string]any) (map[string]any, error) {
	var out map[string]any
	if err := c.PostJSON(ctx, "/api/file-watchers", body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// FileWatchersAdminStreamPath returns the path for the admin SSE snapshot.
func (c *Client) FileWatchersAdminStreamPath() string {
	return "/api/file-watchers/admin/stream"
}
