package cmd

import (
	"context"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/tui"
)

func newChatCmd() *cobra.Command {
	var title string
	cmd := &cobra.Command{
		Use:   "chat [<conversation_id>]",
		Short: "Open an interactive chat REPL with streamed thinking",
		Long: `Open a TUI chat session. With no argument, creates a new conversation.
With a conversation id, resumes that conversation.

Keys:
  Enter       send message
  Ctrl+C      cancel current run (or quit if idle)
  Ctrl+D      quit
  PgUp/PgDn   scroll history`,
		Args: cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			ctx, cancel := context.WithCancel(cmd.Context())
			defer cancel()

			var convID, displayTitle string
			if len(args) == 1 {
				convID = args[0]
				conv, err := state.Client.GetConversation(ctx, convID)
				if err == nil {
					if c, ok := conv["conversation"].(map[string]any); ok {
						displayTitle = stringField(c, "title")
					}
				}
			} else {
				conv, err := state.Client.CreateConversation(ctx, title)
				if err != nil {
					return err
				}
				convID = stringField(conv, "id")
				displayTitle = stringField(conv, "title")
			}

			return tui.Run(ctx, tui.Config{
				Client:         state.Client,
				ConversationID: convID,
				Title:          displayTitle,
				Mode:           tui.ModeInteractive,
				Reasoning:      true,
				Theme:          themeForCfg(),
			})
		},
	}
	cmd.Flags().StringVarP(&title, "title", "t", "", "Title to use when creating a new conversation")
	return cmd
}
