package client

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
)

// ListConversations returns paginated conversation summaries for the
// authenticated profile (resolved server-side from the JWT). Each entry is a
// loose map (id, title, task_id, created_at, …).
func (c *Client) ListConversations(ctx context.Context, limit, offset int) ([]map[string]any, error) {
	q := url.Values{}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	if offset > 0 {
		q.Set("offset", strconv.Itoa(offset))
	}
	path := "/api/conversations"
	if len(q) > 0 {
		path += "?" + q.Encode()
	}

	var resp struct {
		Conversations []map[string]any `json:"conversations"`
	}
	if err := c.GetJSON(ctx, path, &resp); err != nil {
		return nil, err
	}
	return resp.Conversations, nil
}

// CreateConversation creates an empty conversation and returns its details.
func (c *Client) CreateConversation(ctx context.Context, title string) (map[string]any, error) {
	body := map[string]string{}
	if title != "" {
		body["title"] = title
	}
	var resp struct {
		Conversation map[string]any `json:"conversation"`
	}
	if err := c.PostJSON(ctx, "/api/conversations", body, &resp); err != nil {
		return nil, err
	}
	return resp.Conversation, nil
}

// GetConversation returns the conversation plus its full message history.
func (c *Client) GetConversation(ctx context.Context, id string) (map[string]any, error) {
	var out map[string]any
	if err := c.GetJSON(ctx, "/api/conversations/"+url.PathEscape(id), &out); err != nil {
		return nil, err
	}
	return out, nil
}

// GetMessages returns paginated messages for a conversation.
func (c *Client) GetMessages(ctx context.Context, id string, limit, offset int) ([]map[string]any, error) {
	q := url.Values{}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	if offset > 0 {
		q.Set("offset", strconv.Itoa(offset))
	}
	path := "/api/conversations/" + url.PathEscape(id) + "/messages"
	if len(q) > 0 {
		path += "?" + q.Encode()
	}
	var resp struct {
		Messages []map[string]any `json:"messages"`
	}
	if err := c.GetJSON(ctx, path, &resp); err != nil {
		return nil, err
	}
	return resp.Messages, nil
}

// SendMessageResponse mirrors the 202 reply: a run_id the caller can use to
// cancel the run, plus an echo of the conversation id.
type SendMessageResponse struct {
	RunID          string `json:"run_id"`
	ConversationID string `json:"conversation_id"`
}

// SendMessage enqueues a user turn for the agent. The response is returned
// immediately; events are received via Stream() on the conversation stream
// endpoint.
func (c *Client) SendMessage(ctx context.Context, id, text string, reasoning bool) (*SendMessageResponse, error) {
	body := map[string]any{
		"text":      text,
		"reasoning": reasoning,
	}
	var out SendMessageResponse
	if err := c.PostJSON(ctx, "/api/conversations/"+url.PathEscape(id)+"/messages", body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// UpdateConversation patches title and/or task_id.
func (c *Client) UpdateConversation(ctx context.Context, id string, fields map[string]any) error {
	return c.PutJSON(ctx, "/api/conversations/"+url.PathEscape(id), fields, nil)
}

// DeleteConversation removes a conversation.
func (c *Client) DeleteConversation(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/conversations/"+url.PathEscape(id), nil)
}

// DeleteAllConversations clears every conversation for the authenticated
// profile (resolved server-side from the JWT). Returns the number deleted.
func (c *Client) DeleteAllConversations(ctx context.Context) (int, error) {
	var resp struct {
		Success      bool `json:"success"`
		DeletedCount int  `json:"deleted_count"`
	}
	if err := c.bodyJSON(ctx, "DELETE", "/api/conversations", nil, &resp); err != nil {
		return 0, err
	}
	return resp.DeletedCount, nil
}

// CancelTask cancels an in-flight agent run by its run_id (or task_id).
// Returns whether the cancellation was honored.
func (c *Client) CancelTask(ctx context.Context, taskID string) (bool, error) {
	var resp struct {
		Cancelled bool `json:"cancelled"`
	}
	path := fmt.Sprintf("/api/tasks/%s/cancel", url.PathEscape(taskID))
	if err := c.PostJSON(ctx, path, nil, &resp); err != nil {
		return false, err
	}
	return resp.Cancelled, nil
}

// ConversationStreamPath returns the SSE stream path for a conversation.
func (c *Client) ConversationStreamPath(id string) string {
	return "/api/conversations/" + url.PathEscape(id) + "/stream"
}
