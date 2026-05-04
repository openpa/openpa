// Package tui implements the bubbletea chat view used by `opa chat` and
// `opa conv send`/`opa conv attach` (default modes).
package tui

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"openpa.local/cli/internal/client"
)

// Mode controls behavior of the chat model.
type Mode int

const (
	// ModeOneShot: send a single message, stream until complete, exit.
	ModeOneShot Mode = iota
	// ModeAttach: subscribe without sending, stream until complete or quit.
	ModeAttach
	// ModeInteractive: full REPL — input area visible, send loop runs forever.
	ModeInteractive
	// ModeReplay: render a precomputed sequence of events (e.g. a persisted
	// conversation history) and wait for the user to dismiss with ESC. No
	// SSE stream is opened.
	ModeReplay
)

// sectionState tracks which logical block of output the viewport is currently
// in, so the model can inject a header when the agent transitions from
// reasoning to its final response.
type sectionState int

const (
	sectionNone sectionState = iota
	sectionThinking
	sectionResponse
)

// Config configures NewModel.
type Config struct {
	Client         *client.Client
	ConversationID string
	Title          string

	Mode      Mode
	InitialMsg string // ModeOneShot only — the message to send on connect
	Reasoning  bool

	// Replay is the event sequence rendered in ModeReplay. Ignored in other
	// modes.
	Replay []client.Event

	Theme Theme
}

// Run starts the TUI and blocks until the user quits or the run completes.
func Run(ctx context.Context, cfg Config) error {
	m := newModel(cfg)
	prog := tea.NewProgram(&m, tea.WithAltScreen(), tea.WithMouseCellMotion())

	pumpCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	// File-tree pump: listens on cwdCh for working-directory updates from
	// terminal events and (re)opens the snapshot+watch on each new path.
	// Replay mode is offline, so skip it there.
	var cwdCh chan string
	if cfg.Client != nil && cfg.Mode != ModeReplay {
		cwdCh = make(chan string, 4)
		m.cwdSink = cwdCh
		go pumpFileTree(pumpCtx, prog, cfg.Client, cwdCh, cfg.ConversationID)
	}

	if cfg.Mode == ModeReplay {
		go pumpReplay(pumpCtx, prog, cfg.Replay)
	} else {
		go pumpStream(pumpCtx, prog, cfg.Client, cfg.ConversationID, cfg.Mode, cfg.InitialMsg, cfg.Reasoning)
	}

	_, err := prog.Run()
	if cwdCh != nil {
		close(cwdCh)
	}
	return err
}

// pumpReplay walks a precomputed event sequence and dispatches each entry
// through the same evMsg pipeline used by the live stream. After the last
// event it sends evDone so the model latches into "press ESC to exit".
func pumpReplay(ctx context.Context, prog *tea.Program, events []client.Event) {
	for _, ev := range events {
		select {
		case <-ctx.Done():
			return
		default:
		}
		prog.Send(evMsg(ev))
	}
	prog.Send(evDone{})
}

type model struct {
	cfg   Config
	theme Theme

	viewport viewport.Model
	textarea textarea.Model

	events     []renderedEvent
	tokenUsage string

	streaming bool
	runID     string
	ready     bool
	completed bool

	section sectionState

	width, height int

	statusMsg string
	errMsg    string

	// fileTree holds the right-side workspace pane. cwdSink, when non-nil,
	// receives cwd updates from terminal events; pumpFileTree consumes the
	// channel and runs the listing + watch SSE on each new path.
	fileTree fileTreeModel
	cwdSink  chan<- string
}

func newModel(cfg Config) model {
	// Caller is responsible for providing a Theme; cmd/conv.go calls
	// themeForCfg() which always returns a populated value. We don't fall
	// back to DefaultTheme() here because lipgloss styles aren't comparable
	// (they contain slice fields), so detecting the zero value isn't trivial.
	ta := textarea.New()
	ta.Placeholder = "Type a message and press Enter to send (ESC to exit, Ctrl+C to cancel run)"
	ta.Prompt = "❯ "
	ta.CharLimit = 0
	ta.SetHeight(3)
	ta.ShowLineNumbers = false
	ta.Focus()

	vp := viewport.New(80, 20)
	vp.SetContent("")

	return model{
		cfg:      cfg,
		theme:    cfg.Theme,
		viewport: vp,
		textarea: ta,
		fileTree: newFileTreeModel(),
	}
}

// Custom messages dispatched from the SSE pump.
type evMsg client.Event
type evErr struct{ err error }
type evRunID struct{ id string }
type evDone struct{}

