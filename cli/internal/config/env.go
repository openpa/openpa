package config

import (
	"errors"
	"os"
	"strings"
)

type OutputMode string

const (
	OutputTable OutputMode = "table"
	OutputJSON  OutputMode = "json"
)

type Config struct {
	Server  string
	Token   string
	Output  OutputMode
	NoColor bool
}

const (
	EnvServer  = "OPENPA_SERVER"
	EnvToken   = "OPENPA_TOKEN"
	EnvOutput  = "OPA_OUTPUT"
	EnvNoColor = "OPA_NO_COLOR"

	DefaultServer = "http://localhost:10000"
)

// LoadFromEnv reads configuration from environment variables. The active
// profile is resolved server-side from the JWT, so it is not part of the
// CLI's configuration.
func LoadFromEnv() (*Config, error) {
	c := &Config{
		Server:  strings.TrimRight(getenv(EnvServer, DefaultServer), "/"),
		Token:   os.Getenv(EnvToken),
		Output:  OutputTable,
		NoColor: os.Getenv(EnvNoColor) != "",
	}

	switch strings.ToLower(os.Getenv(EnvOutput)) {
	case "", "table":
		c.Output = OutputTable
	case "json":
		c.Output = OutputJSON
	default:
		return nil, errors.New(EnvOutput + " must be 'table' or 'json'")
	}

	return c, nil
}

// RequireToken returns an error if no token was loaded.
func (c *Config) RequireToken() error {
	if c.Token == "" {
		return errors.New(EnvToken + " is not set — obtain a JWT from your OpenPA admin or openpa-ui and export it")
	}
	return nil
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
