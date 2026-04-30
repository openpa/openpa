package tui

import (
	"encoding/json"
	"fmt"

	"openpa.local/cli/internal/client"
)

// renderedEvent is a presentation-layer record of one SSE frame, ready to be
// concatenated into the viewport content.
type renderedEvent struct {
	kind string // "user", "thinking", "text", "phase", "summary", "terminal", "file", "error", "info"
	body string
}

// formatEvent translates an SSE event into a renderedEvent. Unknown event
// types are dropped (returns ok=false).
func formatEvent(ev client.Event, t Theme) (renderedEvent, bool) {
	switch ev.Type {
	case "ready":
		return renderedEvent{kind: "info", body: t.Dim.Render("• connected")}, true
	case "user_message":
		var p struct {
			Data struct {
				Text string `json:"text"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		return renderedEvent{
			kind: "user",
			body: t.UserMsg.Render("you: ") + p.Data.Text,
		}, true
	case "thinking":
		var p struct {
			Data struct {
				Thought       string `json:"thought"`
				Action        string `json:"action"`
				ActionInput   any    `json:"action_input"`
				Observation   string `json:"observation"`
				ModelLabel    string `json:"model_label"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		body := ""
		if p.Data.Thought != "" {
			body += t.Thinking.Render("◇ "+p.Data.Thought) + "\n"
		}
		if p.Data.Action != "" {
			body += t.Dim.Render("  → "+p.Data.Action) + "\n"
		}
		if body == "" {
			return renderedEvent{}, false
		}
		return renderedEvent{kind: "thinking", body: body}, true
	case "text":
		var p struct {
			Data struct {
				Text string `json:"text"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		if p.Data.Text == "" {
			return renderedEvent{}, false
		}
		return renderedEvent{kind: "text", body: t.AssistMsg.Render(p.Data.Text)}, true
	case "phase":
		var p struct {
			Data struct {
				Phase string `json:"phase"`
				Label string `json:"label"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		label := p.Data.Label
		if label == "" {
			label = p.Data.Phase
		}
		if label == "" {
			return renderedEvent{}, false
		}
		return renderedEvent{kind: "phase", body: t.Phase.Render("» " + label)}, true
	case "summary":
		var p struct {
			Data struct {
				Summary string `json:"summary"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		if p.Data.Summary == "" {
			return renderedEvent{}, false
		}
		return renderedEvent{kind: "summary", body: t.Dim.Render("Σ "+p.Data.Summary)}, true
	case "terminal":
		var p struct {
			Data struct {
				Command string `json:"command"`
				Output  string `json:"output"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		body := t.Box.Render("$ " + p.Data.Command + "\n" + p.Data.Output)
		return renderedEvent{kind: "terminal", body: body}, true
	case "file":
		var p struct {
			Data struct {
				Path    string `json:"path"`
				Action  string `json:"action"`
				Content string `json:"content"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		head := p.Data.Action + " " + p.Data.Path
		body := t.Box.Render(head)
		return renderedEvent{kind: "file", body: body}, true
	case "token_usage":
		// Status-bar concern, not a viewport entry.
		return renderedEvent{}, false
	case "result":
		var p struct {
			Data struct {
				Result string `json:"result"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		if p.Data.Result == "" {
			return renderedEvent{}, false
		}
		return renderedEvent{kind: "text", body: t.AssistMsg.Render(p.Data.Result)}, true
	case "complete":
		return renderedEvent{kind: "info", body: t.Dim.Render("• run complete")}, true
	case "error":
		var p struct {
			Data struct {
				Error   string `json:"error"`
				Message string `json:"message"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		msg := p.Data.Message
		if msg == "" {
			msg = p.Data.Error
		}
		if msg == "" {
			msg = "error"
		}
		return renderedEvent{kind: "error", body: t.Err.Render("✗ " + msg)}, true
	}
	return renderedEvent{}, false
}

// extractTokenUsage parses a token_usage event into a one-line status string.
// Returns "" for unparseable events.
func extractTokenUsage(ev client.Event) string {
	if ev.Type != "token_usage" {
		return ""
	}
	var p struct {
		Data struct {
			InputTokens  int `json:"input_tokens"`
			OutputTokens int `json:"output_tokens"`
			TotalTokens  int `json:"total_tokens"`
		} `json:"data"`
	}
	if err := json.Unmarshal(ev.Raw, &p); err != nil {
		return ""
	}
	in, out, total := p.Data.InputTokens, p.Data.OutputTokens, p.Data.TotalTokens
	if total == 0 {
		total = in + out
	}
	if total == 0 {
		return ""
	}
	return fmt.Sprintf("tokens: %d  (in %d / out %d)", total, in, out)
}
