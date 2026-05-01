package cmd

import (
	"context"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
	"openpa.local/cli/internal/proc"
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
		newProcStdinCmd(),
		newProcResizeCmd(),
		newProcAttachCmd(),
		newProcAutostartCmd(),
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

func newProcStdinCmd() *cobra.Command {
	var (
		text       string
		keys       []string
		lineEnding string
	)
	cmd := &cobra.Command{
		Use:   "stdin <pid>",
		Short: "Send input to a running process",
		Long: `Write data to a process's stdin (or PTY).

  --text "..."       Send a literal string (use --line-ending to append \n / \r\n)
  --keys Up,Enter    Send named keys (Enter, Tab, Up, Down, Esc, ...)

If neither --text nor --keys is provided, stdin is read from the CLI's stdin
and sent verbatim.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			body := map[string]any{}
			if lineEnding != "" {
				body["line_ending"] = lineEnding
			}
			switch {
			case len(keys) > 0:
				body["keys"] = keys
			case text != "":
				body["input_text"] = text
			default:
				raw, err := io.ReadAll(os.Stdin)
				if err != nil {
					return fmt.Errorf("read stdin: %w", err)
				}
				body["input_text"] = string(raw)
			}
			out, err := state.Client.SendProcessStdin(cmd.Context(), args[0], body)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(out)
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&text, "text", "", "Literal text to send")
	cmd.Flags().StringSliceVar(&keys, "keys", nil, "Named keys to send (e.g. Enter, Tab, Up)")
	cmd.Flags().StringVar(&lineEnding, "line-ending", "", "Line ending to append: none | lf | crlf")
	return cmd
}

func newProcResizeCmd() *cobra.Command {
	var cols, rows int
	cmd := &cobra.Command{
		Use:   "resize <pid>",
		Short: "Resize a process's PTY (cols × rows)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			if cols <= 0 || rows <= 0 {
				return fmt.Errorf("--cols and --rows are required and must be positive")
			}
			return state.Client.ResizeProcessPTY(cmd.Context(), args[0], cols, rows)
		},
	}
	cmd.Flags().IntVar(&cols, "cols", 0, "Terminal columns")
	cmd.Flags().IntVar(&rows, "rows", 0, "Terminal rows")
	return cmd
}

func newProcAttachCmd() *cobra.Command {
	var noResize bool
	cmd := &cobra.Command{
		Use:   "attach <pid>",
		Short: "Attach to a process's PTY interactively (raw mode)",
		Long: `Open a WebSocket to the process's PTY and pipe stdin/stdout
through the local terminal in raw mode. Ctrl-C is forwarded to the remote
process; use Ctrl-\ (or Ctrl-]) to detach without killing it.

Terminal resize is forwarded automatically. Pass --no-resize to opt out.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return proc.Attach(cmd.Context(), proc.AttachConfig{
				URL:      state.Client.ProcessWebSocketURL(args[0]),
				Token:    state.Client.Token(),
				Resize:   !noResize,
				DetachKey: 0x1c, // Ctrl-\
			})
		},
	}
	cmd.Flags().BoolVar(&noResize, "no-resize", false, "Don't forward terminal resize events")
	return cmd
}

func newProcAutostartCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "autostart",
		Short: "Manage autostart-process registrations",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "list",
			Short: "List autostart registrations",
			RunE: func(c *cobra.Command, _ []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				rows, err := state.Client.ListAutostartProcesses(c.Context())
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(rows)
				}
				t := output.NewTable(state.Output, "ID", "COMMAND", "WORKING_DIR", "PTY", "ERROR")
				for _, r := range rows {
					t.AddRow(
						stringField(r, "id"),
						stringField(r, "command"),
						stringField(r, "working_dir"),
						boolField(r, "is_pty", false),
						stringField(r, "error"),
					)
				}
				t.Render()
				return nil
			},
		},
		newProcAutostartAddCmd(),
		&cobra.Command{
			Use:   "delete <id>",
			Short: "Remove an autostart registration",
			Args:  cobra.ExactArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				return state.Client.DeleteAutostartProcess(c.Context(), args[0])
			},
		},
		&cobra.Command{
			Use:   "run <id>",
			Short: "Spawn the command from an autostart registration immediately",
			Args:  cobra.ExactArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				out, err := state.Client.RunAutostartProcess(c.Context(), args[0])
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(out)
				}
				output.Println(stringField(out, "process_id"))
				return nil
			},
		},
	)
	return cmd
}

func newProcAutostartAddCmd() *cobra.Command {
	var (
		pid   string
		force bool
	)
	cmd := &cobra.Command{
		Use:   "add",
		Short: "Register a live process as autostart so it relaunches at boot",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			if strings.TrimSpace(pid) == "" {
				return fmt.Errorf("--pid is required")
			}
			out, err := state.Client.CreateAutostartFromProcess(cmd.Context(), pid, force)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(out)
			}
			output.Println(stringField(out, "id"))
			return nil
		},
	}
	cmd.Flags().StringVar(&pid, "pid", "", "Process id to register")
	cmd.Flags().BoolVar(&force, "force", false, "Bypass the duplicate-command check")
	return cmd
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
