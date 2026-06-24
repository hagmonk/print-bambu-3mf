// Package bambu extracts printer, process, and filament profiles from Bambu
// Studio 3MF files with full preset-inheritance resolution and per-object
// overrides. It is a faithful Go port of the original Python implementation;
// output is intended to be byte-identical.
package bambu

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// settings is a flat key/value bag of resolved config (values are dynamic JSON).
type settings = map[string]any

// preset is a raw profile document loaded from disk or embedded in a 3MF.
type preset = map[string]any

// Extractor resolves presets against the local Bambu Studio profile directory.
// A fresh Extractor should be used per logical extraction (the profile cache is
// not invalidated), matching the Python class lifecycle.
type Extractor struct {
	userDir    string
	systemDir  string
	cache      map[string]preset
	labels     *LabelMapper
	labelsTried bool
	scopeCache map[string][]string
}

// filamentOverrideMap maps filament-scoped override keys to their corresponding
// process keys.
var filamentOverrideMap = map[string]string{
	"filament_retraction_length":           "retraction_length",
	"filament_retraction_speed":            "retraction_speed",
	"filament_deretraction_speed":          "deretraction_speed",
	"filament_retract_restart_extra":       "retract_restart_extra",
	"filament_retraction_minimum_travel":   "retraction_minimum_travel",
	"filament_retract_before_wipe":         "retract_before_wipe",
	"filament_retract_when_changing_layer": "retract_when_changing_layer",
	"filament_wipe":                        "wipe",
	"filament_wipe_distance":               "wipe_distance",
	"filament_z_hop":                       "z_hop",
	"filament_z_hop_types":                 "z_hop_types",
	"filament_retract_lift_above":          "retract_lift_above",
	"filament_retract_lift_below":          "retract_lift_below",
	"filament_long_retractions_when_cut":   "long_retractions_when_cut",
	"filament_retraction_distances_when_cut": "retraction_distances_when_cut",
}

// NewExtractor builds an Extractor rooted at the default macOS Bambu Studio
// directory, or at the directory named by BAMBU_PROFILE_DIR when set. The
// override exists for hermetic testing and non-default installs; default
// behavior is unchanged.
func NewExtractor() *Extractor {
	root := os.Getenv("BAMBU_PROFILE_DIR")
	if root == "" {
		home, _ := os.UserHomeDir()
		root = filepath.Join(home, "Library", "Application Support", "BambuStudio")
	}
	return &Extractor{
		userDir:    filepath.Join(root, "user"),
		systemDir:  filepath.Join(root, "system", "BBL"),
		cache:      map[string]preset{},
		scopeCache: map[string][]string{},
	}
}

// ---- scope resolution -------------------------------------------------------

func (e *Extractor) scopesForKey(key string) []string {
	cacheKey := strings.ToLower(key)
	if cached, ok := e.scopeCache[cacheKey]; ok {
		return cached
	}
	scope := getScopeForKey(key)
	if scope == nil && cacheKey != key {
		scope = getScopeForKey(cacheKey)
	}
	normalized := scope // already []string or nil
	e.scopeCache[cacheKey] = normalized
	return normalized
}

func (e *Extractor) scopeMatches(key, target string, baseSettings settings) bool {
	scopes := e.scopesForKey(key)
	if len(scopes) > 0 {
		for _, s := range scopes {
			if s == target {
				return true
			}
		}
		return false
	}
	if _, ok := baseSettings[key]; ok {
		return true
	}
	return e.inferScopeFromKey(key) == target
}

var machinePrefixes = []string{
	"printer_", "machine_", "extruder_", "max_print_", "printable_",
	"scan_", "head_", "z_offset", "bed_", "gcode_", "chamber_",
}

func (e *Extractor) inferScopeFromKey(key string) string {
	kl := strings.ToLower(key)
	if _, ok := filamentOverrideMap[kl]; ok || strings.HasPrefix(kl, "filament_") {
		return "filament"
	}
	for _, p := range machinePrefixes {
		if strings.HasPrefix(kl, p) {
			return "machine"
		}
	}
	return "process"
}

