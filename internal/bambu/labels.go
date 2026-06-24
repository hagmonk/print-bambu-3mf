package bambu

import (
	_ "embed"
	"regexp"
	"sort"
	"strings"
	"sync"
)

//go:embed labels.json
var labelsJSON []byte

// labelMetadata is the parsed labels.json: key -> attribute map. Attribute
// values are dynamic (strings, the enum_map object, or the scope list/string).
var (
	labelMetaOnce sync.Once
	labelMeta     map[string]map[string]any
	labelMetaErr  error
)

func getLabelMetadata() (map[string]map[string]any, error) {
	labelMetaOnce.Do(func() {
		var raw map[string]map[string]any
		if err := decodeJSON(labelsJSON, &raw); err != nil {
			labelMetaErr = err
			return
		}
		labelMeta = raw
	})
	return labelMeta, labelMetaErr
}

// getScopeForKey returns the scope(s) declared for a key, normalized to a
// string slice (nil when absent). Scope in labels.json is either a string or a
// list of strings.
func getScopeForKey(key string) []string {
	meta, err := getLabelMetadata()
	if err != nil {
		return nil
	}
	entry, ok := meta[key]
	if !ok {
		return nil
	}
	switch s := entry["scope"].(type) {
	case string:
		return []string{s}
	case []any:
		out := make([]string, 0, len(s))
		for _, v := range s {
			if str, ok := v.(string); ok {
				out = append(out, str)
			}
		}
		return out
	default:
		return nil
	}
}

// ---- LabelMapper ------------------------------------------------------------

var categoryOrder = []string{
	"Quality", "Strength", "Speed", "Support", "Others",
	"Extruders", "Machine limits", "Cooling", "Advanced",
}

// LabelMapper transforms raw preset keys into friendly, grouped labels.
type LabelMapper struct {
	mapping       map[string]map[string]any
	categoryIndex map[string]int
}

// getLabelMapper returns a process-wide LabelMapper, or nil if labels.json is
// unavailable (mirrors the Python FileNotFoundError fallback to raw keys).
var (
	labelMapperOnce sync.Once
	labelMapper     *LabelMapper
)

func getLabelMapper() *LabelMapper {
	labelMapperOnce.Do(func() {
		meta, err := getLabelMetadata()
		if err != nil || meta == nil {
			return
		}
		idx := map[string]int{}
		for i, name := range categoryOrder {
			idx[name] = i
		}
		labelMapper = &LabelMapper{mapping: meta, categoryIndex: idx}
	})
	return labelMapper
}

func entryStr(entry map[string]any, key string) (string, bool) {
	if entry == nil {
		return "", false
	}
	s, ok := entry[key].(string)
	return s, ok && s != ""
}

func (m *LabelMapper) friendlyLabel(key string) string {
	entry := m.mapping[key]
	for _, cand := range []string{"friendly", "full_label", "label"} {
		if v, ok := entryStr(entry, cand); ok {
			return v
		}
	}
	return fallbackLabel(key)
}

