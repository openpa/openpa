package cmd

import (
	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

type systemVar struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Value       string `json:"value"`
}

func newSystemVarsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "system-vars",
		Short: "List the env vars OpenPA injects into exec_shell subprocesses",
		Long: `List the system variables registered on the OpenPA server.

These are the env vars (e.g. OPENPA_SERVER, OPENPA_TOKEN, OPENPA_SKILL_DIR)
that the server injects into every exec_shell subprocess. The values are
resolved server-side for the caller's profile (from the JWT), so the
output reflects exactly what an exec_shell run would see.`,
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			ctx := cmd.Context()
			var vars []systemVar
			if err := state.Client.GetJSON(ctx, "/api/system-vars", &vars); err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(vars)
			}
			t := output.NewTable(state.Output, "NAME", "VALUE", "DESCRIPTION")
			for _, v := range vars {
				t.AddRow(v.Name, v.Value, v.Description)
			}
			t.Render()
			return nil
		},
	}
}