// ---- variant index resolution ----------------------------------------------

func (e *Extractor) extruderVariantIndex(projectConfig map[string]any) int {
	nozzle, _ := asList(projectConfig["nozzle_volume_type"])
	variants, _ := asList(projectConfig["print_extruder_variant"])
	if len(nozzle) == 0 || len(variants) == 0 {
		return 0
	}
	active, _ := asString(nozzle[0])
	if active == "" {
		return 0
	}
	activeLower := strings.ToLower(active)
	for i, v := range variants {
		vs, _ := asString(v)
		if strings.Contains(strings.ToLower(vs), activeLower) {
			return i
		}
	}
	return 0
}

func (e *Extractor) filamentVariantIndex(projectConfig map[string]any) int {
	nozzle, _ := asList(projectConfig["nozzle_volume_type"])
	filVariant, _ := asList(projectConfig["filament_extruder_variant"])
	if len(nozzle) == 0 || len(filVariant) == 0 {
		return 0
	}
	settingsIDs, _ := asList(projectConfig["filament_settings_id"])
	filamentCount := len(settingsIDs)
	if filamentCount < 1 {
		filamentCount = 1
	}
	stride := len(filVariant) / filamentCount
	if stride < 1 {
		stride = 1
	}
	activeS, _ := asString(nozzle[0])
	active := strings.ToLower(activeS)
	limit := stride
	if len(filVariant) < limit {
		limit = len(filVariant)
	}
	for i := 0; i < limit; i++ {
		vs, _ := asString(filVariant[i])
		if strings.Contains(strings.ToLower(vs), active) {
			return i
		}
	}
	return 0
}

func resolveFilamentArrayValue(value []any, slotIndex, variantIndex, filamentCount int) any {
	if len(value) == 0 {
		return nil
	}
	if len(value) == 1 {
		return value[0]
	}
	if filamentCount < 1 {
		filamentCount = 1
	}
	if len(value) == filamentCount {
		if slotIndex < len(value) {
			return value[slotIndex]
		}
		return value[len(value)-1]
	}
	if len(value)%filamentCount == 0 {
		stride := len(value) / filamentCount
		rel := variantIndex
		if rel > stride-1 {
			rel = stride - 1
		}
		absIdx := slotIndex*stride + rel
		if absIdx < len(value) {
			return value[absIdx]
		}
	}
	if slotIndex < len(value) {
		return value[slotIndex]
	}
	return value[len(value)-1]
}

// ---- profile loading --------------------------------------------------------

func (e *Extractor) findUserID() string {
	entries, err := os.ReadDir(e.userDir)
	if err != nil {
		return ""
	}
	for _, ent := range entries {
		if ent.IsDir() && isAllDigits(ent.Name()) {
			return ent.Name()
		}
	}
	return ""
}

func isAllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// loadProfile loads a profile by name and type from user (incl. base/) then
// system (incl. base/) directories. Returns nil if not found. Only successful
// loads are cached, matching the Python behavior.
func (e *Extractor) loadProfile(profileName, profileType string) preset {
	cacheKey := profileType + ":" + profileName
	if p, ok := e.cache[cacheKey]; ok {
		return p
	}
	name := strings.ReplaceAll(profileName, ".json", "")

	var candidates []string
	if userID := e.findUserID(); userID != "" {
		dir := filepath.Join(e.userDir, userID, profileType)
		candidates = append(candidates,
			filepath.Join(dir, name+".json"),
			filepath.Join(dir, "base", name+".json"),
		)
	}
	sysDir := filepath.Join(e.systemDir, profileType)
	candidates = append(candidates,
		filepath.Join(sysDir, name+".json"),
		filepath.Join(sysDir, "base", name+".json"),
	)

	for _, path := range candidates {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var p preset
		if err := decodeJSON(data, &p); err != nil {
			continue
		}
		e.cache[cacheKey] = p
		return p
	}
	return nil
}

