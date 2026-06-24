package bambu

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
)

var processSettingsRe = regexp.MustCompile(`process_settings_(\d+)\.config`)

// embeddedPresets holds presets embedded in a 3MF, plus process presets indexed
// by plate number.
type embeddedPresets struct {
	byName        map[string]preset
	processByPlate map[int]preset
}

func loadProjectConfig(a *archive) (settings, error) {
	if !a.has("Metadata/project_settings.config") {
		return settings{}, nil
	}
	data, err := a.read("Metadata/project_settings.config")
	if err != nil {
		return settings{}, err
	}
	text := strings.TrimSpace(string(data))
	if !strings.HasPrefix(text, "{") {
		return settings{}, nil
	}
	var cfg settings
	if err := decodeJSON([]byte(text), &cfg); err != nil {
		return nil, err
	}
	return cfg, nil
}

func loadEmbeddedPresets(a *archive) embeddedPresets {
	ep := embeddedPresets{byName: map[string]preset{}, processByPlate: map[int]preset{}}
	for _, name := range a.names() {
		isFilament := strings.Contains(name, "filament_settings") && strings.HasSuffix(name, ".config")
		isProcess := strings.Contains(name, "process_settings") && strings.HasSuffix(name, ".config")
		if !isFilament && !isProcess {
			continue
		}
		data, err := a.read(name)
		if err != nil {
			continue
		}
		text := strings.TrimSpace(string(data))
		if !strings.HasPrefix(text, "{") {
			continue
		}
		var p preset
		if err := decodeJSON([]byte(text), &p); err != nil {
			continue
		}
		presetName := name
		if n, ok := asString(p["name"]); ok {
			presetName = n
		}
		ep.byName[presetName] = p
		if isProcess {
			if m := processSettingsRe.FindStringSubmatch(name); m != nil {
				if pn, err := strconv.Atoi(m[1]); err == nil {
					ep.processByPlate[pn] = p
				}
			}
		}
	}
	return ep
}

// readPrinterModelID mirrors _read_printer_model_id.
func (e *Extractor) readPrinterModelID(a *archive, plateNumber *int) any {
	if !a.has("Metadata/slice_info.config") {
		return nil
	}
	data, err := a.read("Metadata/slice_info.config")
	if err != nil {
		return nil
	}
	si, err := parseSliceInfo(data)
	if err != nil {
		return nil
	}
	for _, plate := range si.Plates {
		if plateNumber != nil {
			idx, ok := metaValue(plate.Metadata, "index")
			if !ok || idx != strconv.Itoa(*plateNumber) {
				continue
			}
		}
		if val, ok := metaValue(plate.Metadata, "printer_model_id"); ok && val != "" {
			return val
		}
	}
	return nil
}

// embeddedProcessApplyToBase applies an embedded process preset's own settings
// onto process_base, selecting array element variantIndex. Mirrors the inline
// loop in the Python embedded-process path.
var embeddedProcessSkip = map[string]bool{
	"name": true, "type": true, "from": true, "inherits": true, "version": true,
	"setting_id": true, "instantiation": true, "compatible_printers": true,
	"compatible_printers_condition": true, "description": true,
	"print_settings_id": true, "print_extruder_id": true, "print_extruder_variant": true,
}

func applyEmbeddedProcess(processBase settings, embeddedProcess preset, variantIndex int) {
	for key, value := range embeddedProcess {
		if embeddedProcessSkip[key] {
			continue
		}
		if list, ok := asList(value); ok {
			switch {
			case len(list) == 1:
				processBase[key] = list[0]
			case len(list) > variantIndex:
				processBase[key] = list[variantIndex]
			case len(list) > 0:
				processBase[key] = list[len(list)-1]
			}
		} else {
			processBase[key] = value
		}
	}
}