// transform converts a flat settings map into the friendly (possibly
// category-grouped) structure. Mirrors LabelMapper.transform.
func (m *LabelMapper) transform(s settings) map[string]any {
	structured := map[string]map[string]any{} // category ("" == None) -> tree
	hasCategory := false

	// Stable iteration only affects which value wins on duplicate labels; the
	// Python relies on dict order, but within one settings map keys are unique
	// and labels collide only across distinct keys mapping to the same label.
	// Sort keys for deterministic behavior.
	keys := make([]string, 0, len(s))
	for k := range s {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	for _, key := range keys {
		value := s[key]
		entry := m.mapping[key]
		label := ""
		for _, cand := range []string{"friendly", "full_label", "label"} {
			if v, ok := entryStr(entry, cand); ok {
				label = v
				break
			}
		}
		if label == "" {
			label = fallbackLabel(key)
		}
		category, _ := entryStr(entry, "category")
		group, _ := entryStr(entry, "group")
		section, _ := entryStr(entry, "section")
		friendlyValue := m.friendlyValue(value, entry)

		if category != "" {
			hasCategory = true
		}
		container := ensureContainer(structured, category, group, section)
		container[label] = friendlyValue
	}

	if hasCategory {
		result := map[string]any{}
		for _, category := range m.orderedCategories(structured) {
			if category == "" {
				continue
			}
			result[category] = sortNested(structured[category])
		}
		if none, ok := structured[""]; ok && len(none) > 0 {
			result["Other"] = sortNested(none)
		}
		return result
	}
	if none, ok := structured[""]; ok {
		return sortNested(none).(map[string]any)
	}
	return map[string]any{}
}

func ensureContainer(structured map[string]map[string]any, category, group, section string) map[string]any {
	bucket, ok := structured[category]
	if !ok {
		bucket = map[string]any{}
		structured[category] = bucket
	}
	container := bucket
	if group != "" {
		next, ok := container[group].(map[string]any)
		if !ok {
			next = map[string]any{}
			container[group] = next
		}
		container = next
	}
	if section != "" {
		next, ok := container[section].(map[string]any)
		if !ok {
			next = map[string]any{}
			container[section] = next
		}
		container = next
	}
	return container
}

func (m *LabelMapper) orderedCategories(structured map[string]map[string]any) []string {
	keys := make([]string, 0, len(structured))
	for k := range structured {
		keys = append(keys, k)
	}
	sort.SliceStable(keys, func(i, j int) bool {
		ki, kj := keys[i], keys[j]
		// None (empty) sorts last.
		ni := boolToInt(ki == "")
		nj := boolToInt(kj == "")
		if ni != nj {
			return ni < nj
		}
		ii := m.categoryRank(ki)
		ij := m.categoryRank(kj)
		if ii != ij {
			return ii < ij
		}
		return ki < kj
	})
	return keys
}

func (m *LabelMapper) categoryRank(cat string) int {
	if cat == "" {
		return len(m.categoryIndex)
	}
	if r, ok := m.categoryIndex[cat]; ok {
		return r
	}
	return len(m.categoryIndex)
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// sortNested recursively returns a copy with map keys implicitly sorted on
// marshal. (Go marshals map keys sorted, so this just deep-copies maps; scalar
// and slice values pass through.)
func sortNested(data any) any {
	if m, ok := data.(map[string]any); ok {
		out := make(map[string]any, len(m))
		for k, v := range m {
			out[k] = sortNested(v)
		}
		return out
	}
	return data
}

func (m *LabelMapper) friendlyValue(value any, entry map[string]any) any {
	var enumMap map[string]any
	if entry != nil {
		enumMap, _ = entry["enum_map"].(map[string]any)
	}
	sidetext, _ := entryStr(entry, "sidetext")

	addUnit := func(res any) any {
		if sidetext == "" {
			return res
		}
		// res in (None, "", "nil") -> unchanged
		if res == nil {
			return res
		}
		if s, ok := res.(string); ok && (s == "" || s == "nil") {
			return res
		}
		text := pyStr(res)
		if strings.Contains(text, sidetext) || strings.Contains(text, "%") {
			return text
		}
		return text + " " + sidetext
	}

	if len(enumMap) == 0 {
		if list, ok := asList(value); ok {
			out := make([]any, len(list))
			for i, item := range list {
				out[i] = addUnit(item)
			}
			return out
		}
		return addUnit(value)
	}

	translate := func(item any) any {
		key := pyStr(item)
		label := enumLookup(enumMap, key, item)
		if label == "" {
			return item
		}
		if label != key {
			return label + " (" + key + ")"
		}
		return label
	}

	if list, ok := asList(value); ok {
		out := make([]any, len(list))
		for i, item := range list {
			out[i] = addUnit(translate(item))
		}
		return out
	}
	return addUnit(translate(value))
}

// enumLookup mirrors `enum_map.get(str(item)) or enum_map.get(item)`. Keys in a
// JSON object are always strings, so the second lookup only matches when item is
// itself a string equal to the stringified form (already covered); we keep the
// string-key lookup which is the effective behavior.
func enumLookup(enumMap map[string]any, key string, item any) string {
	if v, ok := enumMap[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

var fallbackSplit = regexp.MustCompile(`[_/]+`)

func fallbackLabel(key string) string {
	parts := fallbackSplit.Split(key, -1)
	var words []string
	for _, part := range parts {
		if part == "" {
			continue
		}
		if isAllUpper(part) || isAllDigits(part) {
			words = append(words, part)
		} else {
			words = append(words, capitalizeFirst(part))
		}
	}
	if len(words) == 0 {
		return key
	}
	return strings.Join(words, " ")
}

func isAllUpper(s string) bool {
	hasLetter := false
	for _, r := range s {
		if r >= 'a' && r <= 'z' {
			return false
		}
		if r >= 'A' && r <= 'Z' {
			hasLetter = true
		}
	}
	return hasLetter
}

// capitalizeFirst mirrors Python str.capitalize(): first char upper, rest lower.
func capitalizeFirst(s string) string {
	if s == "" {
		return s
	}
	lower := strings.ToLower(s)
	return strings.ToUpper(lower[:1]) + lower[1:]
}
