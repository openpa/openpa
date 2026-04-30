// Package stream wires the SSE conversation stream into a renderer abstraction
// that's reusable across `opa conv send`, `opa conv attach`, and `opa chat`.
//
// The pipeline is "subscribe-first, then send" — open the SSE stream and wait
// for the `ready` event before posting the user message, so we can't miss
// early events from a fast run.
package stream

import (
	"context"
	"errors"
	"fmt"

	"openpa.local/cli/internal/client"
)

// Renderer consumes SSE events. Render returns false to end the loop.
// Stop is called once when the loop ends (cleanly or with an error).
type Renderer interface {
	Render(ev client.Event) (cont bool)
	Stop(err error)
}

// Options configures Run.
type Options struct {
	ConversationID string
	// SendText, when non-empty, is POSTed to /messages after the SSE stream
	// reports `ready`. Leave empty for `attach`-style watching.
	SendText  string
	Reasoning bool
	// OnRunID is called once with the run_id returned by SendMessage.
	OnRunID func(runID string)
}

// Run executes the one-shot streaming pipeline (`conv send` and `conv attach`).
// `chat` builds its own loop on top of client.Stream + client.SendMessage so
// it can issue multiple sends over a single SSE stream.
func Run(ctx context.Context, c *client.Client, opts Options, r Renderer) error {
	if opts.ConversationID == "" {
		return errors.New("conversation_id is required")
	}

	streamPath := c.ConversationStreamPath(opts.ConversationID)
	events, errs := c.Stream(ctx, streamPath)

	ready := false
	sent := false
	finalErr := error(nil)

loop:
	for {
		select {
		case <-ctx.Done():
			finalErr = ctx.Err()
			break loop
		case err, open := <-errs:
			if !open {
				errs = nil
				continue
			}
			if err != nil {
				finalErr = err
				break loop
			}
		case ev, open := <-events:
			if !open {
				break loop
			}
			if ev.Type == "ready" && !ready {
				ready = true
				if opts.SendText != "" && !sent {
					resp, err := c.SendMessage(ctx, opts.ConversationID, opts.SendText, opts.Reasoning)
					if err != nil {
						finalErr = fmt.Errorf("send message: %w", err)
						break loop
					}
					sent = true
					if opts.OnRunID != nil {
						opts.OnRunID(resp.RunID)
					}
				}
			}
			if cont := r.Render(ev); !cont {
				break loop
			}
		}
	}

	r.Stop(finalErr)
	return finalErr
}
