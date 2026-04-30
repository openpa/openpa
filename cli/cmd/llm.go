package cmd

import (
	"fmt"
	"time"

	"github.com/spf13/cobra"

	"openpa.local/cli/internal/output"
)

func newLLMCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "llm",
		Short: "Manage LLM providers, models, and model groups",
	}
	cmd.AddCommand(
		newLLMProvidersCmd(),
		newLLMModelGroupsCmd(),
		newLLMDeviceCodeCmd(),
	)
	return cmd
}

func newLLMProvidersCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "providers",
		Short: "List, configure, and inspect LLM providers",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "list",
			Short: "List configured and available providers",
			RunE: func(c *cobra.Command, _ []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				providers, err := state.Client.ListLLMProviders(c.Context())
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(providers)
				}
				t := output.NewTable(state.Output, "NAME", "DISPLAY", "CONFIGURED", "MODELS", "ACTIVE_AUTH")
				for _, p := range providers {
					t.AddRow(
						stringField(p, "name"),
						stringField(p, "display_name"),
						boolField(p, "configured", false),
						numField(p, "model_count"),
						stringField(p, "active_auth_method"),
					)
				}
				t.Render()
				return nil
			},
		},
		&cobra.Command{
			Use:   "models <provider>",
			Short: "List models for a provider",
			Args:  cobra.ExactArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				out, err := state.Client.GetProviderModels(c.Context(), args[0])
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(out)
				}
				models, _ := out["models"].([]any)
				t := output.NewTable(state.Output, "ID", "NAME")
				for _, m := range models {
					mm, ok := m.(map[string]any)
					if !ok {
						continue
					}
					t.AddRow(stringField(mm, "id"), stringField(mm, "name"))
				}
				t.Render()
				return nil
			},
		},
		newLLMConfigureCmd(),
		newLLMDeleteConfigCmd(),
	)
	return cmd
}

func newLLMConfigureCmd() *cobra.Command {
	var apiKey, authMethod string
	var extraJSON string
	cmd := &cobra.Command{
		Use:   "configure <provider>",
		Short: "Set provider configuration (api key, auth method, etc.)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			body := map[string]any{}
			if apiKey != "" {
				body["api_key"] = apiKey
			}
			if authMethod != "" {
				body["auth_method"] = authMethod
			}
			if extraJSON != "" {
				if err := mergeJSONInto(body, extraJSON); err != nil {
					return err
				}
			}
			if len(body) == 0 {
				return fmt.Errorf("at least one of --api-key, --auth-method, or --json is required")
			}
			return state.Client.ConfigureProvider(cmd.Context(), args[0], body)
		},
	}
	cmd.Flags().StringVar(&apiKey, "api-key", "", "API key value (kept secret server-side)")
	cmd.Flags().StringVar(&authMethod, "auth-method", "", "Active auth method id")
	cmd.Flags().StringVar(&extraJSON, "json", "", "Additional fields as a JSON object")
	return cmd
}

func newLLMDeleteConfigCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete-config <provider>",
		Short: "Remove all stored config for a provider",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.DeleteProviderConfig(cmd.Context(), args[0])
		},
	}
}

func newLLMModelGroupsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "model-groups",
		Short: "Read or write the high/low model group assignments",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "get",
			Short: "Show model group assignments",
			RunE: func(c *cobra.Command, _ []string) error {
				if err := requireToken(); err != nil {
					return err
				}
				out, err := state.Client.GetModelGroups(c.Context())
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(out)
				}
				dumpJSON(out)
				return nil
			},
		},
		newLLMModelGroupsSetCmd(),
	)
	return cmd
}

func newLLMModelGroupsSetCmd() *cobra.Command {
	var high, low, defaultProvider string
	cmd := &cobra.Command{
		Use:   "set",
		Short: "Update model group assignments",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			body := map[string]any{}
			groups := map[string]any{}
			if high != "" {
				groups["high"] = high
			}
			if low != "" {
				groups["low"] = low
			}
			if len(groups) > 0 {
				body["model_groups"] = groups
			}
			if defaultProvider != "" {
				body["default_provider"] = defaultProvider
			}
			if len(body) == 0 {
				return fmt.Errorf("at least one of --high, --low, --default-provider is required")
			}
			return state.Client.UpdateModelGroups(cmd.Context(), body)
		},
	}
	cmd.Flags().StringVar(&high, "high", "", "Model id for the 'high' group")
	cmd.Flags().StringVar(&low, "low", "", "Model id for the 'low' group")
	cmd.Flags().StringVar(&defaultProvider, "default-provider", "", "Default provider name")
	return cmd
}

func newLLMDeviceCodeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "device-code",
		Short: "GitHub Copilot device-code authentication flow",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "start",
			Short: "Start a device-code flow; prints user code + verification URL",
			RunE: func(c *cobra.Command, _ []string) error {
				resp, err := state.Client.DeviceCodeStart(c.Context())
				if err != nil {
					return err
				}
				if state.Output.JSON {
					return output.PrintJSON(resp)
				}
				output.PrintKV([][2]string{
					{"verification_uri", resp.VerificationURI},
					{"user_code", resp.UserCode},
					{"device_code", resp.DeviceCode},
					{"expires_in", fmt.Sprintf("%d", resp.ExpiresIn)},
					{"interval", fmt.Sprintf("%d", resp.Interval)},
				})
				return nil
			},
		},
		&cobra.Command{
			Use:   "poll <device_code>",
			Short: "Poll until the user completes the flow",
			Args:  cobra.ExactArgs(1),
			RunE: func(c *cobra.Command, args []string) error {
				ctx := c.Context()
				deviceCode := args[0]
				interval := 5 * time.Second
				for {
					select {
					case <-ctx.Done():
						return ctx.Err()
					default:
					}
					resp, err := state.Client.DeviceCodePoll(ctx, deviceCode)
					if err != nil {
						return err
					}
					switch resp.Status {
					case "complete":
						if state.Output.JSON {
							return output.PrintJSON(resp)
						}
						if resp.AccessToken != "" {
							output.PrintKV([][2]string{
								{"status", "complete"},
								{"access_token", resp.AccessToken},
							})
						} else {
							output.Println("complete (token stored server-side)")
						}
						return nil
					case "expired":
						return fmt.Errorf("device code expired; run `opa llm device-code start` again")
					case "error":
						return fmt.Errorf("device-code flow error: %s", resp.Error)
					case "pending":
						if resp.SlowDown {
							interval += 5 * time.Second
						}
						time.Sleep(interval)
					default:
						return fmt.Errorf("unexpected status %q", resp.Status)
					}
				}
			},
		},
	)
	return cmd
}

func numField(m map[string]any, k string) string {
	if v, ok := m[k]; ok {
		switch n := v.(type) {
		case float64:
			return fmt.Sprintf("%d", int(n))
		case int:
			return fmt.Sprintf("%d", n)
		default:
			return fmt.Sprintf("%v", v)
		}
	}
	return ""
}

func mergeJSONInto(target map[string]any, body string) error {
	var src map[string]any
	if err := jsonUnmarshalString(body, &src); err != nil {
		return fmt.Errorf("invalid --json: %w", err)
	}
	for k, v := range src {
		target[k] = v
	}
	return nil
}
