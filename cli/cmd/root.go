// Package cmd wires cobra commands for the `opa` CLI.
package cmd

import (
	"github.com/spf13/cobra"

	"openpa.local/cli/internal/client"
	"openpa.local/cli/internal/config"
	"openpa.local/cli/internal/output"
)

// State carries process-wide values populated by PersistentPreRun. Subcommands
// read from it after cobra has parsed flags.
type State struct {
	Cfg    *config.Config
	Client *client.Client
	Output output.Mode
	JSON   bool // value of --json
}

var state State

// jsonFlag is bound to --json on the root command.
var jsonFlag bool

// rootCmd is the entry point for `opa`.
var rootCmd = &cobra.Command{
	Use:           "opa",
	Short:         "OpenPA command-line interface",
	Long:          rootLong,
	SilenceUsage:  true,
	SilenceErrors: true,
	PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
		cfg, err := config.LoadFromEnv()
		if err != nil {
			return err
		}
		state.Cfg = cfg
		state.Client = client.New(cfg)
		state.Output = output.FromConfig(cfg, jsonFlag)
		state.JSON = jsonFlag
		return nil
	},
}

const rootLong = `opa is a lightweight command-line client for the OpenPA server.

It exposes the same APIs as the openpa-ui desktop app: tools & skills,
LLM providers, conversations (with streamed thinking), processes, skill
events, and user config.

ENVIRONMENT VARIABLES
  OPA_TOKEN     JWT bearer token for the OpenPA server (required). Obtain it
                from your OpenPA admin or from openpa-ui — the CLI cannot mint
                tokens. The active profile is resolved server-side from the
                token's claims, so no profile env var is needed.
  OPA_SERVER    Server base URL (default: http://localhost:8000).
  OPA_OUTPUT    'table' (default) or 'json'.
  OPA_NO_COLOR  When set, ANSI colors and table borders are disabled.
`

// Execute runs the CLI. It returns the first non-nil error from cobra so
// main can set the exit status.
func Execute() error {
	rootCmd.PersistentFlags().BoolVar(&jsonFlag, "json", false,
		"Output JSON instead of human-readable tables")

	register(
		newMeCmd(),
		newProfileCmd(),
		newToolsCmd(),
		newLLMCmd(),
		newConfigCmd(),
		newConvCmd(),
		newChatCmd(),
		newProcCmd(),
		newSkillEventsCmd(),
	)

	return rootCmd.Execute()
}

func register(cmds ...*cobra.Command) {
	for _, c := range cmds {
		rootCmd.AddCommand(c)
	}
}

// requireToken is a helper for command RunE funcs that need an authenticated
// client. It returns nil if OPA_TOKEN is set, else a friendly error.
func requireToken() error {
	return state.Cfg.RequireToken()
}
