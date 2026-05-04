package tui

import (
	"context"
	"encoding/json"
	"path/filepath"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"openpa.local/cli/internal/client"
)

// fileListingTimeout caps the wait for a single non-streaming
// /api/files/list call. Watches stay open indefinitely; this only applies
// to the snapshot fetches that fire after watch events.
const fileListingTimeout = 10 * time.Second

// fileTreeMinWidth is the smallest column the tree pane will take. Below
// this we hide the tree entirely so the chat keeps useful real estate on
// narrow terminals.
const fileTreeMinWidth = 24

// fileTreePreferredWidth is the column width assigned when the terminal is
// wide enough. Wider terminals don't benefit from a much wider pane —
// filenames are usually short.
const fileTreePreferredWidth = 32

// File-tree messages dispatched into the chat model.
type fileTreeListedMsg struct {
	path      string
	entries   []client.DirectoryEntry
	truncated bool
	err       error
}

type fileTreeWatchMsg struct {
	eventType string
	path      string
	destPath  string
	isDir     bool
}

type fileTreeReadyMsg struct{}

// fileTreeModel holds the per-frame view state. The runner goroutine in
// pumpFileTree owns the SSE/watchdog plumbing; this struct is just data the
// chat model mutates in Update and renders in View.
type fileTreeModel struct {
	cwd       string
	entries   []client.DirectoryEntry
	width     int
	height    int
	visible   bool
	loading   bool
	truncated bool
	errMsg    string
}

func newFileTreeModel() fileTreeModel {
	return fileTreeModel{visible: true}
}

// applyListing replaces the entries shown in the pane.
func (m *fileTreeModel) applyListing(msg fileTreeListedMsg) {
	if msg.err != nil {
		m.errMsg = msg.err.Error()
		m.entries = nil
		m.truncated = false
		m.loading = false
		return
	}
	m.errMsg = ""
	m.cwd = msg.path
	m.entries = msg.entries
	m.truncated = msg.truncated
	m.loading = false
}

// shouldRefetchOn returns true when a watch event lands inside the
// currently-rendered directory and therefore warrants reloading the listing.
// Only the immediate-children level is rendered, so deep changes are ignored.
func (m *fileTreeModel) shouldRefetchOn(msg fileTreeWatchMsg) bool {
	if m.cwd == "" {
		return false
	}
	if filepath.Dir(msg.path) == m.cwd {
		return true
	}
	if msg.eventType == "moved" && msg.destPath != "" && filepath.Dir(msg.destPath) == m.cwd {
		return true
	}
	return false
}

// View renders the pane as a fixed-width column. Returns an empty string
// when hidden or too narrow to show anything useful.
func (m *fileTreeModel) View(t Theme) string {
	if !m.visible || m.width < fileTreeMinWidth || m.height <= 0 {
		return ""
	}
	var lines []string
	header := "📁 " + truncateMiddle(m.cwd, m.width-3)
	if m.cwd == "" {
		header = "📁 (no cwd yet)"
	}
	lines = append(lines, t.TreeHeader.Width(m.width).Render(header))

	if m.errMsg != "" {
		lines = append(lines, t.Err.Render(truncateMiddle(m.errMsg, m.width)))
	} else if m.loading && len(m.entries) == 0 {
		lines = append(lines, t.Dim.Render("loading…"))
	} else {
		for _, e := range m.entries {
			icon := "📄 "
			if e.IsDir {
				icon = "📁 "
			}
			line := icon + e.Name
			if lipgloss.Width(line) > m.width {
				line = truncateMiddle(line, m.width)
			}
			if e.IsDir {
				line = t.TreeDir.Render(line)
			} else {
				line = t.TreeFile.Render(line)
			}
			lines = append(lines, line)
			if len(lines) >= m.height {
				break
			}
		}
		if m.truncated && len(lines) < m.height {
			lines = append(lines, t.Dim.Render("(truncated)"))
		}
	}

	for len(lines) < m.height {
		lines = append(lines, "")
	}
	if len(lines) > m.height {
		lines = lines[:m.height]
	}
	// Render each line padded to ``m.width`` so the column has a clean right
	// edge regardless of content width. The TreePane style draws the divider
	// on the left.
	col := lipgloss.NewStyle().Width(m.width - 2).Render(
		lipgloss.JoinVertical(lipgloss.Left, lines...),
	)
	return t.TreePane.Render(col)
}

