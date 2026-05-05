package cmd

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newFileWatchersCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "file-watchers",
		Aliases: []string{"file-watcher", "fw"},
		Short:   "Manage filesystem watch subscriptions",
	}
	cmd.AddCommand(
		newFileWatchersListCmd(),
		newFileWatchersDeleteCmd(),
		newFileWatchersRegisterCmd(),
		newFileWatchersStreamCmd(),
	)
	return cmd
}

func newFileWatchersListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List file watcher subscriptions for the active profile",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			subs, err := state.Client.ListFileWatchers(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(subs)
			}
			t := output.NewTable(
				state.Output,
				"ID", "NAME", "PATH", "TRIGGERS", "TARGET", "EXTENSIONS", "ARMED", "CONV_TITLE",
			)
			for _, s := range subs {
				t.AddRow(
					stringField(s, "id"),
					stringField(s, "name"),
					stringField(s, "root_path"),
					stringField(s, "event_types"),
					stringField(s, "target_kind"),
					stringField(s, "extensions"),
					boolField(s, "armed", false),
					stringField(s, "conversation_title"),
				)
			}
			t.Render()
			return nil
		},
	}
}

func newFileWatchersDeleteCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete <id>",
		Short: "Delete a file watcher subscription",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.DeleteFileWatcher(cmd.Context(), args[0])
		},
	}
}

func newFileWatchersRegisterCmd() *cobra.Command {
	var (
		path           string
		name           string
		triggersCSV    string
		targetKind     string
		extensionsCSV  string
		recursive      bool
		recursiveSet   bool
		action         string
		conversationID string
	)
	cmd := &cobra.Command{
		Use:   "register",
		Short: "Register a new file watcher subscription",
		Long: `Register a new filesystem watch.

Relative --path is resolved against the user's working directory
(OPENPA_USER_WORKING_DIR); absolute paths are used as-is. --triggers,
--target, and --ext narrow which events fire the watcher.

Examples:
  opa file-watchers register \
    --path Lee --triggers modified,created --target file \
    --ext .py --action "notify the user about the change"

  opa file-watchers register \
    --path C:\\Users\\me\\inbox --triggers created --target file \
    --ext .pdf --action "summarize the new pdf"`,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			if action == "" {
				return fmt.Errorf("--action is required")
			}
			body := map[string]any{"action": action}
			if path != "" {
				body["path"] = path
			}
			if name != "" {
				body["name"] = name
			}
			if triggersCSV != "" {
				body["triggers"] = splitCSV(triggersCSV)
			}
			if targetKind != "" {
				body["target_kind"] = targetKind
			}
			if extensionsCSV != "" {
				body["extensions"] = splitCSV(extensionsCSV)
			}
			if recursiveSet {
				body["recursive"] = recursive
			}
			if conversationID != "" {
				body["conversation_id"] = conversationID
			}
			out, err := state.Client.CreateFileWatcher(cmd.Context(), body)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(out)
			}
			output.PrintKV([][2]string{
				{"id", stringField(out, "id")},
				{"name", stringField(out, "name")},
				{"root_path", stringField(out, "root_path")},
				{"event_types", stringField(out, "event_types")},
				{"target_kind", stringField(out, "target_kind")},
				{"extensions", stringField(out, "extensions")},
				{"recursive", boolField(out, "recursive", true)},
				{"armed", boolField(out, "armed", false)},
				{"conversation_id", stringField(out, "conversation_id")},
			})
			return nil
		},
	}
	cmd.Flags().StringVar(&path, "path", "", "Directory to watch (relative paths join user working dir)")
	cmd.Flags().StringVar(&name, "name", "", "Optional display name (auto-generated if blank)")
	cmd.Flags().StringVar(&triggersCSV, "triggers", "", "Comma-separated subset of created,modified,deleted,moved (default: all)")
	cmd.Flags().StringVar(&targetKind, "target", "", "file | folder | any (default any)")
	cmd.Flags().StringVar(&extensionsCSV, "ext", "", "Comma-separated extensions e.g. .py,.md (file events only; empty = all)")
	cmd.Flags().BoolVar(&recursive, "recursive", true, "Watch subdirectories recursively")
	cmd.Flags().StringVar(&action, "action", "", "Natural-language instruction the assistant runs on each event (required)")
	cmd.Flags().StringVar(&conversationID, "conversation", "", "Existing conversation id to bind to (a new one is created if blank)")
	cmd.PreRun = func(c *cobra.Command, _ []string) {
		recursiveSet = c.Flags().Changed("recursive")
	}
	return cmd
}

func newFileWatchersStreamCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "stream",
		Short: "Stream the file-watchers admin snapshot (SSE)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return runStream(cmd.Context(), state.Client.FileWatchersAdminStreamPath())
		},
	}
}

func splitCSV(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}
