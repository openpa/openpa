package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/mdp/qrterminal/v3"
	"github.com/spf13/cobra"
	"golang.org/x/term"

	"openpa.local/cli/internal/client"
	"openpa.local/cli/internal/output"
)

func newChannelsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "channels",
		Short: "Register and manage external messaging channels",
	}
	cmd.AddCommand(
		newChannelsListCmd(),
		newChannelsAddCmd(),
		newChannelsDeleteCmd(),
		newChannelsCatalogCmd(),
		newChannelsPairCmd(),
	)
	return cmd
}

func newChannelsListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List configured channels for the active profile",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			channels, err := state.Client.ListChannels(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(channels)
			}
			t := output.NewTable(state.Output, "ID", "TYPE", "MODE", "AUTH", "REPLY", "ENABLED", "STATUS")
			for _, c := range channels {
				t.AddRow(
					c.ID, c.ChannelType, c.Mode, c.AuthMode, c.ResponseMode,
					strconv.FormatBool(c.Enabled), c.Status,
				)
			}
			t.Render()
			return nil
		},
	}
}

func newChannelsCatalogCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "catalog",
		Short: "Print the dynamic channel catalog (TOML-defined)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			catalog, err := state.Client.GetChannelCatalog(cmd.Context())
			if err != nil {
				return err
			}
			if state.Output.JSON {
				return output.PrintJSON(catalog)
			}
			dumpJSON(catalog)
			return nil
		},
	}
}

func newChannelsAddCmd() *cobra.Command {
	var (
		channelType  string
		mode         string
		authMode     string
		responseMode string
		configJSON   string
		configKV     []string
		enabled      bool
		noPair       bool
	)
	cmd := &cobra.Command{
		Use:   "add",
		Short: "Register a new channel (auto-launches pairing for QR / code modes)",
		Long: `Register an external messaging channel.

Required: --type. Channel-specific config can be passed two ways:

  --json '{"bot_token":"…"}'        (single JSON object)
  --config bot_token=…              (repeatable key=value, values are strings)

The --config form avoids cross-shell quoting traps — Windows PowerShell
in particular strips inner double quotes from native-binary arguments,
so '--json {"k":"v"}' arrives as '--json {k:v}' and fails to parse.

Refer to "opa channels catalog" for the field layout per channel.

When the chosen mode declares an interactive setup (WhatsApp QR scan,
Telegram userbot code + 2FA), the pairing flow is started automatically
in the same terminal session — equivalent to running "opa channels pair
<id>" right after "add". Pass --no-pair to skip, --json (root flag) also
suppresses auto-pairing because it implies a non-interactive caller.`,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			if channelType == "" {
				return fmt.Errorf("--type is required (e.g. telegram, discord)")
			}
			if configJSON != "" && len(configKV) > 0 {
				return fmt.Errorf("--json and --config are mutually exclusive")
			}
			req := client.CreateChannelRequest{
				ChannelType:  channelType,
				Mode:         mode,
				AuthMode:     authMode,
				ResponseMode: responseMode,
				Enabled:      &enabled,
			}
			if configJSON != "" {
				var cfg map[string]any
				if err := json.Unmarshal([]byte(configJSON), &cfg); err != nil {
					return fmt.Errorf(
						"--json: %w\n"+
							"Hint: on Windows PowerShell, inner double-quotes inside --json are stripped\n"+
							"      when passing arguments to native binaries. Use --config key=value\n"+
							"      instead — e.g. --config phone=+84986664411 (repeat for multiple keys).",
						err)
				}
				req.Config = cfg
			} else if len(configKV) > 0 {
				cfg := map[string]any{}
				for _, kv := range configKV {
					i := strings.IndexByte(kv, '=')
					if i <= 0 {
						return fmt.Errorf("--config %q: expected key=value", kv)
					}
					cfg[kv[:i]] = kv[i+1:]
				}
				req.Config = cfg
			}
			ch, err := state.Client.CreateChannel(cmd.Context(), req)
			if err != nil {
				return err
			}
			if state.Output.JSON {
				// JSON mode = scripted caller. Print the row and exit; never
				// drop into the interactive pairing UI even if the mode would
				// otherwise warrant it.
				return output.PrintJSON(ch)
			}
			output.PrintKV([][2]string{
				{"id", ch.ID},
				{"channel_type", ch.ChannelType},
				{"mode", ch.Mode},
				{"auth_mode", ch.AuthMode},
				{"response_mode", ch.ResponseMode},
				{"enabled", strconv.FormatBool(ch.Enabled)},
				{"status", ch.Status},
			})

			if noPair || !ch.Enabled {
				// --no-pair, or the user explicitly registered the row in
				// the disabled state — there's no live adapter to pair with.
				return nil
			}

			needs, err := channelModeNeedsPairing(cmd.Context(), ch.ChannelType, ch.Mode)
			if err != nil {
				// Don't fail `add` because we couldn't read the catalog;
				// just hint that the user can pair manually.
				fmt.Fprintln(os.Stderr, "warning: catalog lookup failed:", err)
				return nil
			}
			if !needs {
				return nil
			}

			fmt.Println()
			fmt.Println("This channel needs interactive pairing — starting the pairing flow.")
			fmt.Printf("(re-run later with `opa channels pair %s`, or pass --no-pair to skip)\n", ch.ID)
			fmt.Println()
			return runChannelPairing(cmd.Context(), ch.ID)
		},
	}
	cmd.Flags().StringVar(&channelType, "type", "", "Channel type (telegram, whatsapp, discord, messenger, slack)")
	cmd.Flags().StringVar(&mode, "mode", "bot", "Channel mode (bot|userbot)")
	cmd.Flags().StringVar(&authMode, "auth-mode", "none", "Auth mode (none|otp|password)")
	cmd.Flags().StringVar(&responseMode, "response-mode", "normal", "Reply detail (normal|detail)")
	cmd.Flags().StringVar(&configJSON, "json", "", "Channel-specific config as JSON (e.g. '{\"bot_token\":\"…\"}'); on PowerShell prefer --config")
	cmd.Flags().StringArrayVar(&configKV, "config", nil, "Channel-specific config as repeatable key=value (alternative to --json; values are strings)")
	cmd.Flags().BoolVar(&enabled, "enabled", true, "Start the adapter immediately")
	cmd.Flags().BoolVar(&noPair, "no-pair", false, "Skip auto-launching the interactive pairing flow")
	return cmd
}

