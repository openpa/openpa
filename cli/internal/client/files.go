package client

import (
	"context"
	"net/url"
)

// DirectoryEntry mirrors a single row from /api/files/list.
type DirectoryEntry struct {
	Name     string  `json:"name"`
	Path     string  `json:"path"`
	IsDir    bool    `json:"is_dir"`
	Size     *int64  `json:"size"`
	Modified float64 `json:"modified"`
}

// DirectoryListing is the response payload from /api/files/list.
type DirectoryListing struct {
	Path      string           `json:"path"`
	Entries   []DirectoryEntry `json:"entries"`
	Truncated bool             `json:"truncated"`
}

// ListDirectory fetches a non-streaming snapshot of a directory's contents.
// The path must resolve inside the server's allowed bases (OPENPA_WORKING_DIR
// or the user working dir); otherwise the call returns an APIError 403.
//
// ``conversationID`` is optional. When non-empty the server widens the
// allowlist to include the conversation's ``_working_directory_override``
// — needed for paths the agent switched into via
// ``change_working_directory`` with target='custom'.
func (c *Client) ListDirectory(ctx context.Context, path string, showHidden bool, conversationID string) (*DirectoryListing, error) {
	hidden := "0"
	if showHidden {
		hidden = "1"
	}
	q := url.Values{}
	q.Set("path", path)
	q.Set("show_hidden", hidden)
	if conversationID != "" {
		q.Set("conversation_id", conversationID)
	}
	var out DirectoryListing
	if err := c.GetJSON(ctx, "/api/files/list?"+q.Encode(), &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetUserCwd returns the seed working directory used to render the file tree
// before any agent terminal has reported a fresh cwd.
func (c *Client) GetUserCwd(ctx context.Context) (string, error) {
	var out struct {
		Cwd string `json:"cwd"`
	}
	if err := c.GetJSON(ctx, "/api/files/cwd", &out); err != nil {
		return "", err
	}
	return out.Cwd, nil
}

// WatchFilesPath returns the SSE stream path for the watchdog endpoint
// rooted at ``path``. Callers should pass the result to Stream(ctx, ...).
// ``conversationID`` is optional; see ``ListDirectory`` for semantics.
func WatchFilesPath(path string, conversationID string) string {
	q := url.Values{}
	q.Set("path", path)
	if conversationID != "" {
		q.Set("conversation_id", conversationID)
	}
	return "/api/files/watch?" + q.Encode()
}