// pumpStream is the producer half: it runs in a goroutine, opens SSE and
// translates each frame into a tea.Msg via prog.Send. It also performs the
// `subscribe-first → wait for ready → POST` handshake.
func pumpStream(ctx context.Context, prog *tea.Program, c *client.Client, convID string, mode Mode, initial string, reasoning bool) {
	events, errs := c.Stream(ctx, c.ConversationStreamPath(convID))
	ready := false
	sent := false

	for {
		select {
		case <-ctx.Done():
			return
		case err, open := <-errs:
			if !open {
				errs = nil // drain — events channel will signal done
				continue
			}
			if err != nil {
				prog.Send(evErr{err})
				return
			}
		case ev, open := <-events:
			if !open {
				prog.Send(evDone{})
				return
			}
			if ev.Type == "ready" && !ready {
				ready = true
				if (mode == ModeOneShot || mode == ModeInteractive) && initial != "" && !sent {
					resp, err := c.SendMessage(ctx, convID, initial, reasoning)
					if err != nil {
						prog.Send(evErr{fmt.Errorf("send message: %w", err)})
						return
					}
					sent = true
					prog.Send(evRunID{id: resp.RunID})
				}
			}
			prog.Send(evMsg(ev))
		}
	}
}

// sendUserMessage is invoked when the user submits the textarea. It sends the
// message asynchronously so the model's Update can return promptly.
func (m *model) sendUserMessage(text string) tea.Cmd {
	convID := m.cfg.ConversationID
	c := m.cfg.Client
	reasoning := m.cfg.Reasoning
	return func() tea.Msg {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		resp, err := c.SendMessage(ctx, convID, text, reasoning)
		if err != nil {
			return evErr{err}
		}
		return evRunID{id: resp.RunID}
	}
}

// cancelRun calls the cancel-task endpoint asynchronously.
func (m *model) cancelRun() tea.Cmd {
	if m.runID == "" {
		return nil
	}
	c := m.cfg.Client
	id := m.runID
	return func() tea.Msg {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		_, _ = c.CancelTask(ctx, id)
		return nil
	}
}

func (m *model) Init() tea.Cmd {
	// We deliberately don't kick off cursor blink here — the textarea handles
	// its own blink internally once Update receives its first message. Returning
	// a literal package-level Cmd here would couple us to a symbol whose name
	// has shifted between bubbles versions.
	return nil
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmds []tea.Cmd

	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.layout()

	case tea.KeyMsg:
		switch msg.String() {
		case "esc", "ctrl+d":
			return m, tea.Quit
		case "ctrl+c":
			if m.streaming && m.runID != "" {
				m.statusMsg = "cancelling run…"
				return m, m.cancelRun()
			}
			return m, tea.Quit
		case "ctrl+b":
			// Toggle the file-tree pane.
			m.fileTree.visible = !m.fileTree.visible
			m.layout()
			return m, nil
		case "enter":
			if m.cfg.Mode == ModeInteractive && m.textarea.Focused() && !m.streaming {
				text := strings.TrimSpace(m.textarea.Value())
				if text == "" {
					return m, nil
				}
				m.textarea.Reset()
				m.streaming = true
				m.completed = false
				m.section = sectionNone
				m.statusMsg = "sending…"
				return m, m.sendUserMessage(text)
			}
		}

	case evMsg:
		ev := client.Event(msg)
		// ``ready`` (initial cwd seed) and ``cwd`` (change_working_directory
		// publishes) carry the agent's effective working directory. Forward
		// it to the file-tree pump so the pane re-renders against the new
		// path. Both event types use the same payload shape.
		if (ev.Type == "ready" || ev.Type == "cwd") && m.cwdSink != nil {
			var p struct {
				Data struct {
					WorkingDirectory string `json:"working_directory"`
				} `json:"data"`
			}
			if err := json.Unmarshal(ev.Raw, &p); err == nil && p.Data.WorkingDirectory != "" {
				select {
				case m.cwdSink <- p.Data.WorkingDirectory:
				default:
				}
			}
		}
		if ev.Type == "ready" {
			m.ready = true
		}
		if ev.Type == "complete" || ev.Type == "error" {
			m.streaming = false
			m.runID = ""
			m.statusMsg = ""
		}
		if u := extractTokenUsage(ev); u != "" {
			m.tokenUsage = u
		}
		if rendered, ok := formatEvent(ev, m.theme); ok {
			if rendered.kind == "user" {
				// New turn — reset section tracking so the next thinking/text
				// event re-emits the header.
				m.section = sectionNone
			}
			if hdr, ok := m.sectionHeaderFor(rendered.kind); ok {
				m.events = append(m.events, hdr)
			}
			// Coalesce streamed response tokens into a single growing entry
			// so the viewport renders one message instead of one-per-token.
			if rendered.kind == "text" && len(m.events) > 0 && m.events[len(m.events)-1].kind == "text" {
				m.events[len(m.events)-1].body += rendered.body
			} else {
				m.events = append(m.events, rendered)
			}
			m.refreshViewport()
		}
		if (ev.Type == "complete" || ev.Type == "error") && m.cfg.Mode != ModeInteractive {
			// One-shot / attach: keep the screen up so the user can review
			// the output. ESC dismisses (handled in the KeyMsg branch above).
			m.completed = true
		}

	case evRunID:
		m.runID = msg.id
		m.streaming = true
		m.statusMsg = "running…"

	case evErr:
		m.errMsg = msg.err.Error()
		m.streaming = false
		m.runID = ""
		// In one-shot/attach modes, an error ends the run but we keep the
		// screen up so the user can read the error before pressing ESC.
		if m.cfg.Mode != ModeInteractive {
			m.completed = true
		}

	case evDone:
		// Stream closed cleanly without `complete`.
		if m.cfg.Mode != ModeInteractive {
			m.completed = true
		} else {
			m.statusMsg = "stream closed; press ESC to exit"
		}

	case fileTreeListedMsg:
		m.fileTree.applyListing(msg)

	case fileTreeWatchMsg:
		if m.fileTree.shouldRefetchOn(msg) && m.cfg.Client != nil {
			cmds = append(cmds, refetchListingCmd(m.cfg.Client, m.fileTree.cwd, m.cfg.ConversationID))
		}

	case fileTreeReadyMsg:
		// Handshake — nothing visible to do; logged for clarity.
	}

	var cmd tea.Cmd
	m.viewport, cmd = m.viewport.Update(msg)
	cmds = append(cmds, cmd)

	if m.cfg.Mode == ModeInteractive {
		m.textarea, cmd = m.textarea.Update(msg)
		cmds = append(cmds, cmd)
	}

	return m, tea.Batch(cmds...)
}

