package cmd

import (
	"strconv"
	"time"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newMeCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "me",
		Short: "Show identity info for the current OPENPA_TOKEN",
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			ctx := cmd.Context()
			me, err := state.Client.Me(ctx)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(me)
			}
			output.PrintKV([][2]string{
				{"profile", me.Profile},
				{"subject", me.Subject},
				{"issued_at", formatUnix(me.IssuedAt)},
				{"expires_at", formatUnix(me.ExpiresAt)},
				{"working_dir", me.WorkingDir},
				{"user_working_dir", me.UserWorkingDir},
			})
			return nil
		},
	}
}

func formatUnix(ts int64) string {
	if ts == 0 {
		return ""
	}
	t := time.Unix(ts, 0)
	return t.Format(time.RFC3339) + " (" + strconv.FormatInt(ts, 10) + ")"
}
