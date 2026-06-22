"""
Extract printer, process, and filament profiles from Bambu Studio 3MF files
with proper inheritance resolution and per-object overrides
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from .labels import get_label_mapper, get_scope_for_key


class BambuProfileExtractor:
    def __init__(self):
        self.user_dir = Path.home() / "Library" / "Application Support" / "BambuStudio" / "user"
        self.system_dir = Path.home() / "Library" / "Application Support" / "BambuStudio" / "system" / "BBL"
        self.profiles_cache = {}
        self._label_mapper = None
        self._scope_cache: Dict[str, tuple[str, ...]] = {}

        # Mapping of filament override keys to their corresponding process keys.
        self.filament_override_map = {
            'filament_retraction_length': 'retraction_length',
            'filament_retraction_speed': 'retraction_speed',
            'filament_deretraction_speed': 'deretraction_speed',
            'filament_retract_restart_extra': 'retract_restart_extra',
            'filament_retraction_minimum_travel': 'retraction_minimum_travel',
            'filament_retract_before_wipe': 'retract_before_wipe',
            'filament_retract_when_changing_layer': 'retract_when_changing_layer',
            'filament_wipe': 'wipe',
            'filament_wipe_distance': 'wipe_distance',
            'filament_z_hop': 'z_hop',
            'filament_z_hop_types': 'z_hop_types',
            'filament_retract_lift_above': 'retract_lift_above',
            'filament_retract_lift_below': 'retract_lift_below',
            'filament_long_retractions_when_cut': 'long_retractions_when_cut',
            'filament_retraction_distances_when_cut': 'retraction_distances_when_cut',
        }

    def _get_label_mapper(self):
        if self._label_mapper is None:
            try:
                self._label_mapper = get_label_mapper()
            except FileNotFoundError:
                self._label_mapper = False
        return self._label_mapper if self._label_mapper else None

    def _friendly_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        mapper = self._get_label_mapper()
        if not mapper or not isinstance(settings, dict):
            return settings
        return mapper.transform(settings)

    def _apply_friendly_structured(self, result: Dict[str, Any]) -> None:
        if 'machine' in result and 'settings' in result['machine']:
            result['machine']['settings'] = self._friendly_settings(result['machine']['settings'])
        if 'process' in result and 'settings' in result['process']:
            result['process']['settings'] = self._friendly_settings(result['process']['settings'])
        if 'filaments' in result:
            for filament in result['filaments'].values():
                if isinstance(filament, dict) and 'settings' in filament:
                    filament['settings'] = self._friendly_settings(filament['settings'])
        for plate in result.get('plates', {}).values():
            objects = plate.get('objects', []) if isinstance(plate, dict) else []
            for obj in objects:
                if isinstance(obj, dict) and 'process_overrides' in obj:
                    normalized = self._normalize_overrides(obj['process_overrides'])
                    obj['process_overrides'] = self._friendly_settings(normalized)

    def _apply_friendly_object(self, result: Dict[str, Any]) -> None:
        for key in ('machine', 'process', 'filament'):
            if key in result and isinstance(result[key], dict):
                result[key] = self._friendly_settings(result[key])

    def _scope_matches(self, key: str, target: str, base_settings: Dict[str, Any]) -> bool:
        scopes = self._scopes_for_key(key)
        if scopes:
            return target in scopes
        if key in base_settings:
            return True
        inferred = self._infer_scope_from_key(key)
        return inferred == target

    def _scopes_for_key(self, key: str) -> tuple[str, ...]:
        cache_key = key.lower()
        cached = self._scope_cache.get(cache_key)
        if cached is not None:
            return cached

        scope = get_scope_for_key(key)
        if scope is None:
            if cache_key != key:
                scope = get_scope_for_key(cache_key)

        if scope is None:
            normalized: tuple[str, ...] = ()
        elif isinstance(scope, str):
            normalized = (scope,)
        else:
            normalized = tuple(scope)

        self._scope_cache[cache_key] = normalized
        return normalized

    def _infer_scope_from_key(self, key: str) -> str:
        key_lower = key.lower()
        if key_lower in self.filament_override_map or key_lower.startswith('filament_'):
            return 'filament'

        machine_prefixes = (
            'printer_',
            'machine_',
            'extruder_',
            'max_print_',
            'printable_',
            'scan_',
            'head_',
            'z_offset',
            'bed_',
            'gcode_',
            'chamber_',
        )
        if key_lower.startswith(machine_prefixes):
            return 'machine'

        return 'process'

    def _get_extruder_variant_index(self, project_config: Dict[str, Any]) -> int:
        """Determine the active extruder variant index from project config.

        Many process settings are stored as arrays indexed by extruder variant
        (e.g., 'Direct Drive Standard' at index 0, 'Direct Drive High Flow' at index 1).
        This method detects which variant is active based on nozzle_volume_type.
        """
        nozzle_volume_type = project_config.get('nozzle_volume_type', [])
        print_extruder_variant = project_config.get('print_extruder_variant', [])

        if not nozzle_volume_type or not print_extruder_variant:
            return 0

        # Get the active nozzle type (e.g., "High Flow", "Standard")
        active_nozzle = nozzle_volume_type[0] if nozzle_volume_type else None
        if not active_nozzle:
            return 0

        # Find which variant index matches this nozzle type
        # "High Flow" should match "Direct Drive High Flow"
        # "Standard" should match "Direct Drive Standard"
        active_nozzle_lower = active_nozzle.lower()
        for i, variant in enumerate(print_extruder_variant):
            if active_nozzle_lower in variant.lower():
                return i

        return 0

    def _resolve_filament_variant_index(self, project_config: Dict[str, Any]) -> int:
        """Return the variant offset within a single filament's stride.

        Per-(filament × variant) arrays in project_config are laid out as
        contiguous strides — each filament occupies len(filament_extruder_variant)
        / len(filament_settings_id) entries, and within that stride entries follow
        the order in filament_extruder_variant. This returns the index within
        that stride matching the active nozzle volume type (e.g. 'High Flow').
        """
        nozzle_volume_type = project_config.get('nozzle_volume_type') or []
        filament_extruder_variant = project_config.get('filament_extruder_variant') or []
        if not nozzle_volume_type or not filament_extruder_variant:
            return 0

        filament_count = max(1, len(project_config.get('filament_settings_id') or []))
        stride = max(1, len(filament_extruder_variant) // filament_count)

        active = nozzle_volume_type[0].lower()
        for i in range(min(stride, len(filament_extruder_variant))):
            if active in filament_extruder_variant[i].lower():
                return i
        return 0

    def _resolve_filament_array_value(
        self,
        value: List[Any],
        slot_index: int,
        variant_index: int,
        filament_count: int,
    ) -> Any:
        """Pick the right element from a project-level filament array.

        Arrays come in three flavours: length 1 (shared), length == filament_count
        (one per slot), or length == filament_count * stride (per-(slot × variant)).
        """
        if not value:
            return None
        if len(value) == 1:
            return value[0]
        filament_count = max(1, filament_count)
        if len(value) == filament_count:
            return value[slot_index] if slot_index < len(value) else value[-1]
        if len(value) % filament_count == 0:
            stride = len(value) // filament_count
            rel = min(variant_index, stride - 1)
            abs_idx = slot_index * stride + rel
            if abs_idx < len(value):
                return value[abs_idx]
        return value[slot_index] if slot_index < len(value) else value[-1]

    def _read_printer_model_id(
        self,
        zf: zipfile.ZipFile,
        file_list: List[str],
        plate_number: Optional[int],
    ) -> Optional[str]:
        if 'Metadata/slice_info.config' not in file_list:
            return None
        try:
            tree = ET.fromstring(zf.read('Metadata/slice_info.config'))
        except ET.ParseError:
            return None

        for plate_elem in tree.findall('.//plate'):
            if plate_number is not None:
                idx_elem = plate_elem.find("./metadata[@key='index']")
                if idx_elem is None or idx_elem.get('value') != str(plate_number):
                    continue
            value_elem = plate_elem.find("./metadata[@key='printer_model_id']")
            if value_elem is not None:
                value = value_elem.get('value')
                if value:
                    return value
        return None

    def _normalize_overrides(self, overrides: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(overrides, dict):
            return overrides
        normalized: Dict[str, Any] = {}
        for key, value in overrides.items():
            process_key = self.filament_override_map.get(key, key)
            normalized[process_key] = value
        return normalized

    def _apply_differing_overrides(
        self,
        inherited_settings: Dict[str, Any],
        project_config: Dict[str, Any],
        variant_index: int,
        embedded_preset: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Apply project config overrides selectively to avoid stale cached values.

        The logic handles two types of staleness:
        1. Stale project values from a previous preset (e.g., layer_height: 0.3 when
           current preset inherits 0.2) - these match neither inheritance nor embedded
        2. Fresh user overrides made after saving the embedded preset (e.g., changing
           top_surface_pattern from concentric to monotonic)

        We apply project_config values when:
        - It's a multi-element array (per-variant setting explicitly configured)
        - OR the value differs from the embedded preset (user changed it after saving)

        We skip project_config values when:
        - It's a scalar that matches the inherited value (just a cached copy)
        - It's a scalar that doesn't exist in embedded AND differs from inheritance
          (likely stale from a previous preset)
        """
        result = inherited_settings.copy()
        embedded = embedded_preset or {}

        # Metadata keys to skip
        skip_keys = {
            'name', 'from', 'version', 'filament_settings_id',
            'print_settings_id', 'printer_settings_id', 'compatible_printers',
            'different_settings_to_system', 'inherits', 'setting_id',
            'print_extruder_id', 'print_extruder_variant', 'nozzle_volume_type',
            'extruder_variant_list', 'filament_extruder_variant', 'printer_extruder_variant'
        }

        for key, proj_value in project_config.items():
            if key in skip_keys:
                continue

            # Only consider process-scoped settings
            if not self._scope_matches(key, 'process', inherited_settings):
                continue

            # Extract effective value from project_config
            if isinstance(proj_value, list):
                if len(proj_value) == 0:
                    continue
                elif len(proj_value) == 1:
                    effective_proj_value = proj_value[0]
                    is_multi_element_array = False
                elif len(proj_value) > variant_index:
                    effective_proj_value = proj_value[variant_index]
                    is_multi_element_array = True
                else:
                    effective_proj_value = proj_value[-1]
                    is_multi_element_array = True
            else:
                effective_proj_value = proj_value
                is_multi_element_array = False

            # Always apply multi-element arrays (per-variant settings)
            if is_multi_element_array:
                result[key] = effective_proj_value
                continue

            # For scalars, check if embedded preset has this key
            embedded_value = embedded.get(key)
            if embedded_value is not None:
                # Embedded preset explicitly sets this key
                # Apply project_config if it differs from embedded (user changed after saving)
                if str(effective_proj_value) != str(embedded_value):
                    result[key] = effective_proj_value
                # If they match, keep the inherited value (embedded might be stale too)
            # If embedded doesn't have the key, keep the inherited value
            # (project_config scalar is likely stale from previous preset)

        return result

    def find_user_id(self) -> Optional[str]:
        """Find the user ID directory"""
        if self.user_dir.exists():
            for path in self.user_dir.iterdir():
                if path.is_dir() and path.name.isdigit():
                    return path.name
        return None

    def load_profile(self, profile_name: str, profile_type: str = 'filament') -> Optional[Dict]:
        """Load a profile from user or system directory"""
        cache_key = f"{profile_type}:{profile_name}"
        if cache_key in self.profiles_cache:
            return self.profiles_cache[cache_key]

        profile_name = profile_name.replace('.json', '')

        # Try user directory first (including base subdirectory)
        user_id = self.find_user_id()
        if user_id:
            # Check main profile directory
            user_profile_dir = self.user_dir / user_id / profile_type
            user_profile_path = user_profile_dir / f"{profile_name}.json"

            if user_profile_path.exists():
                try:
                    with open(user_profile_path, 'r') as f:
                        profile = json.load(f)
                        self.profiles_cache[cache_key] = profile
                        return profile
                except:
                    pass

            # Check base subdirectory
            base_profile_path = user_profile_dir / "base" / f"{profile_name}.json"
            if base_profile_path.exists():
                try:
                    with open(base_profile_path, 'r') as f:
                        profile = json.load(f)
                        self.profiles_cache[cache_key] = profile
                        return profile
                except:
                    pass

        # Try system directory
        system_profile_dir = self.system_dir / profile_type
        system_profile_path = system_profile_dir / f"{profile_name}.json"

        if system_profile_path.exists():
            try:
                with open(system_profile_path, 'r') as f:
                    profile = json.load(f)
                    self.profiles_cache[cache_key] = profile
                    return profile
            except:
                pass

        # Try system base subdirectory
        system_base_path = system_profile_dir / "base" / f"{profile_name}.json"
        if system_base_path.exists():
            try:
                with open(system_base_path, 'r') as f:
                    profile = json.load(f)
                    self.profiles_cache[cache_key] = profile
                    return profile
            except:
                pass

        return None

    def merge_preset_chain(self, preset_name: str, preset_type: str, embedded_presets: Dict, index: int = 0) -> Dict[str, Any]:
        """Recursively merge preset inheritance chain into flat key/values"""
        merged = {}

        if not preset_name:
            return merged

        # Find the preset - check embedded first, then disk
        preset = None
        if preset_name in embedded_presets:
            preset = embedded_presets[preset_name]
        else:
            preset = self.load_profile(preset_name, preset_type)

        if not preset:
            return merged

        # First, get parent settings if there's inheritance
        if 'inherits' in preset and preset['inherits']:
            parent_settings = self.merge_preset_chain(preset['inherits'], preset_type, embedded_presets, index)
            merged.update(parent_settings)

        # Then apply this preset's settings
        for key, value in preset.items():
            # Skip metadata
            if key in {'name', 'type', 'from', 'inherits', 'version', 'setting_id',
                      'instantiation', 'compatible_printers', 'compatible_printers_condition',
                      'description'}:
                continue

            # Handle arrays - extract the specific index for this filament/extruder slot
            if isinstance(value, list):
                if len(value) == 1:
                    merged[key] = value[0]
                elif len(value) > index:
                    merged[key] = value[index]
                else:
                    # Use last value if index out of bounds
                    merged[key] = value[-1] if value else None
            else:
                merged[key] = value

        return merged

    def apply_project_overrides(
        self,
        base_settings: Dict,
        project_config: Dict,
        setting_type: str,
        index: int = 0,
        *,
        variant_index: int = 0,
        filament_count: int = 1,
    ) -> Dict:
        """Apply project_settings.config overrides to base settings.

        For filament scope, list values may be laid out per-slot or
        per-(slot × variant); `variant_index` selects the variant within a slot.
        """
        result = base_settings.copy()

        for key, value in project_config.items():
            # Skip metadata
            if key in {'name', 'from', 'version', 'filament_settings_id',
                      'print_settings_id', 'printer_settings_id', 'compatible_printers',
                      'different_settings_to_system'}:
                continue

            # Determine if this key belongs to the current setting type
            belongs = self._scope_matches(key, setting_type, base_settings)

            if belongs:
                if isinstance(value, list):
                    if setting_type == 'filament':
                        resolved = self._resolve_filament_array_value(
                            value, index, variant_index, filament_count
                        )
                        if resolved is not None:
                            result[key] = resolved
                    elif len(value) > index:
                        result[key] = value[index]
                    elif value:
                        result[key] = value[-1]
                else:
                    result[key] = value

        return result

    def resolve_indexed_value(self, value: Any, index: int) -> Optional[Any]:
        """Return a scalar value from a possibly indexed project setting"""
        if value is None:
            return None
        if isinstance(value, list):
            if not value:
                return None
            if len(value) == 1:
                return value[0]
            if index < len(value):
                return value[index]
            return value[-1]
        return value

    def apply_filament_overrides_to_process(self, process_settings: Dict[str, Any], project_config: Dict[str, Any], filament_index: int, object_overrides: Optional[Dict[str, Any]] = None) -> None:
        """Merge filament override values into process settings for the active extruder"""
        overrides = object_overrides or {}

        for filament_key, process_key in self.filament_override_map.items():
            # Priority: object-level override, then project-level per-filament value
            raw_value = overrides.get(filament_key)
            if raw_value is None:
                raw_value = self.resolve_indexed_value(project_config.get(filament_key), filament_index)

            if raw_value is None:
                continue

            if isinstance(raw_value, str) and raw_value.strip().lower() == 'nil':
                continue

            process_settings[process_key] = raw_value

    def parse_object_overrides(self, project_config: Dict, object_index: int) -> Dict[str, Any]:
        """Parse per-object setting overrides from different_settings_to_system"""
        overrides = {}

        # Get the different_settings_to_system array
        diff_settings = project_config.get('different_settings_to_system', [])

        if object_index < len(diff_settings):
            override_str = diff_settings[object_index]
            if override_str:  # If not empty string
                # Parse semicolon-separated list of override keys
                override_keys = override_str.split(';')

                # For each override key, get the value from project_config
                for key in override_keys:
                    key = key.strip()
                    if key in project_config:
                        value = project_config[key]
                        # If it's an array, take the value at object_index
                        if isinstance(value, list):
                            if object_index < len(value):
                                overrides[key] = value[object_index]
                            elif value:
                                overrides[key] = value[-1]
                        else:
                            # Scalar value applies to all objects
                            overrides[key] = value

        return overrides

    # Legacy flat extractor removed per new structured output requirements

    def extract_structured_from_3mf(
        self,
        file_path: Path,
        resolve_inheritance: bool = True,
        plate_number: Optional[int] = None,
        include_gcode: bool = False,
        friendly_names: bool = False,
    ) -> Dict:
        """Extract profiles in a structured, scope-aware format.

        Output schema:
        {
          'machine': { 'id': str, 'settings': {...} },
          'process': { 'id': str, 'settings': {...} },
          'filaments': { 'slotN': { 'id': str, 'settings': {...} }, ... },
          'plates': { '<n>': { 'objects': [ { name,id,index,extruder, process_id, filament_slot, filament_id, process_overrides }, ... ] }}
        }
        If plate_number is specified, only that plate is returned under 'plates'.
        """
        result: Dict[str, Any] = {}

        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()

            with zipfile.ZipFile(BytesIO(file_content), 'r') as zf:
                file_list = zf.namelist()

                # Load project settings (the main config)
                project_config: Dict[str, Any] = {}
                if 'Metadata/project_settings.config' in file_list:
                    content = zf.read('Metadata/project_settings.config')
                    text = content.decode('utf-8', errors='ignore').strip()
                    if text.startswith('{'):
                        project_config = json.loads(text)

                # Canonical printer model id (e.g. "N6" for X2D) — only present in
                # sliced 3MFs, written per-plate into slice_info.config.
                model_id = self._read_printer_model_id(zf, file_list, plate_number)

                # Load embedded presets (filament and process)
                embedded_presets: Dict[str, Any] = {}
                embedded_process_by_plate: Dict[int, Dict[str, Any]] = {}
                for file_name in file_list:
                    is_filament = 'filament_settings' in file_name and file_name.endswith('.config')
                    is_process = 'process_settings' in file_name and file_name.endswith('.config')
                    if is_filament or is_process:
                        try:
                            content = zf.read(file_name)
                            text_content = content.decode('utf-8', errors='ignore').strip()
                            if text_content.startswith('{'):
                                data = json.loads(text_content)
                                preset_name = data.get('name', file_name)
                                embedded_presets[preset_name] = data
                                # Track process presets by plate number (e.g., process_settings_1.config -> plate 1)
                                if is_process:
                                    import re as re_mod
                                    match = re_mod.search(r'process_settings_(\d+)\.config', file_name)
                                    if match:
                                        plate_num = int(match.group(1))
                                        embedded_process_by_plate[plate_num] = data
                        except Exception:
                            pass

                # IDs
                process_id = project_config.get('print_settings_id', '')
                machine_id = project_config.get('printer_settings_id', '')
                filament_ids = project_config.get('filament_settings_id', [])
                if isinstance(filament_ids, str):
                    filament_ids = [filament_ids]

                # Machine
                if machine_id:
                    machine_base = self.merge_preset_chain(machine_id, 'machine', embedded_presets)
                    machine_settings = self.apply_project_overrides(machine_base, project_config, 'machine') if resolve_inheritance else machine_base
                    if not include_gcode:
                        machine_settings = self.filter_gcode_keys(machine_settings)
                    result['machine'] = {'id': machine_id, 'model_id': model_id, 'settings': machine_settings}

                # Process (project-level)
                # Determine the active extruder variant index for process settings arrays
                variant_index = self._get_extruder_variant_index(project_config)

                # Use embedded process preset if available (prefer plate 1 or the specified plate)
                target_plate = plate_number if plate_number is not None else 1
                embedded_process = embedded_process_by_plate.get(target_plate)

                if embedded_process and resolve_inheritance:
                    # Use the embedded preset's inheritance chain with variant index
                    embedded_inherits = embedded_process.get('inherits', '')
                    process_base = self.merge_preset_chain(embedded_inherits, 'process', embedded_presets, variant_index) if embedded_inherits else {}
                    # Apply the embedded preset's own settings on top (handling arrays with variant index)
                    for key, value in embedded_process.items():
                        if key not in {'name', 'type', 'from', 'inherits', 'version', 'setting_id',
                                      'instantiation', 'compatible_printers', 'compatible_printers_condition',
                                      'description', 'print_settings_id', 'print_extruder_id', 'print_extruder_variant'}:
                            # Handle array values with variant index
                            if isinstance(value, list):
                                if len(value) == 1:
                                    process_base[key] = value[0]
                                elif len(value) > variant_index:
                                    process_base[key] = value[variant_index]
                                elif value:
                                    process_base[key] = value[-1]
                            else:
                                process_base[key] = value

                    # Apply project overrides only for values that differ from inheritance
                    # This filters out stale cached values while keeping intentional overrides
                    process_settings = self._apply_differing_overrides(
                        process_base, project_config, variant_index, embedded_process
                    )
                    process_id = embedded_process.get('name', process_id)
                else:
                    # Fall back to original behavior
                    process_base = self.merge_preset_chain(process_id, 'process', embedded_presets, variant_index) if process_id else {}
                    process_settings = self.apply_project_overrides(process_base, project_config, 'process', variant_index) if resolve_inheritance else (process_base or project_config)

                if not include_gcode:
                    process_settings = self.filter_gcode_keys(process_settings)
                result['process'] = {'id': process_id, 'settings': process_settings}

                # Build object and plate mapping; collect used filament ids with representative slot index
                plates: Dict[str, Any] = {}
                used_filaments: Dict[str, int] = {}  # filament_id -> first seen slot index
                if 'Metadata/model_settings.config' in file_list:
                    try:
                        model_content = zf.read('Metadata/model_settings.config')
                        model_tree = ET.fromstring(model_content)

                        # All objects list (index across file)
                        objects: List[Dict[str, Any]] = []
                        id_to_index: Dict[str, int] = {}
                        id_to_info: Dict[str, Dict[str, Any]] = {}
                        idx = 0
                        for obj_elem in model_tree.findall(".//object"):
                            obj_id = obj_elem.get('id')
                            name_elem = obj_elem.find(".//metadata[@key='name']")
                            ext_elem = obj_elem.find(".//metadata[@key='extruder']")
                            obj_name = name_elem.get('value') if name_elem is not None else f"object_{obj_id}"
                            extruder = int(ext_elem.get('value')) if ext_elem is not None else 1
                            info = {
                                'name': obj_name,
                                'id': obj_id,
                                'index': idx,
                                'extruder': extruder,
                            }
                            objects.append(info)
                            id_to_index[obj_id] = idx
                            id_to_info[obj_id] = info
                            idx += 1

                        # For each plate, collect objects via model_instance -> object_id
                        for plate_elem in model_tree.findall(".//plate"):
                            plate_id_elem = plate_elem.find(".//metadata[@key='plater_id']")
                            if plate_id_elem is None:
                                continue
                            plate_id = int(plate_id_elem.get('value'))
                            if plate_number is not None and plate_id != plate_number:
                                continue
                            plate_objects: List[Dict[str, Any]] = []
                            for instance in plate_elem.findall(".//model_instance"):
                                obj_id_elem = instance.find(".//metadata[@key='object_id']")
                                if obj_id_elem is None:
                                    continue
                                obj_id = obj_id_elem.get('value')
                                if obj_id not in id_to_info:
                                    continue
                                base = id_to_info[obj_id]
                                object_index = base['index']
                                filament_slot = base['extruder'] - 1
                                filament_id = filament_ids[filament_slot] if filament_slot < len(filament_ids) else None
                                if filament_id and filament_id not in used_filaments:
                                    used_filaments[filament_id] = filament_slot
                                # Per-object overrides
                                overrides = self.parse_object_overrides(project_config, object_index)
                                if not include_gcode:
                                    overrides = self.filter_gcode_keys(overrides)
                                plate_objects.append({
                                    'name': base['name'],
                                    'id': base['id'],
                                    'index': object_index,
                                    'extruder': base['extruder'],
                                    'process_id': process_id,
                                    'filament_id': filament_id,
                                    'process_overrides': overrides,
                                })
                            plates[str(plate_id)] = {'objects': plate_objects}

                    except Exception:
                        pass

                result['plates'] = plates

                # Filaments (only used ones, unique by preset id)
                filaments: Dict[str, Any] = {}
                filament_variant_idx = self._resolve_filament_variant_index(project_config)
                filament_count = len(filament_ids) if filament_ids else 1
                for fid, slot_index in used_filaments.items():
                    # Preset arrays are per-variant only (e.g. one filament profile
                    # holds nozzle_temperature for all variants); index by variant.
                    filament_base = self.merge_preset_chain(
                        fid, 'filament', embedded_presets, filament_variant_idx
                    )
                    if resolve_inheritance:
                        filament_settings = self.apply_project_overrides(
                            filament_base, project_config, 'filament',
                            index=slot_index,
                            variant_index=filament_variant_idx,
                            filament_count=filament_count,
                        )
                    else:
                        filament_settings = filament_base
                    if not include_gcode:
                        filament_settings = self.filter_gcode_keys(filament_settings)
                    filaments[fid] = {
                        'id': fid,
                        'settings': filament_settings
                    }
                result['filaments'] = filaments

        except Exception as e:
            print(f"Error processing {file_path}: {e}", file=sys.stderr)
            return {}

        if friendly_names:
            self._apply_friendly_structured(result)

        return result

    def get_plate_info(self, file_path: Path) -> Dict[int, Dict]:
        """Get detailed information about all plates in a 3MF file"""
        plate_info = {}

        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()

            with zipfile.ZipFile(BytesIO(file_content), 'r') as zf:
                file_list = zf.namelist()

                # Parse the 3D model to get build items (actual plate data)
                if '3D/3dmodel.model' in file_list:
                    import xml.etree.ElementTree as ET
                    model_content = zf.read('3D/3dmodel.model')
                    root = ET.fromstring(model_content)

                    # Namespace for 3MF
                    ns = {'': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}

                    # Find build items - each one represents a plate
                    build = root.find('.//build', ns)
                    if build is not None:
                        items = build.findall('item', ns)
                        for i, item in enumerate(items, 1):
                            plate_info[i] = {
                                'has_json': False,
                                'objects': [],
                                'object_id': item.get('objectid')
                            }

                # Get object names from model_settings.config
                object_names = {}
                if 'Metadata/model_settings.config' in file_list:
                    settings_content = zf.read('Metadata/model_settings.config').decode('utf-8', errors='ignore')
                    # Parse XML to get object names
                    import re
                    for match in re.finditer(r'<object id=\"(\d+)\"[^>]*>.*?<metadata key=\"name\" value=\"([^\"]+)\"', settings_content, re.DOTALL):
                        obj_id = match.group(1)
                        obj_name = match.group(2)
                        object_names[obj_id] = obj_name

                # Map object names to plates
                for plate_num, info in plate_info.items():
                    if 'object_id' in info and info['object_id'] in object_names:
                        info['objects'].append(object_names[info['object_id']])

                # Also check for plate JSON files to get additional metadata
                for plate_num in list(plate_info.keys()):
                    json_file = f'Metadata/plate_{plate_num}.json'
                    if json_file in file_list:
                        try:
                            content = zf.read(json_file)
                            data = json.loads(content.decode('utf-8'))
                            plate_info[plate_num]['has_json'] = True
                            plate_info[plate_num]['bed_type'] = data.get('bed_type', 'unknown')
                            plate_info[plate_num]['nozzle_diameter'] = data.get('nozzle_diameter', 'unknown')

                            # Override with JSON object names if available
                            if 'bbox_objects' in data:
                                plate_info[plate_num]['objects'] = []
                                for obj in data['bbox_objects']:
                                    plate_info[plate_num]['objects'].append(obj.get('name', 'unnamed'))
                        except:
                            pass

        except Exception as e:
            print(f"Error getting plate info: {e}", file=sys.stderr)

        return plate_info

    def get_plate_numbers(self, file_path: Path) -> List[int]:
        """Get list of plate numbers in a 3MF file"""
        plate_info = self.get_plate_info(file_path)
        return list(plate_info.keys())

    def list_objects(self, file_path: Path) -> Dict[str, Any]:
        """List all plates and objects in the 3MF file"""
        result = {'plates': {}, 'objects': []}

        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()

            with zipfile.ZipFile(BytesIO(file_content), 'r') as zf:
                file_list = zf.namelist()

                # Parse model settings for objects
                if 'Metadata/model_settings.config' in file_list:
                    model_content = zf.read('Metadata/model_settings.config')
                    model_tree = ET.fromstring(model_content)

                    # Extract objects
                    object_index = 0
                    for obj_elem in model_tree.findall(".//object"):
                        obj_id = obj_elem.get('id')
                        name_elem = obj_elem.find(".//metadata[@key='name']")
                        ext_elem = obj_elem.find(".//metadata[@key='extruder']")

                        obj_name = name_elem.get('value') if name_elem is not None else f"object_{obj_id}"
                        extruder = int(ext_elem.get('value')) if ext_elem is not None else 1

                        result['objects'].append({
                            'name': obj_name,
                            'id': obj_id,
                            'index': object_index,
                            'extruder': extruder
                        })
                        object_index += 1

                    # Extract plates
                    for plate_elem in model_tree.findall(".//plate"):
                        plate_id_elem = plate_elem.find(".//metadata[@key='plater_id']")
                        if plate_id_elem is not None:
                            plate_id = int(plate_id_elem.get('value'))
                            plate_objects = []

                            for instance in plate_elem.findall(".//model_instance"):
                                obj_id_elem = instance.find(".//metadata[@key='object_id']")
                                if obj_id_elem is not None:
                                    obj_id = obj_id_elem.get('value')
                                    # Find object name
                                    for obj in result['objects']:
                                        if obj['id'] == obj_id:
                                            plate_objects.append(obj['name'])
                                            break

                            result['plates'][plate_id] = plate_objects

                # Get additional plate info
                plate_info = self.get_plate_info(file_path)
                for plate_num, info in plate_info.items():
                    if plate_num not in result['plates']:
                        result['plates'][plate_num] = info.get('objects', [])

        except Exception as e:
            print(f"Error listing objects: {e}", file=sys.stderr)

        return result

    def get_object_settings(
        self,
        file_path: Path,
        object_selector: str,
        include_gcode: bool = False,
        plate_number: Optional[int] = None,
        friendly_names: bool = False,
    ) -> Dict[str, Any]:
        """Get complete settings for a specific object by name or index"""
        result = {}

        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()

            with zipfile.ZipFile(BytesIO(file_content), 'r') as zf:
                file_list = zf.namelist()

                # Load project settings
                project_config = {}
                if 'Metadata/project_settings.config' in file_list:
                    content = zf.read('Metadata/project_settings.config')
                    text = content.decode('utf-8', errors='ignore').strip()
                    if text.startswith('{'):
                        project_config = json.loads(text)

                # Load embedded presets (filament and process)
                embedded_presets = {}
                embedded_process_by_plate: Dict[int, Dict[str, Any]] = {}
                for file_name in file_list:
                    is_filament = 'filament_settings' in file_name and file_name.endswith('.config')
                    is_process = 'process_settings' in file_name and file_name.endswith('.config')
                    if is_filament or is_process:
                        try:
                            content = zf.read(file_name)
                            text_content = content.decode('utf-8', errors='ignore').strip()
                            if text_content.startswith('{'):
                                data = json.loads(text_content)
                                preset_name = data.get('name', file_name)
                                embedded_presets[preset_name] = data
                                # Track process presets by plate number
                                if is_process:
                                    match = re.search(r'process_settings_(\d+)\.config', file_name)
                                    if match:
                                        plate_num = int(match.group(1))
                                        embedded_process_by_plate[plate_num] = data
                        except:
                            pass

                # Find the object
                object_index = None
                object_extruder = None
                object_name = None

                if 'Metadata/model_settings.config' in file_list:
                    model_content = zf.read('Metadata/model_settings.config')
                    model_tree = ET.fromstring(model_content)

                    # Build a set of allowed object IDs if plate_number is specified
                    allowed_object_ids = None
                    if plate_number is not None:
                        allowed_object_ids = set()
                        for plate_elem in model_tree.findall(".//plate"):
                            plate_id_elem = plate_elem.find(".//metadata[@key='plater_id']")
                            if plate_id_elem is not None and int(plate_id_elem.get('value')) == plate_number:
                                for instance in plate_elem.findall(".//model_instance"):
                                    obj_id_elem = instance.find(".//metadata[@key='object_id']")
                                    if obj_id_elem is not None:
                                        allowed_object_ids.add(obj_id_elem.get('value'))

                    # Check if selector is an integer
                    try:
                        target_index = int(object_selector)
                        idx = 0
                        for obj_elem in model_tree.findall(".//object"):
                            if allowed_object_ids is not None and obj_elem.get('id') not in allowed_object_ids:
                                idx += 1
                                continue
                            if idx == target_index:
                                object_index = idx
                                name_elem = obj_elem.find(".//metadata[@key='name']")
                                object_name = name_elem.get('value') if name_elem is not None else f"object_{obj_elem.get('id')}"
                                ext_elem = obj_elem.find(".//metadata[@key='extruder']")
                                object_extruder = int(ext_elem.get('value')) if ext_elem is not None else 1
                                break
                            idx += 1
                    except ValueError:
                        # Selector is a name
                        idx = 0
                        for obj_elem in model_tree.findall(".//object"):
                            name_elem = obj_elem.find(".//metadata[@key='name']")
                            if name_elem is not None and name_elem.get('value') == object_selector:
                                if allowed_object_ids is not None and obj_elem.get('id') not in allowed_object_ids:
                                    idx += 1
                                    continue
                                object_index = idx
                                object_name = object_selector
                                ext_elem = obj_elem.find(".//metadata[@key='extruder']")
                                object_extruder = int(ext_elem.get('value')) if ext_elem is not None else 1
                                break
                            idx += 1

                if object_index is None:
                    return {'error': f'Object "{object_selector}" not found'}

                # Get preset IDs
                process_id = project_config.get('print_settings_id', '')
                machine_id = project_config.get('printer_settings_id', '')
                filament_ids = project_config.get('filament_settings_id', [])
                if isinstance(filament_ids, str):
                    filament_ids = [filament_ids]

                filament_slot = max(object_extruder - 1, 0)
                filament_id = filament_ids[filament_slot] if filament_slot < len(filament_ids) else None

                # Resolve machine settings
                machine_base = self.merge_preset_chain(machine_id, 'machine', embedded_presets)
                machine_settings = self.apply_project_overrides(machine_base, project_config, 'machine')
                if not include_gcode:
                    machine_settings = self.filter_gcode_keys(machine_settings)
                result['machine'] = machine_settings

                # Resolve process settings
                # Determine the active extruder variant index for process settings arrays
                variant_index = self._get_extruder_variant_index(project_config)

                # Use embedded process preset if available
                target_plate = plate_number if plate_number is not None else 1
                embedded_process = embedded_process_by_plate.get(target_plate)

                if embedded_process:
                    # Use the embedded preset's inheritance chain with variant index
                    embedded_inherits = embedded_process.get('inherits', '')
                    process_base = self.merge_preset_chain(embedded_inherits, 'process', embedded_presets, variant_index) if embedded_inherits else {}
                    # Apply the embedded preset's own settings on top
                    for key, value in embedded_process.items():
                        if key not in {'name', 'type', 'from', 'inherits', 'version', 'setting_id',
                                      'instantiation', 'compatible_printers', 'compatible_printers_condition',
                                      'description', 'print_settings_id', 'print_extruder_id', 'print_extruder_variant'}:
                            if isinstance(value, list):
                                if len(value) == 1:
                                    process_base[key] = value[0]
                                elif len(value) > variant_index:
                                    process_base[key] = value[variant_index]
                                elif value:
                                    process_base[key] = value[-1]
                            else:
                                process_base[key] = value
                    # Apply differing overrides from project config
                    process_settings = self._apply_differing_overrides(process_base, project_config, variant_index, embedded_process)
                else:
                    # Fall back to original behavior
                    process_base = self.merge_preset_chain(process_id, 'process', embedded_presets, variant_index)
                    process_settings = self.apply_project_overrides(process_base, project_config, 'process', variant_index)

                # Apply per-object overrides
                obj_overrides = self.parse_object_overrides(project_config, object_index)
                # Apply non-filament overrides directly to the process block
                non_filament_overrides = {k: v for k, v in obj_overrides.items() if not k.startswith('filament_')}
                process_settings.update(non_filament_overrides)

                # Apply filament-scoped overrides to the effective process values
                if filament_slot is not None:
                    self.apply_filament_overrides_to_process(process_settings, project_config, filament_slot, obj_overrides)

                if not include_gcode:
                    process_settings = self.filter_gcode_keys(process_settings)
                result['process'] = process_settings

                # Resolve filament settings
                if filament_id:
                    filament_base = self.merge_preset_chain(filament_id, 'filament', embedded_presets, filament_slot)
                    filament_settings = self.apply_project_overrides(filament_base, project_config, 'filament', filament_slot)
                    if not include_gcode:
                        filament_settings = self.filter_gcode_keys(filament_settings)
                    result['filament'] = filament_settings

        except Exception as e:
            result['error'] = str(e)

        if friendly_names and 'error' not in result:
            self._apply_friendly_object(result)

        return result

    def filter_gcode_keys(self, settings: Dict) -> Dict:
        """Remove keys containing 'gcode' from settings"""
        return {k: v for k, v in settings.items() if 'gcode' not in k.lower()}
