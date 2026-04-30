package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"openpa.local/cli/internal/config"
)

type Client struct {
	cfg  *config.Config
	http *http.Client
}

func New(cfg *config.Config) *Client {
	return &Client{
		cfg:  cfg,
		http: &http.Client{Timeout: 60 * time.Second},
	}
}

// Server returns the base server URL.
func (c *Client) Server() string { return c.cfg.Server }

// Token returns the bearer token. May be empty for unauthenticated calls.
func (c *Client) Token() string { return c.cfg.Token }

// HTTPClient exposes the underlying http.Client for callers that need streaming
// (SSE, raw bodies). Auth headers are NOT injected — use NewRequest for those.
func (c *Client) HTTPClient() *http.Client { return c.http }

// Endpoint joins the server base URL with a path.
func (c *Client) Endpoint(path string) string {
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	return c.cfg.Server + path
}

// NewRequest builds a request with the bearer token attached.
func (c *Client) NewRequest(ctx context.Context, method, path string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.Endpoint(path), body)
	if err != nil {
		return nil, err
	}
	if c.cfg.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.cfg.Token)
	}
	return req, nil
}

// APIError is returned when the server responds with a non-2xx status. The
// Body field carries any decoded `{"error": "..."}` message; otherwise Raw
// holds the response body.
type APIError struct {
	Status int
	Body   string
	Raw    []byte
}

func (e *APIError) Error() string {
	if e.Body != "" {
		return fmt.Sprintf("server returned %d: %s", e.Status, e.Body)
	}
	return fmt.Sprintf("server returned %d", e.Status)
}

// Do executes a request and returns the response. Caller is responsible for
// closing the body. If the response is non-2xx, the body is fully read and
// wrapped in an APIError.
func (c *Client) Do(req *http.Request) (*http.Response, error) {
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		defer resp.Body.Close()
		raw, _ := io.ReadAll(resp.Body)
		apiErr := &APIError{Status: resp.StatusCode, Raw: raw}
		var e struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(raw, &e) == nil && e.Error != "" {
			apiErr.Body = e.Error
		} else {
			apiErr.Body = strings.TrimSpace(string(raw))
		}
		return nil, apiErr
	}
	return resp, nil
}

// GetJSON performs GET <path> and decodes the JSON response into out.
func (c *Client) GetJSON(ctx context.Context, path string, out any) error {
	req, err := c.NewRequest(ctx, http.MethodGet, path, nil)
	if err != nil {
		return err
	}
	resp, err := c.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if out == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// PostJSON marshals body to JSON, POSTs it to path, and decodes the response into out.
// Pass nil body for empty POSTs, nil out to discard the response.
func (c *Client) PostJSON(ctx context.Context, path string, body, out any) error {
	return c.bodyJSON(ctx, http.MethodPost, path, body, out)
}

// PutJSON marshals body to JSON, PUTs it to path, and decodes the response into out.
func (c *Client) PutJSON(ctx context.Context, path string, body, out any) error {
	return c.bodyJSON(ctx, http.MethodPut, path, body, out)
}

// Delete performs DELETE <path>. If out is non-nil, the JSON body is decoded.
func (c *Client) Delete(ctx context.Context, path string, out any) error {
	req, err := c.NewRequest(ctx, http.MethodDelete, path, nil)
	if err != nil {
		return err
	}
	resp, err := c.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if out == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *Client) bodyJSON(ctx context.Context, method, path string, body, out any) error {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("marshal request body: %w", err)
		}
		reader = bytes.NewReader(raw)
	}
	req, err := c.NewRequest(ctx, method, path, reader)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if out == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		return nil
	}
	dec := json.NewDecoder(resp.Body)
	if err := dec.Decode(out); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	return nil
}