// channelModeNeedsPairing returns true when the catalog declares a
// ``setup_kind`` for the given (channel_type, mode) — i.e. the adapter
// expects a QR scan, verification code, or similar interactive step
// before it can finish coming online. Used by ``opa channels add`` to
// decide whether to drop into the pairing flow automatically.
func channelModeNeedsPairing(ctx context.Context, channelType, modeID string) (bool, error) {
	catalog, err := state.Client.GetChannelCatalog(ctx)
	if err != nil {
		return false, err
	}
	entry, ok := catalog[channelType].(map[string]any)
	if !ok {
		return false, nil
	}
	channelSection, ok := entry["channel"].(map[string]any)
	if !ok {
		return false, nil
	}
	rawModes, ok := channelSection["modes"].([]any)
	if !ok {
		return false, nil
	}
	for _, m := range rawModes {
		mm, ok := m.(map[string]any)
		if !ok {
			continue
		}
		if id, _ := mm["id"].(string); id == modeID {
			sk, _ := mm["setup_kind"].(string)
			return sk != "", nil
		}
	}
	return false, nil
}

func newChannelsDeleteCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "delete <id>",
		Short: "Delete a channel (cascades all its conversations and senders)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return state.Client.DeleteChannel(cmd.Context(), args[0])
		},
	}
}

func newChannelsPairCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "pair <id>",
		Short: "Interactive pairing flow (QR scan, verification code, 2FA password)",
		Long: `Run the interactive pairing flow for a channel directly in the terminal.

For WhatsApp this renders the linked-device QR as a Unicode-block code you
can scan from your phone's WhatsApp → Settings → Linked Devices → Link a
Device. Each new QR pushed by the server replaces the previous one (Baileys
rotates every ~20s while waiting).

For Telegram userbot this prompts for the verification code Telegram sends
through the Telegram app itself (or by SMS if no other Telegram session is
connected). If the account has two-step verification enabled, you'll then
be prompted for the cloud password (typed without echo).

The command exits when pairing succeeds (` + "`ready`" + ` event), the
session is logged out remotely, the server returns a fatal error, or you
press Ctrl-C.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := requireToken(); err != nil {
				return err
			}
			return runChannelPairing(cmd.Context(), args[0])
		},
	}
}

// authEventPayload is the union of fields the auth-events SSE may carry —
// the ``kind`` field discriminates. ``omitempty`` is harmless on receive.
type authEventPayload struct {
	Kind      string `json:"kind"`
	Qr        string `json:"qr,omitempty"`        // data-URL — ignored by the CLI
	Raw       string `json:"raw,omitempty"`       // raw QR string for terminal rendering
	Phone     string `json:"phone,omitempty"`     // hint for code_required prompt
	Error     string `json:"error,omitempty"`     // shown above the next prompt
	LoggedOut bool   `json:"logged_out,omitempty"`
}

func runChannelPairing(ctx context.Context, channelID string) error {
	events, errs := state.Client.Stream(ctx, state.Client.ChannelAuthEventsPath(channelID))

	stdin := bufio.NewReader(os.Stdin)

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err, ok := <-errs:
			if !ok {
				errs = nil
				continue
			}
			if err != nil {
				return err
			}
		case ev, ok := <-events:
			if !ok {
				return nil
			}
			var payload authEventPayload
			if err := json.Unmarshal(ev.Raw, &payload); err != nil {
				continue
			}
			if state.Output.JSON {
				fmt.Println(string(ev.Raw))
				if payload.Kind == "ready" {
					return nil
				}
				continue
			}
			switch payload.Kind {
			case "qr":
				renderTerminalQR(payload.Raw)
			case "code_required":
				if err := promptAndSubmit(ctx, channelID, stdin, &payload, false); err != nil {
					return err
				}
			case "password_required":
				if err := promptAndSubmit(ctx, channelID, stdin, &payload, true); err != nil {
					return err
				}
			case "ready":
				fmt.Println()
				fmt.Println("✓ Paired successfully.")
				return nil
			case "disconnected":
				if payload.LoggedOut {
					fmt.Println()
					fmt.Println("Session was logged out — pair again to re-link.")
					return nil
				}
				fmt.Println()
				fmt.Println("Disconnected — waiting for reconnect…")
			case "error":
				if payload.Error != "" {
					fmt.Fprintln(os.Stderr, "error:", payload.Error)
				}
			}
		}
	}
}

func renderTerminalQR(raw string) {
	if raw == "" {
		fmt.Println("(QR received without raw payload — open the web UI to scan)")
		return
	}
	clearScreen()
	fmt.Println("Open WhatsApp → Settings → Linked Devices → Link a Device, then scan:")
	fmt.Println()
	// Half-block rendering uses ▀ / ▄ so each cell is half the vertical
	// space of a full block — WhatsApp's QR is fairly dense and a
	// full-block render usually overflows a default 24-row terminal.
	qrterminal.GenerateHalfBlock(raw, qrterminal.M, os.Stdout)
	fmt.Println()
	fmt.Println("(QR rotates every ~20s; the latest one will replace this view automatically)")
}

func clearScreen() {
	// ANSI clear+home — works on Windows Terminal, modern PowerShell, and
	// any *nix terminal. Old cmd.exe doesn't honour it but is rare now.
	fmt.Print("\033[2J\033[H")
}

func promptAndSubmit(
	ctx context.Context, channelID string, stdin *bufio.Reader,
	payload *authEventPayload, secret bool,
) error {
	fmt.Println()
	if payload.Error != "" {
		fmt.Fprintln(os.Stderr, "error:", payload.Error)
	}
	if payload.Kind == "code_required" {
		hint := payload.Phone
		if hint == "" {
			hint = "your phone"
		}
		fmt.Printf("Telegram sent a verification code to %s.\n", hint)
		fmt.Print("Code: ")
	} else {
		fmt.Println("Two-step verification password required.")
		fmt.Print("Password: ")
	}

	var (
		input string
		err   error
	)
	if secret {
		// Hide the password from the screen. Using ``os.Stdin.Fd()``
		// (uintptr) instead of ``syscall.Stdin`` because the latter has
		// different types across Windows and *nix.
		var raw []byte
		raw, err = term.ReadPassword(int(os.Stdin.Fd()))
		fmt.Println()
		input = string(raw)
	} else {
		input, err = stdin.ReadString('\n')
	}
	if err != nil {
		if err == io.EOF {
			return fmt.Errorf("aborted: stdin closed before input was provided")
		}
		return err
	}
	input = strings.TrimRight(input, "\r\n")
	if input == "" {
		fmt.Fprintln(os.Stderr, "empty input — waiting for next prompt…")
		return nil
	}

	code, password := "", ""
	if payload.Kind == "code_required" {
		code = input
	} else {
		password = input
	}
	if err := state.Client.SubmitChannelAuthInput(ctx, channelID, code, password); err != nil {
		fmt.Fprintln(os.Stderr, "submit failed:", err)
	}
	// Either the next event from the SSE stream is ``ready``, the same
	// prompt re-fires with an ``error`` field for retry, or
	// ``password_required`` follows the code step.
	return nil
}
