package cmd

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newAgentsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "agents",
		Aliases: []string{"agent"},
		Short:   "Manage A2A and MCP server registrations",
	}
	cmd.AddCommand(
		newAgentsListCmd(),
		newAgentsAddCmd(),
		newAgentsDeleteCmd(),
		newAgentsEnableCmd(true),
		newAgentsEnableCmd(false),
		newAgentsReconnectCmd(),
		newAgentsAuthURLCmd(),
		newAgentsUnlinkCmd(),
		newAgentsConfigCmd(),
	)
	return cmd
}

func newAgentsListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List registered A2A and MCP tools with auth + enabled status",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			agents, err := state.Client.ListAgents(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(agents)
			}
			t := output.NewTable(state.Output, "TOOL_ID", "TYPE", "ENABLED", "STATUS", "URL")
			for _, a := range agents {
				t.AddRow(
					stringField(a, "tool_id"),
					stringField(a, "agent_type"),
					boolField(a, "enabled", false),
					stringField(a, "status_text"),
					stringField(a, "url"),
				)
			}
			t.Render()
			return nil
		},
	}
}

func newAgentsAddCmd() *cobra.Command {
	var (
		agentType, agentURL, jsonConfig string
		systemPrompt, description       string
		llmProvider, llmModel, effort   string
	)
	cmd := &cobra.Command{
		Use:   "add",
		Short: "Register a new A2A or MCP server",
		Long: `Register a new A2A or MCP server.

  --type a2a --url <url>                       Add an A2A agent
  --type mcp --url <url>                       Add an HTTP/SSE MCP server
  --type mcp --json-config '{<vscode-json>}'   Add an MCP server (stdio or http) from JSON

For MCP servers, additional flags configure the wrapping LLM:
  --system-prompt   Server-specific system prompt
  --description     Display description
  --llm-provider, --llm-model, --reasoning-effort  Per-server LLM overrides`,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			if agentType == "" {
				return fmt.Errorf("--type is required (a2a or mcp)")
			}
			if agentURL == "" && jsonConfig == "" {
				return fmt.Errorf("either --url or --json-config is required")
			}
			body := map[string]any{"type": agentType}
			if agentURL != "" {
				body["url"] = agentURL
			}
			if jsonConfig != "" {
				body["json_config"] = jsonConfig
			}
			if systemPrompt != "" {
				body["system_prompt"] = systemPrompt
			}
			if description != "" {
				body["description"] = description
			}
			if llmProvider != "" {
				body["llm_provider"] = llmProvider
			}
			if llmModel != "" {
				body["llm_model"] = llmModel
			}
			if effort != "" {
				body["reasoning_effort"] = effort
			}
			agent, err := state.Client.AddAgent(cmd.Context(), body)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(agent)
			}
			output.PrintKV([][2]string{
				{"tool_id", stringField(agent, "tool_id")},
				{"name", stringField(agent, "name")},
				{"agent_type", stringField(agent, "agent_type")},
				{"url", stringField(agent, "url")},
				{"status_text", stringField(agent, "status_text")},
			})
			return nil
		},
	}
	cmd.Flags().StringVar(&agentType, "type", "", "a2a | mcp")
	cmd.Flags().StringVar(&agentURL, "url", "", "Agent URL")
	cmd.Flags().StringVar(&jsonConfig, "json-config", "",
		"VS Code-style MCP server JSON (for --type mcp)")
	cmd.Flags().StringVar(&systemPrompt, "system-prompt", "", "MCP system prompt")
	cmd.Flags().StringVar(&description, "description", "", "MCP description")
	cmd.Flags().StringVar(&llmProvider, "llm-provider", "", "MCP LLM provider override")
	cmd.Flags().StringVar(&llmModel, "llm-model", "", "MCP LLM model override")
	cmd.Flags().StringVar(&effort, "reasoning-effort", "", "MCP reasoning effort")
	return cmd
}

func newAgentsDeleteCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete <tool_id>",
		Short: "Unregister an A2A or MCP tool",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.RemoveAgent(cmd.Context(), args[0])
		},
	}
}

func newAgentsEnableCmd(enable bool) *cobra.Command {
	use := "enable"
	short := "Enable an agent for the active profile"
	if !enable {
		use = "disable"
		short = "Disable an agent for the active profile"
	}
	return &cobra.Command{
		Use:   use + " <tool_id>",
		Short: short,
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.SetAgentEnabled(cmd.Context(), args[0], enable)
		},
	}
}

func newAgentsReconnectCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "reconnect <tool_id>",
		Short: "Retry a stub agent's connection",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.ReconnectAgent(cmd.Context(), args[0])
		},
	}
}

func newAgentsAuthURLCmd() *cobra.Command {
	var returnURL string
	cmd := &cobra.Command{
		Use:   "auth-url <tool_id>",
		Short: "Print the OAuth authorize URL for an agent",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			authURL, err := state.Client.GetAgentAuthURL(cmd.Context(), args[0], returnURL)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(map[string]string{"auth_url": authURL})
			}
			output.Println(authURL)
			return nil
		},
	}
	cmd.Flags().StringVar(&returnURL, "return-url", "",
		"Optional URL to redirect to after the OAuth callback completes")
	return cmd
}

func newAgentsUnlinkCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "unlink <tool_id>",
		Short: "Drop the active profile's OAuth token for an agent",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.UnlinkAgent(cmd.Context(), args[0])
		},
	}
}

func newAgentsConfigCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "config",
		Short: "Read or update an MCP / built-in agent's per-profile config",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "get <tool_id>",
			Short: "Show the agent's LLM + meta config",
			Args:  cobra.ExactArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				cfg, err := state.Client.GetAgentConfig(c.Context(), args[0])
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(cfg)
				}
				dumpJSON(cfg)
				return nil
			},
		},
		newAgentsConfigSetCmd(),
	)
	return cmd
}

func newAgentsConfigSetCmd() *cobra.Command {
	var (
		llmProvider, llmModel, effort string
		fullReasoning                 string
		systemPrompt, description     string
	)
	cmd := &cobra.Command{
		Use:   "set <tool_id>",
		Short: "Patch an agent's LLM and meta config (only specified flags change)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			body := map[string]any{}
			if llmProvider != "" {
				body["llm_provider"] = llmProvider
			}
			if llmModel != "" {
				body["llm_model"] = llmModel
			}
			if effort != "" {
				body["reasoning_effort"] = effort
			}
			switch strings.ToLower(fullReasoning) {
			case "":
			case "true":
				body["full_reasoning"] = true
			case "false":
				body["full_reasoning"] = false
			default:
				return fmt.Errorf("--full-reasoning must be 'true' or 'false'")
			}
			if systemPrompt != "" {
				body["system_prompt"] = systemPrompt
			}
			if description != "" {
				body["description"] = description
			}
			if len(body) == 0 {
				return fmt.Errorf("at least one config flag is required")
			}
			return state.Client.UpdateAgentConfig(cmd.Context(), args[0], body)
		},
	}
	cmd.Flags().StringVar(&llmProvider, "llm-provider", "", "LLM provider")
	cmd.Flags().StringVar(&llmModel, "llm-model", "", "LLM model")
	cmd.Flags().StringVar(&effort, "reasoning-effort", "", "low | medium | high")
	cmd.Flags().StringVar(&fullReasoning, "full-reasoning", "", "true | false")
	cmd.Flags().StringVar(&systemPrompt, "system-prompt", "", "System prompt")
	cmd.Flags().StringVar(&description, "description", "", "Description")
	return cmd
}
