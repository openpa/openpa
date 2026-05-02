package cmd

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/client"
	"openpa.local/cli/internal/output"
	"openpa.local/cli/internal/stream"
	"openpa.local/cli/internal/tui"
)

func newConvCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "conv",
		Aliases: []string{"conversation"},
		Short:   "Manage conversations and stream agent runs",
	}
	cmd.AddCommand(
		newConvListCmd(),
		newConvNewCmd(),
		newConvGetCmd(),
		newConvHistoryCmd(),
		newConvSendCmd(),
		newConvAttachCmd(),
		newConvRenameCmd(),
		newConvSetIDCmd(),
		newConvCancelCmd(),
		newConvDeleteCmd(),
		newConvDeleteAllCmd(),
	)
	return cmd
}

func newConvListCmd() *cobra.Command {
	var limit, offset int
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List conversations for the active profile",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			convs, err := state.Client.ListConversations(cmd.Context(), limit, offset)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(convs)
			}
			t := output.NewTable(state.Output, "ID", "TITLE", "CREATED_AT", "TASK_ID")
			for _, c := range convs {
				t.AddRow(
					stringField(c, "id"),
					stringField(c, "title"),
					stringField(c, "created_at"),
					stringField(c, "task_id"),
				)
			}
			t.Render()
			return nil
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 50, "Page size")
	cmd.Flags().IntVar(&offset, "offset", 0, "Offset for pagination")
	return cmd
}

func newConvNewCmd() *cobra.Command {
	var title string
	cmd := &cobra.Command{
		Use:   "new",
		Short: "Create a new conversation; prints the conversation id",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			conv, err := state.Client.CreateConversation(cmd.Context(), title)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(conv)
			}
			id := stringField(conv, "id")
			if isTTYStdout() {
				output.PrintKV([][2]string{
					{"id", id},
					{"title", stringField(conv, "title")},
					{"created_at", stringField(conv, "created_at")},
				})
			} else {
				output.Println(id)
			}
			return nil
		},
	}
	cmd.Flags().StringVarP(&title, "title", "t", "", "Conversation title")
	return cmd
}

