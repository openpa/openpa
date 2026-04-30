package cmd

import (
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newConfigCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "config",
		Short: "Read and write user_config (per-profile general settings)",
	}
	cmd.AddCommand(
		newConfigSchemaCmd(),
		newConfigGetCmd(),
		newConfigSetCmd(),
		newConfigResetCmd(),
	)
	return cmd
}

func newConfigSchemaCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "schema",
		Short: "Print the user_config schema (groups, fields, types, defaults)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			schema, err := state.Client.GetConfigSchema(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(schema)
			}
			groups, _ := schema["groups"].(map[string]any)
			groupNames := make([]string, 0, len(groups))
			for name := range groups {
				groupNames = append(groupNames, name)
			}
			sort.Strings(groupNames)
			for _, gn := range groupNames {
				group, _ := groups[gn].(map[string]any)
				fmt.Printf("[%s] %s\n", gn, stringField(group, "label"))
				if d := stringField(group, "description"); d != "" {
					fmt.Println("  " + d)
				}
				fields, _ := group["fields"].(map[string]any)
				fieldNames := make([]string, 0, len(fields))
				for fn := range fields {
					fieldNames = append(fieldNames, fn)
				}
				sort.Strings(fieldNames)
				for _, fn := range fieldNames {
					f, _ := fields[fn].(map[string]any)
					fmt.Printf("  %s.%s  type=%s default=%v\n",
						gn, fn, stringField(f, "type"), f["default"])
				}
				fmt.Println()
			}
			return nil
		},
	}
}

func newConfigGetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "get [<group.key>]",
		Short: "Show all config values, or a single key when given",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			cfg, err := state.Client.GetUserConfig(cmd.Context())
			if err != nil {
				return err
			}
			values, _ := cfg["values"].(map[string]any)
			defaults, _ := cfg["defaults"].(map[string]any)
			if len(args) == 1 {
				key := args[0]
				v := values[key]
				if v == nil {
					v = defaults[key]
				}
				if state.Output.JSON {
					return output.PrintJSON(map[string]any{key: v})
				}
				output.Println(fmt.Sprintf("%v", v))
				return nil
			}
			if state.Output.JSON {
				return output.PrintJSON(cfg)
			}
			t := output.NewTable(state.Output, "KEY", "VALUE", "DEFAULT")
			keys := make([]string, 0, len(defaults))
			for k := range defaults {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				v := values[k]
				display := ""
				if v != nil {
					display = fmt.Sprintf("%v", v)
				}
				t.AddRow(k, display, fmt.Sprintf("%v", defaults[k]))
			}
			t.Render()
			return nil
		},
	}
}

func newConfigSetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "set <group.key> <value>",
		Short: "Set a config key for the active profile",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			values := map[string]any{args[0]: coerceConfigValue(args[1])}
			return state.Client.UpdateUserConfig(cmd.Context(), values)
		},
	}
}

func newConfigResetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "reset <group.key>",
		Short: "Revert a config key to its declared default",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.ResetUserConfigKey(cmd.Context(), args[0])
		},
	}
}

// coerceConfigValue takes a CLI string and converts it to the closest sensible
// JSON type. Bool literals and numeric literals are converted; everything else
// is left as a string (the server validates against the declared schema).
func coerceConfigValue(s string) any {
	switch strings.ToLower(s) {
	case "true":
		return true
	case "false":
		return false
	}
	if n, err := strconv.ParseInt(s, 10, 64); err == nil {
		return n
	}
	if f, err := strconv.ParseFloat(s, 64); err == nil {
		return f
	}
	return s
}
