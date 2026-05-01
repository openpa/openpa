package tui

import "github.com/charmbracelet/lipgloss"

// Theme groups the lipgloss styles used by the chat view. It's a value type so
// callers can hand it around without pointer-receiver footguns.
type Theme struct {
	Header    lipgloss.Style
	StatusBar lipgloss.Style
	UserMsg   lipgloss.Style
	AssistMsg lipgloss.Style
	Thinking  lipgloss.Style
	Phase     lipgloss.Style
	Section   lipgloss.Style
	Box       lipgloss.Style
	Err       lipgloss.Style
	Dim       lipgloss.Style
	Prompt    lipgloss.Style
}

// DefaultTheme returns the colored theme.
func DefaultTheme() Theme {
	return Theme{
		Header: lipgloss.NewStyle().
			Background(lipgloss.Color("63")).
			Foreground(lipgloss.Color("230")).
			Padding(0, 1).
			Bold(true),
		StatusBar: lipgloss.NewStyle().
			Background(lipgloss.Color("236")).
			Foreground(lipgloss.Color("250")).
			Padding(0, 1),
		UserMsg: lipgloss.NewStyle().
			Foreground(lipgloss.Color("39")).
			Bold(true),
		AssistMsg: lipgloss.NewStyle().
			Foreground(lipgloss.Color("252")),
		Thinking: lipgloss.NewStyle().
			Foreground(lipgloss.Color("244")).
			Italic(true),
		Phase: lipgloss.NewStyle().
			Foreground(lipgloss.Color("214")).
			Italic(true),
		Section: lipgloss.NewStyle().
			Foreground(lipgloss.Color("252")).
			Bold(true),
		Box: lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("240")).
			Padding(0, 1),
		Err: lipgloss.NewStyle().
			Foreground(lipgloss.Color("196")).
			Bold(true),
		Dim: lipgloss.NewStyle().
			Foreground(lipgloss.Color("240")),
		Prompt: lipgloss.NewStyle().
			Foreground(lipgloss.Color("212")).
			Bold(true),
	}
}

// MonochromeTheme returns a theme with no colors (for OPA_NO_COLOR).
func MonochromeTheme() Theme {
	plain := lipgloss.NewStyle()
	return Theme{
		Header:    plain.Bold(true),
		StatusBar: plain,
		UserMsg:   plain.Bold(true),
		AssistMsg: plain,
		Thinking:  plain.Italic(true),
		Phase:     plain.Italic(true),
		Section:   plain.Bold(true),
		Box:       plain.Border(lipgloss.NormalBorder()),
		Err:       plain.Bold(true),
		Dim:       plain,
		Prompt:    plain.Bold(true),
	}
}
