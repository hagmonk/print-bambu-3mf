package bambu

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
)

// GetObjectSettings is the port of get_object_settings. The returned map holds
// flat machine/process/filament settings, or an "error" key on failure.
func (e *Extractor) GetObjectSettings(path, objectSelector string, includeGcode bool, plateNumber *int, friendlyNames bool) map[string]any {
	result := map[string]any{}

	a, err := openArchive(path)
	if err != nil {
		result["error"] = err.Error()
		return result
	}
	defer a.Close()

	projectConfig, err := loadProjectConfig(a)
	if err != nil {
		result["error"] = err.Error()
		return result
	}
	embedded := loadEmbeddedPresets(a)

	var objectIndex = -1
	var objectExtruder = 1

	if a.has("Metadata/model_settings.config") {
		data, err := a.read("Metadata/model_settings.config")
		if err != nil {
			result["error"] = err.Error()
			return result
		}
		ms, err := parseModelSettings(data)
		if err != nil {
			result["error"] = err.Error()
			return result
		}

		// Allowed object IDs when a plate is specified.
		var allowed map[string]bool
		if plateNumber != nil {
			allowed = map[string]bool{}
			for _, plate := range ms.Plates {
				pid, ok := metaValue(plate.Metadata, "plater_id")
				if ok && atoiDefault(pid, 0) == *plateNumber {
					for _, inst := range plate.Instances {
						if oid, ok := metaValue(inst.Metadata, "object_id"); ok {
							allowed[oid] = true
						}
					}
				}
			}
		}

		if targetIndex, convErr := strconv.Atoi(objectSelector); convErr == nil {
			idx := 0
			for _, obj := range ms.Objects {
				if allowed != nil && !allowed[obj.ID] {
					idx++
					continue
				}
				if idx == targetIndex {
					objectIndex = idx
					if ev, ok := metaValue(obj.Metadata, "extruder"); ok {
						objectExtruder = atoiDefault(ev, 1)
					}
					break
				}
				idx++
			}
		} else {
			idx := 0
			for _, obj := range ms.Objects {
				name, hasName := metaValue(obj.Metadata, "name")
				if hasName && name == objectSelector {
					if allowed != nil && !allowed[obj.ID] {
						idx++
						continue
					}
					objectIndex = idx
					if ev, ok := metaValue(obj.Metadata, "extruder"); ok {
						objectExtruder = atoiDefault(ev, 1)
					}
					break
				}
				idx++
			}
		}
	}

	if objectIndex == -1 {
		result["error"] = fmt.Sprintf("Object \"%s\" not found", objectSelector)
		return result
	}

	processID, _ := asString(projectConfig["print_settings_id"])
	machineID, _ := asString(projectConfig["printer_settings_id"])
	filamentIDs := stringList(projectConfig["filament_settings_id"])

	filamentSlot := objectExtruder - 1
	if filamentSlot < 0 {
		filamentSlot = 0
	}
	var filamentID string
	if filamentSlot < len(filamentIDs) {
		filamentID = filamentIDs[filamentSlot]
	}

	// Machine
	machineBase := e.mergePresetChain(machineID, "machine", embedded.byName, 0)
	machineSettings := e.applyProjectOverrides(machineBase, projectConfig, "machine", 0, 0, 1)
	if !includeGcode {
		machineSettings = filterGcodeKeys(machineSettings)
	}
	result["machine"] = machineSettings

	// Process
	variantIndex := e.extruderVariantIndex(projectConfig)
	targetPlate := 1
	if plateNumber != nil {
		targetPlate = *plateNumber
	}
	embeddedProcess, hasEmbeddedProcess := embedded.processByPlate[targetPlate]

	var processSettings settings
	if hasEmbeddedProcess {
		embeddedInherits, _ := asString(embeddedProcess["inherits"])
		var processBase settings
		if embeddedInherits != "" {
			processBase = e.mergePresetChain(embeddedInherits, "process", embedded.byName, variantIndex)
		} else {
			processBase = settings{}
		}
		applyEmbeddedProcess(processBase, embeddedProcess, variantIndex)
		processSettings = e.applyDifferingOverrides(processBase, projectConfig, variantIndex, embeddedProcess)
	} else {
		processBase := e.mergePresetChain(processID, "process", embedded.byName, variantIndex)
		processSettings = e.applyProjectOverrides(processBase, projectConfig, "process", variantIndex, 0, 1)
	}

	// Per-object overrides
	objOverrides := parseObjectOverrides(projectConfig, objectIndex)
	for k, v := range objOverrides {
		if !strings.HasPrefix(k, "filament_") {
			processSettings[k] = v
		}
	}
	e.applyFilamentOverridesToProcess(processSettings, projectConfig, filamentSlot, objOverrides)

	if !includeGcode {
		processSettings = filterGcodeKeys(processSettings)
	}
	result["process"] = processSettings

	// Filament
	if filamentID != "" {
		filamentBase := e.mergePresetChain(filamentID, "filament", embedded.byName, filamentSlot)
		filamentSettings := e.applyProjectOverrides(filamentBase, projectConfig, "filament", filamentSlot, 0, 1)
		if !includeGcode {
			filamentSettings = filterGcodeKeys(filamentSettings)
		}
		result["filament"] = filamentSettings
	}

	if friendlyNames {
		if _, hasErr := result["error"]; !hasErr {
			e.applyFriendlyObject(result)
		}
	}
	return result
}

