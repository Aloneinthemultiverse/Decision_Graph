package main

// WHY: top-level Go rationale

import "fmt"

// NOTE: comment above type definition
type Config struct {
	// HACK: field-level rationale
	APIKey string
}

func (c *Config) Validate() error {
	// SAFETY: method-internal rationale
	// BUG: another one
	return nil
}

// DEPRECATED: trailing comment, no symbol below
