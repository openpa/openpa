package cmd

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newProcCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "proc",
		Aliases: []string{"process"},
		Short:   "List and control long-running processes",
	}
	cmd.AddCommand(
		newProcListCmd(),
		newProcGetCmd(),
		newProcStopCmd(),
		newProcStreamCmd(),
	)
	return cmd
}

func newProcListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List running processes for the active profile",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			procs, err := state.Client.ListProcesses(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(procs)
			}
			t := output.NewTable(state.Output, "PID", "STATUS", "COMMAND", "WORKING_DIR")
			for _, p := range procs {
				t.AddRow(
					stringField(p, "id"),
					stringField(p, "status"),
					stringField(p, "command"),
					stringField(p, "working_dir"),
				)
			}
			t.Render()
			return nil
		},
	}
}

func newProcGetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "get <pid>",
		Short: "Show details for a single process",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			p, err := state.Client.GetProcess(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(p)
			}
			dumpJSON(p)
			return nil
		},
	}
}

func newProcStopCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "stop <pid>",
		Short: "Terminate a process",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.StopProcess(cmd.Context(), args[0])
		},
	}
}

func newProcStreamCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "stream",
		Short: "Stream live process snapshots (SSE) until interrupted",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			ctx, cancel := context.WithCancel(cmd.Context())
			defer cancel()
			events, errs := state.Client.Stream(ctx, state.Client.ProcessesStreamPath())
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
		},
	}
}