// ExtractStructured is the port of extract_structured_from_3mf.
func (e *Extractor) ExtractStructured(path string, resolveInheritance bool, plateNumber *int, includeGcode, friendlyNames bool) map[string]any {
	result := map[string]any{}

	a, err := openArchive(path)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error processing %s: %v\n", path, err)
		return map[string]any{}
	}
	defer a.Close()

	projectConfig, err := loadProjectConfig(a)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error processing %s: %v\n", path, err)
		return map[string]any{}
	}

	modelID := e.readPrinterModelID(a, plateNumber)
	embedded := loadEmbeddedPresets(a)

	processID, _ := asString(projectConfig["print_settings_id"])
	machineID, _ := asString(projectConfig["printer_settings_id"])
	filamentIDs := stringList(projectConfig["filament_settings_id"])

	// Machine
	if machineID != "" {
		machineBase := e.mergePresetChain(machineID, "machine", embedded.byName, 0)
		var machineSettings settings
		if resolveInheritance {
			machineSettings = e.applyProjectOverrides(machineBase, projectConfig, "machine", 0, 0, 1)
		} else {
			machineSettings = machineBase
		}
		if !includeGcode {
			machineSettings = filterGcodeKeys(machineSettings)
		}
		result["machine"] = map[string]any{"id": machineID, "model_id": modelID, "settings": machineSettings}
	}

	// Process
	variantIndex := e.extruderVariantIndex(projectConfig)
	targetPlate := 1
	if plateNumber != nil {
		targetPlate = *plateNumber
	}
	embeddedProcess, hasEmbeddedProcess := embedded.processByPlate[targetPlate]

	var processSettings settings
	if hasEmbeddedProcess && resolveInheritance {
		embeddedInherits, _ := asString(embeddedProcess["inherits"])
		var processBase settings
		if embeddedInherits != "" {
			processBase = e.mergePresetChain(embeddedInherits, "process", embedded.byName, variantIndex)
		} else {
			processBase = settings{}
		}
		applyEmbeddedProcess(processBase, embeddedProcess, variantIndex)
		processSettings = e.applyDifferingOverrides(processBase, projectConfig, variantIndex, embeddedProcess)
		if n, ok := asString(embeddedProcess["name"]); ok {
			processID = n
		}
	} else {
		var processBase settings
		if processID != "" {
			processBase = e.mergePresetChain(processID, "process", embedded.byName, variantIndex)
		} else {
			processBase = settings{}
		}
		if resolveInheritance {
			processSettings = e.applyProjectOverrides(processBase, projectConfig, "process", variantIndex, 0, 1)
		} else if len(processBase) > 0 {
			processSettings = processBase
		} else {
			processSettings = projectConfig
		}
	}
	if !includeGcode {
		processSettings = filterGcodeKeys(processSettings)
	}
	result["process"] = map[string]any{"id": processID, "settings": processSettings}

	// Plates & objects
	plates := map[string]any{}
	usedFilaments := map[string]int{}
	usedOrder := []string{} // preserve first-seen order (value lookup only; output is sorted)
	if a.has("Metadata/model_settings.config") {
		if data, err := a.read("Metadata/model_settings.config"); err == nil {
			if ms, err := parseModelSettings(data); err == nil {
				idToInfo := map[string]objBase{}
				idx := 0
				for _, obj := range ms.Objects {
					name := fmt.Sprintf("object_%s", obj.ID)
					if n, ok := metaValue(obj.Metadata, "name"); ok {
						name = n
					}
					extruder := 1
					if ev, ok := metaValue(obj.Metadata, "extruder"); ok {
						extruder = atoiDefault(ev, 1)
					}
					idToInfo[obj.ID] = objBase{name: name, id: obj.ID, index: idx, extruder: extruder}
					idx++
				}

				for _, plate := range ms.Plates {
					plateIDStr, ok := metaValue(plate.Metadata, "plater_id")
					if !ok {
						continue
					}
					plateID := atoiDefault(plateIDStr, 0)
					if plateNumber != nil && plateID != *plateNumber {
						continue
					}
					plateObjects := []any{}
					for _, inst := range plate.Instances {
						objID, ok := metaValue(inst.Metadata, "object_id")
						if !ok {
							continue
						}
						base, ok := idToInfo[objID]
						if !ok {
							continue
						}
						filamentSlot := base.extruder - 1
						var filamentID any
						if filamentSlot >= 0 && filamentSlot < len(filamentIDs) {
							fid := filamentIDs[filamentSlot]
							filamentID = fid
							if _, seen := usedFilaments[fid]; !seen {
								usedFilaments[fid] = filamentSlot
								usedOrder = append(usedOrder, fid)
							}
						}
						overrides := parseObjectOverrides(projectConfig, base.index)
						if !includeGcode {
							overrides = filterGcodeKeys(overrides)
						}
						plateObjects = append(plateObjects, map[string]any{
							"name":              base.name,
							"id":                base.id,
							"index":             base.index,
							"extruder":          base.extruder,
							"process_id":        processID,
							"filament_id":       filamentID,
							"process_overrides": overrides,
						})
					}
					plates[strconv.Itoa(plateID)] = map[string]any{"objects": plateObjects}
				}
			}
		}
	}
	result["plates"] = plates

	// Filaments (used only, unique by preset id)
	filaments := map[string]any{}
	filamentVariantIdx := e.filamentVariantIndex(projectConfig)
	filamentCount := len(filamentIDs)
	if filamentCount == 0 {
		filamentCount = 1
	}
	for _, fid := range usedOrder {
		slotIndex := usedFilaments[fid]
		filamentBase := e.mergePresetChain(fid, "filament", embedded.byName, filamentVariantIdx)
		var filamentSettings settings
		if resolveInheritance {
			filamentSettings = e.applyProjectOverrides(filamentBase, projectConfig, "filament", slotIndex, filamentVariantIdx, filamentCount)
		} else {
			filamentSettings = filamentBase
		}
		if !includeGcode {
			filamentSettings = filterGcodeKeys(filamentSettings)
		}
		filaments[fid] = map[string]any{"id": fid, "settings": filamentSettings}
	}
	result["filaments"] = filaments

	if friendlyNames {
		e.applyFriendlyStructured(result)
	}
	return result
}

