package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newSetupCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "setup",
		Short: "First-run setup wizard and server configuration",
		Long: `First-run / server configuration commands.

  opa setup status                Check whether setup is complete
  opa setup complete              POST a setup payload (no auth, returns a token)
  opa setup reset-orphaned        Clear setup_complete when no profiles exist
  opa setup reconfigure           Reset setup status (admin auth required)
  opa setup server-config get     Read non-secret server settings
  opa setup server-config set ... Update server settings`,
	}
	cmd.AddCommand(
		newSetupStatusCmd(),
		newSetupCompleteCmd(),
		newSetupResetOrphanedCmd(),
		newSetupReconfigureCmd(),
		newSetupServerConfigCmd(),
	)
	return cmd
}

func newSetupStatusCmd() *cobra.Command {
	var profile string
	cmd := &cobra.Command{
		Use:   "status",
		Short: "Show setup completion status (unauthenticated)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			out, err := state.Client.GetSetupStatus(cmd.Context(), profile)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(out)
			}
			rows := [][2]string{
				{"setup_complete", boolField(out, "setup_complete", false)},
			}
			if _, ok := out["profile_exists"]; ok {
				rows = append(rows, [2]string{"profile_exists", boolField(out, "profile_exists", false)})
			}
			if _, ok := out["has_profiles"]; ok {
				rows = append(rows, [2]string{"has_profiles", boolField(out, "has_profiles", false)})
			}
			output.PrintKV(rows)
			return nil
		},
	}
	cmd.Flags().StringVar(&profile, "profile", "", "Also check whether this profile exists")
	return cmd
}

func newSetupCompleteCmd() *cobra.Command {
	var (
		profile, jsonBody, jsonFile string
	)
	cmd := &cobra.Command{
		Use:   "complete",
		Short: "POST a setup payload; prints the resulting JWT (unauthenticated)",
		Long: `Complete the first-run setup or onboard a new profile.

The JSON payload mirrors the openpa-ui wizard:

  {
    "profile": "admin",
    "server_config": { "jwt_secret": "...", "user_working_dir": "..." },
    "llm_config":    { "anthropic.api_key": "sk-...", "auth_method": "anthropic" },
    "tool_configs":  { "<tool_id>": { "_enabled": "true", "VAR_NAME": "value" } },
    "agent_configs": { "<tool_id>": { "llm_provider": "anthropic", "llm_model": "claude-..." } }
  }

Pass it via --json '<inline>', --json-file <path>, or stdin. --profile
optionally overrides the "profile" field. The first profile must be 'admin'.

The server returns a JWT token; export it as OPENPA_TOKEN to use the rest of
the CLI.`,
		RunE: func(cmd *cobra.Command, _ []string) error {
			body, err := readSetupPayload(jsonBody, jsonFile)
			if err != nil {
				return err
			}
			if profile != "" {
				body["profile"] = profile
			}
			if _, ok := body["profile"]; !ok {
				return fmt.Errorf("a 'profile' field is required (use --profile or include it in the JSON)")
			}
			resp, err := state.Client.CompleteSetup(cmd.Context(), body)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(resp)
			}
			output.PrintKV([][2]string{
				{"profile", resp.Profile},
				{"expires_at", resp.ExpiresAt},
				{"token", resp.Token},
			})
			fmt.Fprintln(os.Stderr, "")
			fmt.Fprintln(os.Stderr, "Export the token to use the CLI:")
			fmt.Fprintln(os.Stderr, "  export OPENPA_TOKEN="+resp.Token)
			return nil
		},
	}
	cmd.Flags().StringVar(&profile, "profile", "", "Profile name (overrides the JSON field)")
	cmd.Flags().StringVar(&jsonBody, "json", "", "Setup payload as a JSON object")
	cmd.Flags().StringVar(&jsonFile, "json-file", "", "Path to a JSON file containing the setup payload (use - for stdin)")
	return cmd
}

func readSetupPayload(inline, file string) (map[string]any, error) {
	var raw []byte
	switch {
	case inline != "" && file != "":
		return nil, fmt.Errorf("--json and --json-file are mutually exclusive")
	case inline != "":
		raw = []byte(inline)
	case file == "" || file == "-":
		var err error
		raw, err = io.ReadAll(os.Stdin)
		if err != nil {
			return nil, fmt.Errorf("read JSON from stdin: %w", err)
		}
	default:
		var err error
		raw, err = os.ReadFile(file)
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", file, err)
		}
	}
	body := map[string]any{}
	if len(raw) == 0 {
		return body, nil
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, fmt.Errorf("parse setup JSON: %w", err)
	}
	return body, nil
}

func newSetupResetOrphanedCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "reset-orphaned",
		Short: "Clear setup_complete when no profiles exist (unauthenticated)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			return state.Client.ResetOrphanedSetup(cmd.Context())
		},
	}
}

func newSetupReconfigureCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "reconfigure",
		Short: "Reset setup_complete so the wizard can run again (admin auth)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.Reconfigure(cmd.Context())
		},
	}
}

func newSetupServerConfigCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "server-config",
		Aliases: []string{"server"},
		Short:   "Read or write server-wide configuration (admin auth)",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "get [<key>]",
			Short: "Show all server config values, or a single key when given",
			Args:  cobra.MaximumNArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				cfg, err := state.Client.GetServerConfig(c.Context())
				if err != nil {
					return err
				}
				if len(args) == 1 {
					v := cfg[args[0]]
					if state.Output.JSON {
						return output.PrintJSON(map[string]any{args[0]: v})
					}
					output.Println(fmt.Sprintf("%v", v))
					return nil
				}
				if state.Output.JSON {
					return output.PrintJSON(cfg)
				}
				output.PrintMap(cfg)
				return nil
			},
		},
		&cobra.Command{
			Use:   "set KEY=VALUE [KEY=VALUE...]",
			Short: "Write one or more server config keys",
			Args:  cobra.MinimumNArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				values := map[string]any{}
				for _, kv := range args {
					k, v, ok := strings.Cut(kv, "=")
					if !ok {
						return fmt.Errorf("expected KEY=VALUE, got %q", kv)
					}
					values[k] = v
				}
				return state.Client.UpdateServerConfig(c.Context(), values)
			},
		},
	)
	return cmd
}