// ---- list mode --------------------------------------------------------------

// ObjInfo describes a single object in list output.
type ObjInfo struct {
	Name     string
	ID       string
	Index    int
	Extruder int
}

// ListResult is the structured result of ListObjects.
type ListResult struct {
	Plates  map[int][]string
	Objects []ObjInfo
}

// ListObjects is the port of list_objects.
func (e *Extractor) ListObjects(path string) ListResult {
	res := ListResult{Plates: map[int][]string{}, Objects: []ObjInfo{}}

	a, err := openArchive(path)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error listing objects: %v\n", err)
		return res
	}
	defer a.Close()

	if a.has("Metadata/model_settings.config") {
		if data, err := a.read("Metadata/model_settings.config"); err == nil {
			if ms, err := parseModelSettings(data); err == nil {
				for i, obj := range ms.Objects {
					name := fmt.Sprintf("object_%s", obj.ID)
					if n, ok := metaValue(obj.Metadata, "name"); ok {
						name = n
					}
					extruder := 1
					if ev, ok := metaValue(obj.Metadata, "extruder"); ok {
						extruder = atoiDefault(ev, 1)
					}
					res.Objects = append(res.Objects, ObjInfo{Name: name, ID: obj.ID, Index: i, Extruder: extruder})
				}
				for _, plate := range ms.Plates {
					pid, ok := metaValue(plate.Metadata, "plater_id")
					if !ok {
						continue
					}
					plateID := atoiDefault(pid, 0)
					names := []string{}
					for _, inst := range plate.Instances {
						oid, ok := metaValue(inst.Metadata, "object_id")
						if !ok {
							continue
						}
						for _, o := range res.Objects {
							if o.ID == oid {
								names = append(names, o.Name)
								break
							}
						}
					}
					res.Plates[plateID] = names
				}
			}
		}
	}

	plateInfo, order := e.getPlateInfo(a)
	for _, plateNum := range order {
		if _, exists := res.Plates[plateNum]; !exists {
			res.Plates[plateNum] = plateInfo[plateNum].objects
		}
	}
	return res
}

type plateInfoEntry struct {
	objects  []string
	objectID string
}

var objNameRe = regexp.MustCompile(`(?s)<object id="(\d+)"[^>]*>.*?<metadata key="name" value="([^"]+)"`)

// getPlateInfo mirrors get_plate_info: it derives one plate per 3dmodel.model
// build item (1-indexed), resolves object names from model_settings (and any
// plate_N.json bbox override), and returns entries keyed by plate number in
// build order.
func (e *Extractor) getPlateInfo(a *archive) (map[int]plateInfoEntry, []int) {
	info := map[int]plateInfoEntry{}
	var order []int

	if a.has("3D/3dmodel.model") {
		if data, err := a.read("3D/3dmodel.model"); err == nil {
			if m, err := parseModel3D(data); err == nil {
				for i, item := range m.Build.Items {
					n := i + 1
					info[n] = plateInfoEntry{objects: []string{}, objectID: item.ObjectID}
					order = append(order, n)
				}
			}
		}
	}

	objectNames := map[string]string{}
	if a.has("Metadata/model_settings.config") {
		if data, err := a.read("Metadata/model_settings.config"); err == nil {
			for _, m := range objNameRe.FindAllStringSubmatch(string(data), -1) {
				objectNames[m[1]] = m[2]
			}
		}
	}

	for _, n := range order {
		entry := info[n]
		if name, ok := objectNames[entry.objectID]; ok {
			entry.objects = append(entry.objects, name)
		}
		info[n] = entry
	}

	// plate_N.json overrides.
	for _, n := range order {
		jsonFile := fmt.Sprintf("Metadata/plate_%d.json", n)
		if !a.has(jsonFile) {
			continue
		}
		data, err := a.read(jsonFile)
		if err != nil {
			continue
		}
		var pj map[string]any
		if err := decodeJSON(data, &pj); err != nil {
			continue
		}
		entry := info[n]
		if bbox, ok := asList(pj["bbox_objects"]); ok {
			entry.objects = []string{}
			for _, o := range bbox {
				name := "unnamed"
				if om, ok := asMap(o); ok {
					if nm, ok := asString(om["name"]); ok {
						name = nm
					}
				}
				entry.objects = append(entry.objects, name)
			}
		}
		info[n] = entry
	}

	return info, order
}
