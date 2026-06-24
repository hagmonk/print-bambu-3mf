package bambu

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// Decoded JSON values use Go's dynamic shapes:
//   number -> json.Number (string-backed, preserves exact textual form)
//   string -> string
//   bool   -> bool
//   null   -> nil
//   array  -> []any
//   object -> map[string]any
//
// json.Number is essential: it keeps "0.2" as "0.2" (never 0.20000001) so that
// both equality comparisons and re-serialized output match the Python original.

// decodeJSON unmarshals bytes into a dynamic value, preserving numbers as
// json.Number to mirror Python's json module fidelity.
func decodeJSON(data []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	return dec.Decode(v)
}

// pyStr mimics Python's str() for the JSON-derived values that flow through the
// override-resolution logic. The original code compares values via
// str(a) != str(b); reproducing that exactly is required for fidelity.
func pyStr(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case json.Number:
		return t.String()
	default:
		return fmt.Sprint(t)
	}
}

// asList returns the slice if v is a JSON array, else (nil, false).
func asList(v any) ([]any, bool) {
	l, ok := v.([]any)
	return l, ok
}

// asMap returns the map if v is a JSON object, else (nil, false).
func asMap(v any) (map[string]any, bool) {
	m, ok := v.(map[string]any)
	return m, ok
}

// asString returns the string if v is a JSON string, else ("", false).
func asString(v any) (string, bool) {
	s, ok := v.(string)
	return s, ok
}