// presetMetadataSkip are keys excluded when flattening a preset's own settings
// in merge_preset_chain.
var presetMetadataSkip = map[string]bool{
	"name": true, "type": true, "from": true, "inherits": true,
	"version": true, "setting_id": true, "instantiation": true,
	"compatible_printers": true, "compatible_printers_condition": true,
	"description": true,
}

// mergePresetChain recursively merges a preset inheritance chain into a flat
// key/value map, selecting array element `index` for the active slot/extruder.
func (e *Extractor) mergePresetChain(presetName, presetType string, embedded map[string]preset, index int) settings {
	merged := settings{}
	if presetName == "" {
		return merged
	}

	var p preset
	if ep, ok := embedded[presetName]; ok {
		p = ep
	} else {
		p = e.loadProfile(presetName, presetType)
	}
	if p == nil {
		return merged
	}

	if inh, ok := asString(p["inherits"]); ok && inh != "" {
		parent := e.mergePresetChain(inh, presetType, embedded, index)
		for k, v := range parent {
			merged[k] = v
		}
	}

	for key, value := range p {
		if presetMetadataSkip[key] {
			continue
		}
		if list, ok := asList(value); ok {
			merged[key] = indexArray(list, index)
		} else {
			merged[key] = value
		}
	}
	return merged
}

// indexArray mirrors Python's per-index array element selection:
// len==1 -> element 0; index in range -> that element; else last (or nil).
func indexArray(list []any, index int) any {
	switch {
	case len(list) == 1:
		return list[0]
	case index < len(list):
		return list[index]
	case len(list) > 0:
		return list[len(list)-1]
	default:
		return nil
	}
}

// applyProjectOverridesSkip are metadata keys skipped in apply_project_overrides.
var applyProjectOverridesSkip = map[string]bool{
	"name": true, "from": true, "version": true, "filament_settings_id": true,
	"print_settings_id": true, "printer_settings_id": true, "compatible_printers": true,
	"different_settings_to_system": true,
}

// applyProjectOverrides applies project_settings.config values onto base
// settings, scoped to setting_type. For filament scope, list layout may be
// per-slot or per-(slot×variant); variantIndex selects within a slot.
func (e *Extractor) applyProjectOverrides(base, projectConfig settings, settingType string, index, variantIndex, filamentCount int) settings {
	result := copySettings(base)
	for key, value := range projectConfig {
		if applyProjectOverridesSkip[key] {
			continue
		}
		if !e.scopeMatches(key, settingType, base) {
			continue
		}
		if list, ok := asList(value); ok {
			if settingType == "filament" {
				resolved := resolveFilamentArrayValue(list, index, variantIndex, filamentCount)
				if resolved != nil {
					result[key] = resolved
				}
			} else if index < len(list) {
				result[key] = list[index]
			} else if len(list) > 0 {
				result[key] = list[len(list)-1]
			}
		} else {
			result[key] = value
		}
	}
	return result
}

// applyDifferingOverridesSkip are metadata keys skipped in the differing-override pass.
var applyDifferingOverridesSkip = map[string]bool{
	"name": true, "from": true, "version": true, "filament_settings_id": true,
	"print_settings_id": true, "printer_settings_id": true, "compatible_printers": true,
	"different_settings_to_system": true, "inherits": true, "setting_id": true,
	"print_extruder_id": true, "print_extruder_variant": true, "nozzle_volume_type": true,
	"extruder_variant_list": true, "filament_extruder_variant": true, "printer_extruder_variant": true,
}