// truncateMiddle shortens a string with an ellipsis in the middle to fit
// within max display columns. Operates on the rune count, which is a fine
// approximation for the ASCII paths the OS surfaces here.
func truncateMiddle(s string, max int) string {
	if max <= 0 || s == "" {
		return ""
	}
	if lipgloss.Width(s) <= max {
		return s
	}
	if max <= 3 {
		return strings.Repeat(".", max)
	}
	runes := []rune(s)
	keep := max - 1 // for the ellipsis
	half := keep / 2
	return string(runes[:half]) + "…" + string(runes[len(runes)-(keep-half):])
}

// pumpFileTree is the producer half of the file-tree subsystem. It loops
// reading from cwdCh: each cwd pushed into the channel cancels the
// previous fetch+watch and restarts against the new path.
//
// The seed cwd arrives via the conversation SSE stream's ``ready`` event
// (it carries the agent's effective working directory for the
// conversation). Subsequent ``cwd`` events — fired by the
// change_working_directory tool — keep the tree in sync as the agent
// navigates during reasoning.
func pumpFileTree(ctx context.Context, prog *tea.Program, c *client.Client, cwdCh <-chan string, conversationID string) {
	var (
		current     string
		innerCancel context.CancelFunc
	)
	stop := func() {
		if innerCancel != nil {
			innerCancel()
			innerCancel = nil
		}
	}
	defer stop()

	startWatching := func(path string) {
		stop()
		current = path
		inner, cancel := context.WithCancel(ctx)
		innerCancel = cancel
		go runFileTreeWatch(inner, prog, c, path, conversationID)
	}

	for {
		select {
		case <-ctx.Done():
			return
		case path, ok := <-cwdCh:
			if !ok {
				return
			}
			if path == "" || path == current {
				continue
			}
			startWatching(path)
		}
	}
}

// runFileTreeWatch fetches the snapshot listing once, then opens the
// /api/files/watch SSE stream and forwards events into the chat model
// via prog.Send.
func runFileTreeWatch(ctx context.Context, prog *tea.Program, c *client.Client, path string, conversationID string) {
	listing, err := c.ListDirectory(ctx, path, false, conversationID)
	if err != nil {
		if ctx.Err() != nil {
			return
		}
		prog.Send(fileTreeListedMsg{path: path, err: err})
		// Don't open the watch if we couldn't even list — likely a
		// permission/allowlist failure that watching won't fix.
		return
	}
	prog.Send(fileTreeListedMsg{
		path:      listing.Path,
		entries:   listing.Entries,
		truncated: listing.Truncated,
	})

	events, errs := c.Stream(ctx, client.WatchFilesPath(path, conversationID))
	for {
		select {
		case <-ctx.Done():
			return
		case <-errs:
			return
		case ev, open := <-events:
			if !open {
				return
			}
			if ev.Type == "ready" {
				prog.Send(fileTreeReadyMsg{})
				continue
			}
			var p struct {
				Path     string `json:"path"`
				DestPath string `json:"dest_path"`
				IsDir    bool   `json:"is_dir"`
			}
			_ = json.Unmarshal(ev.Raw, &p)
			prog.Send(fileTreeWatchMsg{
				eventType: ev.Type,
				path:      p.Path,
				destPath:  p.DestPath,
				isDir:     p.IsDir,
			})
		}
	}
}

// refetchListingCmd is a one-shot tea.Cmd used after a watch event to refresh
// the rendered entries. It runs against a short-lived context so it doesn't
// block the program if the server is slow.
func refetchListingCmd(c *client.Client, path string, conversationID string) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), fileListingTimeout)
		defer cancel()
		listing, err := c.ListDirectory(ctx, path, false, conversationID)
		if err != nil {
			return fileTreeListedMsg{path: path, err: err}
		}
		return fileTreeListedMsg{
			path:      listing.Path,
			entries:   listing.Entries,
			truncated: listing.Truncated,
		}
	}
}