func newConvGetCmd() *cobra.Command {
	var detail bool
	cmd := &cobra.Command{
		Use:   "get <id>",
		Short: "Fetch a conversation with its full message history",
		Long: `Print a conversation summary plus its messages.

  --detail   open a TUI that replays the full thinking-process trace
             (Thought / Action / Input / Observation / Response) for every
             agent turn. Press ESC to exit.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			out, err := state.Client.GetConversation(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(out)
			}
			conv, _ := out["conversation"].(map[string]any)
			messages, _ := out["messages"].([]any)
			if detail {
				return tui.Run(cmd.Context(), tui.Config{
					Client:         state.Client,
					ConversationID: stringField(conv, "id"),
					Title:          stringField(conv, "title"),
					Mode:           tui.ModeReplay,
					Replay:         buildReplayEvents(messages),
					Theme:          themeForCfg(),
				})
			}
			output.PrintKV([][2]string{
				{"id", stringField(conv, "id")},
				{"title", stringField(conv, "title")},
				{"task_id", stringField(conv, "task_id")},
				{"created_at", stringField(conv, "created_at")},
			})
			fmt.Println()
			fmt.Println("--- messages ---")
			for _, m := range messages {
				mm, ok := m.(map[string]any)
				if !ok {
					continue
				}
				fmt.Printf("[%s] %s\n", stringField(mm, "role"), stringField(mm, "content"))
			}
			return nil
		},
	}
	cmd.Flags().BoolVar(&detail, "detail", false, "Open a TUI that replays the full thinking-process trace")
	return cmd
}

// buildReplayEvents converts the persisted message list returned by
// GetConversation into the same SSE-event shape the live stream produces, so
// the TUI can re-render a completed conversation faithfully (including each
// ReAct step's Thought / Action / Input / Observation).
func buildReplayEvents(messages []any) []client.Event {
	events := make([]client.Event, 0, len(messages)*4+1)
	for _, m := range messages {
		mm, ok := m.(map[string]any)
		if !ok {
			continue
		}
		role := stringField(mm, "role")
		content := stringField(mm, "content")
		switch role {
		case "user":
			events = append(events, makeReplayEvent("user_message", map[string]any{
				"content": content,
			}))
		case "agent", "assistant":
			steps, _ := mm["thinking_steps"].([]any)
			for _, s := range steps {
				step, ok := s.(map[string]any)
				if !ok {
					continue
				}
				// Persisted steps use lowercase keys; the wire format (and
				// the TUI's events.go parser) uses the agent's capitalized
				// keys. Translate so the renderer sees a familiar shape.
				events = append(events, makeReplayEvent("thinking", map[string]any{
					"Thought":      stringField(step, "thought"),
					"Action":       stringField(step, "action"),
					"Action_Input": step["action_input"],
					"Model_Label":  step["model_label"],
				}))
				if obs := stringField(step, "observation"); obs != "" {
					events = append(events, makeReplayEvent("result", map[string]any{
						"Observation": obs,
					}))
				}
			}
			if content != "" {
				events = append(events, makeReplayEvent("text", map[string]any{
					"token": content,
				}))
			}
		}
	}
	events = append(events, makeReplayEvent("complete", map[string]any{}))
	return events
}

// makeReplayEvent wraps a synthesized payload in the same {type, data} envelope
// the SSE pipeline produces, so the TUI's formatEvent can read it unchanged.
func makeReplayEvent(typ string, data map[string]any) client.Event {
	raw, _ := json.Marshal(map[string]any{"type": typ, "data": data})
	return client.Event{Type: typ, Raw: raw}
}

func newConvHistoryCmd() *cobra.Command {
	var limit, offset int
	cmd := &cobra.Command{
		Use:   "history <id>",
		Short: "Show paginated message history for a conversation",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			msgs, err := state.Client.GetMessages(cmd.Context(), args[0], limit, offset)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(msgs)
			}
			for _, m := range msgs {
				fmt.Printf("[%s] %s\n", stringField(m, "role"), stringField(m, "content"))
			}
			return nil
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 100, "Page size")
	cmd.Flags().IntVar(&offset, "offset", 0, "Offset for pagination")
	return cmd
}

func newConvSendCmd() *cobra.Command {
	var raw, noReasoning bool
	cmd := &cobra.Command{
		Use:   "send <id> <message>",
		Short: "Send a message; default streams the thinking process via TUI",
		Long: `Send a user message to an existing conversation. The default mode
opens a TUI that renders thinking, text, terminal output, and other events as
they stream from the agent.

  --raw    print only the assistant text to stdout (pipe-friendly)
  --json   emit each SSE event as a JSON line to stdout (machine-readable)`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			ctx, cancel := context.WithCancel(cmd.Context())
			defer cancel()

			convID, text := args[0], args[1]
			reasoning := !noReasoning

			if state.JSON {
				return stream.Run(ctx, state.Client, stream.Options{
					ConversationID: convID,
					SendText:       text,
					Reasoning:      reasoning,
				}, stream.JSONRenderer{})
			}
			if raw {
				return stream.Run(ctx, state.Client, stream.Options{
					ConversationID: convID,
					SendText:       text,
					Reasoning:      reasoning,
				}, stream.RawRenderer{})
			}
			return tui.Run(ctx, tui.Config{
				Client:         state.Client,
				ConversationID: convID,
				Mode:           tui.ModeOneShot,
				InitialMsg:     text,
				Reasoning:      reasoning,
				Theme:          themeForCfg(),
			})
		},
	}
	cmd.Flags().BoolVar(&raw, "raw", false, "Plain text output (no TUI)")
	cmd.Flags().BoolVar(&noReasoning, "no-reasoning", false, "Disable reasoning mode for this message")
	return cmd
}

func newConvAttachCmd() *cobra.Command {
	var raw bool
	cmd := &cobra.Command{
		Use:   "attach <id>",
		Short: "Subscribe to a conversation's live run without sending a message",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			ctx, cancel := context.WithCancel(cmd.Context())
			defer cancel()
			if state.JSON {
				return stream.Run(ctx, state.Client, stream.Options{ConversationID: args[0]}, stream.JSONRenderer{})
			}
			if raw {
				return stream.Run(ctx, state.Client, stream.Options{ConversationID: args[0]}, stream.RawRenderer{})
			}
			return tui.Run(ctx, tui.Config{
				Client:         state.Client,
				ConversationID: args[0],
				Mode:           tui.ModeAttach,
				Theme:          themeForCfg(),
			})
		},
	}
	cmd.Flags().BoolVar(&raw, "raw", false, "Plain text output (no TUI)")
	return cmd
}

func newConvRenameCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "rename <id> <title>",
		Short: "Set the title of a conversation",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.UpdateConversation(cmd.Context(), args[0],
				map[string]any{"title": args[1]})
		},
	}
}

func newConvSetIDCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "set-id <old_id> <new_id>",
		Short: "Change a conversation's id (also resets title to the new id)",
		Long: `Rename a conversation's id. The new id must match
^[a-z0-9][a-z0-9_-]{0,127}$ — lowercase a-z, digits, '-', or '_', starting
with an alphanumeric character. The title is reset to the new id; use
'opa conv rename <new_id> <title>' afterward if you want a different title.

Cannot be run while the conversation has an active streaming run.`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.UpdateConversation(cmd.Context(), args[0],
				map[string]any{"id": args[1]})
		},
	}
}

func newConvCancelCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "cancel <run_id>",
		Short: "Cancel an in-flight agent run",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			cancelled, err := state.Client.CancelTask(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(map[string]bool{"cancelled": cancelled})
			}
			if cancelled {
				output.Println("cancelled")
			} else {
				output.Println("no active run for that id")
			}
			return nil
		},
	}
}

func newConvDeleteCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete <id>",
		Short: "Delete a single conversation",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.DeleteConversation(cmd.Context(), args[0])
		},
	}
}

func newConvDeleteAllCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete-all",
		Short: "Delete every conversation for the active profile",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			n, err := state.Client.DeleteAllConversations(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(map[string]int{"deleted_count": n})
			}
			output.Println(fmt.Sprintf("deleted %d conversation(s)", n))
			return nil
		},
	}
}

func themeForCfg() tui.Theme {
	if state.Cfg.NoColor {
		return tui.MonochromeTheme()
	}
	return tui.DefaultTheme()
}