type objBase struct {
	name     string
	id       string
	index    int
	extruder int
}

// stringList coerces a value into a []string. A bare string becomes a
// single-element slice (mirroring the filament_settings_id normalization).
func stringList(v any) []string {
	if s, ok := v.(string); ok {
		return []string{s}
	}
	list, ok := asList(v)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		if s, ok := item.(string); ok {
			out = append(out, s)
		} else {
			out = append(out, pyStr(item))
		}
	}
	return out
}

// ---- friendly-name application ---------------------------------------------

func (e *Extractor) getLabelMapper() *LabelMapper {
	if !e.labelsTried {
		e.labels = getLabelMapper()
		e.labelsTried = true
	}
	return e.labels
}

func (e *Extractor) friendlySettings(s settings) any {
	mapper := e.getLabelMapper()
	if mapper == nil {
		return s
	}
	return mapper.transform(s)
}

func (e *Extractor) applyFriendlyStructured(result map[string]any) {
	if m, ok := result["machine"].(map[string]any); ok {
		if s, ok := m["settings"].(settings); ok {
			m["settings"] = e.friendlySettings(s)
		}
	}
	if p, ok := result["process"].(map[string]any); ok {
		if s, ok := p["settings"].(settings); ok {
			p["settings"] = e.friendlySettings(s)
		}
	}
	if fils, ok := result["filaments"].(map[string]any); ok {
		for _, f := range fils {
			if fm, ok := f.(map[string]any); ok {
				if s, ok := fm["settings"].(settings); ok {
					fm["settings"] = e.friendlySettings(s)
				}
			}
		}
	}
	if plates, ok := result["plates"].(map[string]any); ok {
		for _, pl := range plates {
			pm, ok := pl.(map[string]any)
			if !ok {
				continue
			}
			objs, ok := pm["objects"].([]any)
			if !ok {
				continue
			}
			for _, o := range objs {
				om, ok := o.(map[string]any)
				if !ok {
					continue
				}
				if ov, ok := om["process_overrides"].(settings); ok {
					normalized := normalizeOverrides(ov)
					om["process_overrides"] = e.friendlySettings(normalized)
				}
			}
		}
	}
}

func (e *Extractor) applyFriendlyObject(result map[string]any) {
	for _, key := range []string{"machine", "process", "filament"} {
		if s, ok := result[key].(settings); ok {
			result[key] = e.friendlySettings(s)
		}
	}
}
