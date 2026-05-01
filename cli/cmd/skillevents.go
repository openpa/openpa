package cmd

import (
	"context"
	"fmt"
	"io"
	"os"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newSkillEventsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "skill-events",
		Aliases: []string{"events"},
		Short:   "Manage skill event subscriptions and tail notifications",
	}
	cmd.AddCommand(
		newSkillEventsListCmd(),
		newSkillEventsDeleteCmd(),
		newSkillEventsSimulateCmd(),
		newSkillEventsStreamCmd(),
		newSkillEventsNotificationsCmd(),
		newSkillEventsEventsCmd(),
		newSkillEventsListenerStatusCmd(),
		newSkillEventsListenerStartCmd(),
	)
	return cmd
}

func newSkillEventsListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List skill event subscriptions for the active profile",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			subs, err := state.Client.ListSkillEventSubscriptions(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(subs)
			}
			t := output.NewTable(state.Output, "ID", "SKILL", "EVENT_TYPE", "CONVERSATION", "CONV_TITLE")
			for _, s := range subs {
				t.AddRow(
					stringField(s, "id"),
					stringField(s, "skill_name"),
					stringField(s, "event_type"),
					stringField(s, "conversation_id"),
					stringField(s, "conversation_title"),
				)
			}
			t.Render()
			return nil
		},
	}
}

func newSkillEventsDeleteCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete <id>",
		Short: "Delete a skill event subscription",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.DeleteSkillEventSubscription(cmd.Context(), args[0])
		},
	}
}

func newSkillEventsSimulateCmd() *cobra.Command {
	var filename string
	cmd := &cobra.Command{
		Use:   "simulate <id>",
		Short: "Drop a markdown file into the watched events folder (dev tool)",
		Long: `Simulate a skill event by writing a markdown file under the
subscription's watched events directory. Reads the file content from stdin.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			body, err := io.ReadAll(os.Stdin)
			if err != nil {
				return fmt.Errorf("read content from stdin: %w", err)
			}
			return state.Client.SimulateSkillEvent(cmd.Context(), args[0], string(body), filename)
		},
	}
	cmd.Flags().StringVar(&filename, "filename", "", "Optional filename (defaults to a unique simulate-*.md)")
	return cmd
}

func newSkillEventsStreamCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "stream",
		Short: "Stream the skill-events admin snapshot (SSE)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return runStream(cmd.Context(), state.Client.SkillEventsAdminStreamPath())
		},
	}
}

func newSkillEventsNotificationsCmd() *cobra.Command {
	var since int64
	cmd := &cobra.Command{
		Use:   "notifications",
		Short: "Tail per-profile skill-event notifications (SSE)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return runStream(cmd.Context(), state.Client.SkillEventNotificationsStreamPath(since))
		},
	}
	cmd.Flags().Int64Var(&since, "since", 0, "Resume cursor (millis since epoch)")
	return cmd
}

func newSkillEventsEventsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "events <skill>",
		Short: "List the events declared by a skill (from its SKILL.md)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			out, err := state.Client.ListSkillEvents(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(out)
			}
			dumpJSON(out)
			return nil
		},
	}
}

func newSkillEventsListenerStatusCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "listener-status <skill>",
		Short: "Show the listener daemon's heartbeat-derived status for a skill",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			out, err := state.Client.GetListenerStatus(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(out)
			}
			output.PrintKV([][2]string{
				{"skill_name", stringField(out, "skill_name")},
				{"running", boolField(out, "running", false)},
				{"last_heartbeat", fmt.Sprintf("%v", out["last_heartbeat"])},
				{"autostart_id", fmt.Sprintf("%v", out["autostart_id"])},
				{"command", stringField(out, "command")},
			})
			return nil
		},
	}
}

func newSkillEventsListenerStartCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "listener-start <skill>",
		Short: "Start (or resume) a skill's listener daemon as an autostart process",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			out, err := state.Client.StartListener(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(out)
			}
			output.PrintKV([][2]string{
				{"process_id", stringField(out, "process_id")},
				{"autostart_id", fmt.Sprintf("%v", out["autostart_id"])},
			})
			return nil
		},
	}
}

func runStream(parent context.Context, path string) error {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	events, errs := state.Client.Stream(ctx, path)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err, open := <-errs:
			if !open {
				errs = nil
				continue
			}
			if err != nil {
				return err
			}
		case ev, open := <-events:
			if !open {
				return nil
			}
			if state.Output.JSON {
				fmt.Println(string(ev.Raw))
				continue
			}
			fmt.Printf("[%s] %s\n", ev.Type, string(ev.Raw))
		}
	}
}
