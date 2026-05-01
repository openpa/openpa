package tui

import (
	"encoding/json"
	"fmt"
	"strings"

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
		// Server publishes `{"id", "content", "metadata"}` — the request text
		// lives in `content`, not `text`.
		var p struct {
			Data struct {
				Content string `json:"content"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		return renderedEvent{
			kind: "user",
			body: t.UserMsg.Render("you: ") + p.Data.Content,
		}, true
	case "thinking":
		// Server publishes the agent's ReAct chunk verbatim. Field names use
		// the agent's capitalized convention (`Thought`, `Action`,
		// `Action_Input`); Observation is NOT in this event — it arrives in
		// a separate `result` event handled below.
		var p struct {
			Data struct {
				Thought     string `json:"Thought"`
				Action      string `json:"Action"`
				ActionInput any    `json:"Action_Input"`
				ModelLabel  string `json:"Model_Label"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		body := ""
		if p.Data.Thought != "" {
			body += t.Thinking.Render("◇ Thought: "+p.Data.Thought) + "\n"
		}
		if p.Data.Action != "" {
			body += t.Dim.Render("  → Action: "+p.Data.Action) + "\n"
		}
		if input := formatActionInput(p.Data.ActionInput); input != "" {
			body += t.Dim.Render("    Input: "+input) + "\n"
		}
		if body == "" {
			return renderedEvent{}, false
		}
		return renderedEvent{kind: "thinking", body: body}, true
	case "text":
		// Server streams the assistant's response as one event per token in
		// `data.token`. The chat model accumulates consecutive text events
		// into a single growing message (see chat.go).
		var p struct {
			Data struct {
				Token string `json:"token"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		if p.Data.Token == "" {
			return renderedEvent{}, false
		}
		return renderedEvent{kind: "text", body: t.AssistMsg.Render(p.Data.Token)}, true
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
		// `result` carries the tool observation that follows the most recent
		// thinking event in the ReAct loop. It belongs in the Thinking
		// Process section, not the final response.
		var p struct {
			Data struct {
				Observation string `json:"Observation"`
			} `json:"data"`
		}
		_ = json.Unmarshal(ev.Raw, &p)
		if p.Data.Observation == "" {
			return renderedEvent{}, false
		}
		obs := strings.ReplaceAll(p.Data.Observation, "\n", "\n    ")
		body := t.Dim.Render("  ◂ Observation: "+obs) + "\n"
		return renderedEvent{kind: "thinking", body: body}, true
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

// formatActionInput renders the ReAct action_input field. Strings pass through
// verbatim; structured values get pretty-printed JSON aligned under the
// "Input:" label so nested fields stay visually grouped. Returns "" for absent
// inputs (nil, empty string, or unmarshal failure on an empty payload).
func formatActionInput(v any) string {
	switch x := v.(type) {
	case nil:
		return ""
	case string:
		return x
	}
	b, err := json.MarshalIndent(v, "    ", "  ")
	if err != nil {
		return fmt.Sprintf("%v", v)
	}
	return string(b)
}

// extractTokenUsage parses a token_usage event into a one-line status string.
// Returns "" for unparseable events.
func extractTokenUsage(ev client.Event) string {
	if ev.Type != "token_usage" {
		return ""
	}
	// Server nests usage under data.token_usage.
	var p struct {
		Data struct {
			TokenUsage struct {
				InputTokens  int `json:"input_tokens"`
				OutputTokens int `json:"output_tokens"`
			} `json:"token_usage"`
		} `json:"data"`
	}
	if err := json.Unmarshal(ev.Raw, &p); err != nil {
		return ""
	}
	in, out := p.Data.TokenUsage.InputTokens, p.Data.TokenUsage.OutputTokens
	total := in + out
	if total == 0 {
		return ""
	}
	return fmt.Sprintf("tokens: %d  (in %d / out %d)", total, in, out)
}
