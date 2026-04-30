package cmd

import (
	"fmt"
	"io"
	"os"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newProfileCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "profile",
		Short: "Manage OpenPA profiles, persona, and skill mode",
	}
	cmd.AddCommand(
		newProfileListCmd(),
		newProfileGetCmd(),
		newProfileCreateCmd(),
		newProfileDeleteCmd(),
		newProfilePersonaCmd(),
		newProfileSkillModeCmd(),
	)
	return cmd
}

func newProfileListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List profiles",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			profiles, err := state.Client.ListProfiles(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(profiles)
			}
			t := output.NewTable(state.Output, "PROFILE")
			for _, p := range profiles {
				t.AddRow(p)
			}
			t.Render()
			return nil
		},
	}
}

func newProfileGetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "get <name>",
		Short: "Show details for a profile (persona + skill mode)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			ctx := cmd.Context()
			persona, err := state.Client.GetPersona(ctx, args[0])
			if err != nil {
				return err
			}
			mode, err := state.Client.GetSkillMode(ctx, args[0])
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(map[string]any{
					"name":       args[0],
					"persona":    persona,
					"skill_mode": mode,
				})
			}
			output.PrintKV([][2]string{
				{"name", args[0]},
				{"skill_mode", mode},
			})
			fmt.Println()
			fmt.Println("--- persona ---")
			fmt.Println(persona)
			return nil
		},
	}
}

func newProfileCreateCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "create <name>",
		Short: "Create a new profile",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			if err := state.Client.CreateProfile(cmd.Context(), args[0]); err != nil {
				return err
			}
			output.Println(args[0])
			return nil
		},
	}
}

func newProfileDeleteCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete <name>",
		Short: "Delete a profile (cascades conversations, tools, skills)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.DeleteProfile(cmd.Context(), args[0])
		},
	}
}

func newProfilePersonaCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "persona",
		Short: "Manage a profile's persona text",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "get <name>",
			Short: "Print a profile's persona to stdout",
			Args:  cobra.ExactArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				p, err := state.Client.GetPersona(c.Context(), args[0])
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(map[string]string{"content": p})
				}
				fmt.Print(p)
				return nil
			},
		},
		&cobra.Command{
			Use:   "set <name>",
			Short: "Replace a profile's persona from stdin",
			Args:  cobra.ExactArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				body, err := io.ReadAll(os.Stdin)
				if err != nil {
					return fmt.Errorf("read persona from stdin: %w", err)
				}
				return state.Client.SetPersona(c.Context(), args[0], string(body))
			},
		},
	)
	return cmd
}

func newProfileSkillModeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "skill-mode",
		Short: "Get or set a profile's skill mode (manual/automatic)",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "get <name>",
			Short: "Show the current skill mode",
			Args:  cobra.ExactArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				m, err := state.Client.GetSkillMode(c.Context(), args[0])
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(map[string]string{"mode": m})
				}
				output.Println(m)
				return nil
			},
		},
		&cobra.Command{
			Use:   "set <name> <mode>",
			Short: "Set skill mode to 'manual' or 'automatic'",
			Args:  cobra.ExactArgs(2),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				return state.Client.SetSkillMode(c.Context(), args[0], args[1])
			},
		},
	)
	return cmd
}