// applyDifferingOverrides selectively applies project_config process values to
// avoid stale cached values while keeping intentional user overrides. Mirrors
// _apply_differing_overrides precisely.
func (e *Extractor) applyDifferingOverrides(inherited, projectConfig settings, variantIndex int, embedded preset) settings {
	result := copySettings(inherited)
	if embedded == nil {
		embedded = preset{}
	}

	for key, projValue := range projectConfig {
		if applyDifferingOverridesSkip[key] {
			continue
		}
		if !e.scopeMatches(key, "process", inherited) {
			continue
		}

		var effective any
		isMultiElement := false
		if list, ok := asList(projValue); ok {
			switch {
			case len(list) == 0:
				continue
			case len(list) == 1:
				effective = list[0]
				isMultiElement = false
			case len(list) > variantIndex:
				effective = list[variantIndex]
				isMultiElement = true
			default:
				effective = list[len(list)-1]
				isMultiElement = true
			}
		} else {
			effective = projValue
			isMultiElement = false
		}

		if isMultiElement {
			result[key] = effective
			continue
		}

		embeddedValue, hasEmbedded := embedded[key]
		if hasEmbedded && embeddedValue != nil {
			if pyStr(effective) != pyStr(embeddedValue) {
				result[key] = effective
			}
		}
	}
	return result
}

// resolveIndexedValue returns a scalar from a possibly-indexed project setting.
func resolveIndexedValue(value any, index int) any {
	if value == nil {
		return nil
	}
	if list, ok := asList(value); ok {
		if len(list) == 0 {
			return nil
		}
		if len(list) == 1 {
			return list[0]
		}
		if index < len(list) {
			return list[index]
		}
		return list[len(list)-1]
	}
	return value
}

// applyFilamentOverridesToProcess merges filament override values into process
// settings for the active extruder.
func (e *Extractor) applyFilamentOverridesToProcess(processSettings settings, projectConfig settings, filamentIndex int, objectOverrides map[string]any) {
	for filamentKey, processKey := range filamentOverrideMap {
		var raw any
		if objectOverrides != nil {
			raw = objectOverrides[filamentKey]
		}
		if raw == nil {
			raw = resolveIndexedValue(projectConfig[filamentKey], filamentIndex)
		}
		if raw == nil {
			continue
		}
		if s, ok := raw.(string); ok && strings.ToLower(strings.TrimSpace(s)) == "nil" {
			continue
		}
		processSettings[processKey] = raw
	}
}

// normalizeOverrides renames filament override keys to their process keys.
func normalizeOverrides(overrides map[string]any) map[string]any {
	normalized := map[string]any{}
	for key, value := range overrides {
		if pk, ok := filamentOverrideMap[key]; ok {
			normalized[pk] = value
		} else {
			normalized[key] = value
		}
	}
	return normalized
}

// parseObjectOverrides parses per-object setting overrides from
// different_settings_to_system for the given object index.
func parseObjectOverrides(projectConfig settings, objectIndex int) map[string]any {
	overrides := map[string]any{}
	diff, _ := asList(projectConfig["different_settings_to_system"])
	if objectIndex >= len(diff) {
		return overrides
	}
	overrideStr, _ := asString(diff[objectIndex])
	if overrideStr == "" {
		return overrides
	}
	for _, key := range strings.Split(overrideStr, ";") {
		key = strings.TrimSpace(key)
		value, ok := projectConfig[key]
		if !ok {
			continue
		}
		if list, ok := asList(value); ok {
			if objectIndex < len(list) {
				overrides[key] = list[objectIndex]
			} else if len(list) > 0 {
				overrides[key] = list[len(list)-1]
			}
		} else {
			overrides[key] = value
		}
	}
	return overrides
}

// filterGcodeKeys removes keys containing "gcode" (case-insensitive).
func filterGcodeKeys(s settings) settings {
	out := settings{}
	for k, v := range s {
		if !strings.Contains(strings.ToLower(k), "gcode") {
			out[k] = v
		}
	}
	return out
}

func copySettings(s settings) settings {
	out := make(settings, len(s))
	for k, v := range s {
		out[k] = v
	}
	return out
}

// atoiDefault parses s as an int, returning def on failure.
func atoiDefault(s string, def int) int {
	if n, err := strconv.Atoi(strings.TrimSpace(s)); err == nil {
		return n
	}
	return def
}