func (m *model) layout() {
	w := m.width
	h := m.height
	if w == 0 || h == 0 {
		return
	}

	headerH := 1
	statusH := 1
	inputH := 0
	if m.cfg.Mode == ModeInteractive {
		inputH = 5
	}
	vpH := h - headerH - statusH - inputH
	if vpH < 3 {
		vpH = 3
	}

	treeW := 0
	// Reserve a column for the file tree only when the model wants it visible
	// AND there's enough room left for the chat to remain usable. The +2
	// covers the divider/border baked into TreePane.
	if m.fileTree.visible && m.cfg.Client != nil && w >= fileTreeMinWidth*3 {
		treeW = fileTreePreferredWidth
		if treeW > w/3 {
			treeW = w / 3
		}
	}
	m.fileTree.width = treeW
	m.fileTree.height = vpH

	m.viewport.Width = w - treeW
	m.viewport.Height = vpH
	if m.cfg.Mode == ModeInteractive {
		m.textarea.SetWidth(w - 2)
	}
	m.refreshViewport()
}

// sectionHeaderFor advances the model's section state based on the kind of the
// next rendered event. If the section transitions (or starts), it returns a
// synthesized header event to insert before the real event. Neutral kinds
// (user, info, error) don't change the section.
func (m *model) sectionHeaderFor(kind string) (renderedEvent, bool) {
	var next sectionState
	switch kind {
	case "thinking", "phase", "terminal", "file", "summary":
		next = sectionThinking
	case "text":
		next = sectionResponse
	default:
		return renderedEvent{}, false
	}
	if next == m.section {
		return renderedEvent{}, false
	}
	m.section = next
	label := "── Thinking Process ──"
	if next == sectionResponse {
		label = "── Response ──"
	}
	prefix := ""
	if len(m.events) > 0 {
		prefix = "\n"
	}
	return renderedEvent{
		kind: "section",
		body: prefix + m.theme.Section.Render(label),
	}, true
}

func (m *model) refreshViewport() {
	parts := make([]string, 0, len(m.events)*2)
	for _, e := range m.events {
		parts = append(parts, e.body)
	}
	m.viewport.SetContent(strings.Join(parts, "\n"))
	m.viewport.GotoBottom()
}

func (m *model) View() string {
	t := m.theme
	header := t.Header.Width(m.width).Render(m.headerText())

	status := m.statusLine()
	statusBar := t.StatusBar.Width(m.width).Render(status)

	body := m.viewport.View()
	if m.fileTree.width > 0 {
		tree := m.fileTree.View(t)
		if tree != "" {
			body = lipgloss.JoinHorizontal(lipgloss.Top, body, tree)
		}
	}

	if m.cfg.Mode == ModeInteractive {
		input := lipgloss.NewStyle().Width(m.width).Render(m.textarea.View())
		return lipgloss.JoinVertical(lipgloss.Left, header, body, statusBar, input)
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, body, statusBar)
}

func (m *model) headerText() string {
	if m.cfg.Title != "" {
		return m.cfg.Title
	}
	return m.cfg.ConversationID
}

func (m *model) statusLine() string {
	t := m.theme
	parts := []string{}
	switch {
	case m.completed && m.cfg.Mode != ModeInteractive && m.errMsg != "":
		parts = append(parts, t.Err.Render("error: "+m.errMsg+" · press ESC to exit"))
	case m.completed && m.cfg.Mode != ModeInteractive:
		parts = append(parts, "done · press ESC to exit")
	case m.errMsg != "":
		parts = append(parts, t.Err.Render("error: "+m.errMsg))
	case m.statusMsg != "":
		parts = append(parts, m.statusMsg)
	case m.streaming:
		parts = append(parts, "streaming…")
	case m.ready:
		parts = append(parts, "ready")
	default:
		parts = append(parts, "connecting…")
	}
	if m.tokenUsage != "" {
		parts = append(parts, t.Dim.Render(m.tokenUsage))
	}
	return strings.Join(parts, "  ·  ")
}
