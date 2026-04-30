package cmd

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newToolsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "tools",
		Aliases: []string{"tool"},
		Short:   "List and configure tools & skills",
	}
	cmd.AddCommand(
		newToolsListCmd(),
		newToolsGetCmd(),
		newToolsEnableCmd(true),
		newToolsEnableCmd(false),
		newToolsSetVarCmd(),
		newToolsSetArgsCmd(),
		newToolsSetLLMCmd(),
		newToolsResetLLMCmd(),
	)
	return cmd
}

func newToolsListCmd() *cobra.Command {
	var typeFilter string
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all tools (built-in, mcp, a2a, skill, intrinsic)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			tools, err := state.Client.ListTools(cmd.Context(), typeFilter)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(tools)
			}
			t := output.NewTable(state.Output, "TOOL_ID", "TYPE", "ENABLED", "CONFIGURED", "NAME")
			for _, row := range tools {
				t.AddRow(
					stringField(row, "tool_id"),
					stringField(row, "tool_type"),
					boolField(row, "enabled", true),
					boolField(row, "configured", false),
					stringField(row, "name"),
				)
			}
			t.Render()
			return nil
		},
	}
	cmd.Flags().StringVar(&typeFilter, "type", "",
		"Filter by tool_type: built-in, mcp, a2a, skill, intrinsic")
	return cmd
}

func newToolsGetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "get <tool_id>",
		Short: "Show detailed configuration for a tool",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			tool, err := state.Client.GetTool(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(tool)
			}
			renderToolDetail(tool)
			return nil
		},
	}
}

func renderToolDetail(t map[string]any) {
	output.PrintKV([][2]string{
		{"tool_id", stringField(t, "tool_id")},
		{"name", stringField(t, "name")},
		{"tool_type", stringField(t, "tool_type")},
		{"description", stringField(t, "description")},
		{"configured", boolField(t, "configured", false)},
	})
	if cfg, ok := t["config"].(map[string]any); ok {
		fmt.Println()
		fmt.Println("--- config ---")
		dumpJSON(cfg)
	}
	if locked, ok := t["locked_llm_fields"].([]any); ok && len(locked) > 0 {
		fmt.Println()
		fmt.Print("locked_llm_fields: ")
		parts := make([]string, 0, len(locked))
		for _, v := range locked {
			parts = append(parts, fmt.Sprintf("%v", v))
		}
		fmt.Println(strings.Join(parts, ", "))
	}
}

func newToolsEnableCmd(enable bool) *cobra.Command {
	use := "enable"
	short := "Enable an A2A or MCP tool"
	if !enable {
		use = "disable"
		short = "Disable an A2A or MCP tool"
	}
	return &cobra.Command{
		Use:   use + " <tool_id>",
		Short: short,
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.SetToolEnabled(cmd.Context(), args[0], enable)
		},
	}
}

func newToolsSetVarCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "set-var <tool_id> KEY=VALUE [KEY=VALUE...]",
		Short: "Set Tool Variables (env-style key/value pairs)",
		Args:  cobra.MinimumNArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			vars := map[string]string{}
			for _, kv := range args[1:] {
				k, v, ok := strings.Cut(kv, "=")
				if !ok {
					return fmt.Errorf("expected KEY=VALUE, got %q", kv)
				}
				vars[k] = v
			}
			return state.Client.SetToolVariables(cmd.Context(), args[0], vars)
		},
	}
}

func newToolsSetArgsCmd() *cobra.Command {
	var jsonBody string
	cmd := &cobra.Command{
		Use:   "set-args <tool_id> --json '<JSON object>'",
		Short: "Set Tool Arguments from a JSON object",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			if jsonBody == "" {
				return fmt.Errorf("--json is required")
			}
			var values map[string]any
			if err := json.Unmarshal([]byte(jsonBody), &values); err != nil {
				return fmt.Errorf("invalid --json: %w", err)
			}
			return state.Client.SetToolArguments(cmd.Context(), args[0], values)
		},
	}
	cmd.Flags().StringVar(&jsonBody, "json", "", "Tool arguments as a JSON object")
	return cmd
}

func newToolsSetLLMCmd() *cobra.Command {
	var (
		provider, model, reasoning string
		fullReasoning              string
	)
	cmd := &cobra.Command{
		Use:   "set-llm <tool_id>",
		Short: "Set LLM Parameters for a tool (partial — omitted flags unchanged)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			params := map[string]any{}
			if provider != "" {
				params["llm_provider"] = provider
			}
			if model != "" {
				params["llm_model"] = model
			}
			if reasoning != "" {
				params["reasoning_effort"] = reasoning
			}
			switch strings.ToLower(fullReasoning) {
			case "":
				// untouched
			case "true":
				params["full_reasoning"] = true
			case "false":
				params["full_reasoning"] = false
			default:
				return fmt.Errorf("--full-reasoning must be 'true' or 'false'")
			}
			if len(params) == 0 {
				return fmt.Errorf("at least one of --provider, --model, --reasoning-effort, --full-reasoning is required")
			}
			return state.Client.SetToolLLMParams(cmd.Context(), args[0], params)
		},
	}
	cmd.Flags().StringVar(&provider, "provider", "", "LLM provider (e.g. anthropic, openai)")
	cmd.Flags().StringVar(&model, "model", "", "Model name")
	cmd.Flags().StringVar(&reasoning, "reasoning-effort", "", "low | medium | high")
	cmd.Flags().StringVar(&fullReasoning, "full-reasoning", "", "true | false")
	return cmd
}

func newToolsResetLLMCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "reset-llm <tool_id> <key> [key...]",
		Short: "Delete LLM-parameter overrides so code defaults apply",
		Args:  cobra.MinimumNArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.ResetToolLLMParams(cmd.Context(), args[0], args[1:])
		},
	}
}

func stringField(m map[string]any, k string) string {
	if v, ok := m[k]; ok {
		if s, ok := v.(string); ok {
			return s
		}
		return fmt.Sprintf("%v", v)
	}
	return ""
}

func boolField(m map[string]any, k string, fallback bool) string {
	if v, ok := m[k]; ok {
		if b, ok := v.(bool); ok {
			if b {
				return "yes"
			}
			return "no"
		}
	}
	if fallback {
		return "yes"
	}
	return "no"
}

func dumpJSON(v any) {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fmt.Println(v)
		return
	}
	fmt.Println(string(b))
}
