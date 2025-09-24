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
from typing import Dict, List, Optional, Any


class BambuProfileExtractor:
    def __init__(self):
        self.user_dir = Path.home() / "Library" / "Application Support" / "BambuStudio" / "user"
        self.system_dir = Path.home() / "Library" / "Application Support" / "BambuStudio" / "system" / "BBL"
        self.profiles_cache = {}

        # Setting type patterns for categorization
        self.type_patterns = {
            'filament': ['filament_', 'nozzle_temp', 'bed_temp', 'chamber_temp',
                        'fan_', 'overhang_fan', 'pressure_advance', 'retract',
                        '_plate_temp', 'required_nozzle_HRC'],
            'process': ['layer_', 'wall_', 'infill_', 'support_', 'bridge_',
                       'brim_', 'skirt_', 'seam_', 'gap_', 'thin_wall', 'thick_',
                       'overhang_', 'enable_arc', 'resolution', 'xy_', 'elefant',
                       'sparse_infill', 'solid_infill', 'top_surface', 'bottom_surface',
                       'inner_wall', 'outer_wall', 'travel_', 'first_layer', 'top_shell',
                       'bottom_shell', 'enable_', 'detect_', 'print_', 'spiral_',
                       'fuzzy_', 'filter_', 'adaptive_', 'support_', 'raft_'],
            'machine': ['machine_', 'printer_', 'nozzle_diameter', 'printable_',
                       'gcode_flavor', 'max_print_', 'extruder_', 'scan_',
                       'head_', 'upward_', 'bed_', 'z_offset']
        }

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

    def apply_project_overrides(self, base_settings: Dict, project_config: Dict, setting_type: str, index: int = 0) -> Dict:
        """Apply project_settings.config overrides to base settings"""
        result = base_settings.copy()

        for key, value in project_config.items():
            # Skip metadata
            if key in {'name', 'from', 'version', 'filament_settings_id',
                      'print_settings_id', 'printer_settings_id', 'compatible_printers',
                      'different_settings_to_system'}:
                continue

            # Determine if this key belongs to the current setting type
            belongs = False
            for pattern in self.type_patterns.get(setting_type, []):
                if pattern in key.lower():
                    belongs = True
                    break

            if belongs:
                if isinstance(value, list):
                    if len(value) > index:
                        result[key] = value[index]
                    elif value:
                        result[key] = value[-1]
                else:
                    result[key] = value

        return result

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

    def extract_structured_from_3mf(self, file_path: Path, resolve_inheritance: bool = True, plate_number: Optional[int] = None, include_gcode: bool = False) -> Dict:
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

                # Load embedded presets (filament only)
                embedded_presets: Dict[str, Any] = {}
                for file_name in file_list:
                    if 'filament_settings' in file_name and file_name.endswith('.config'):
                        try:
                            content = zf.read(file_name)
                            text_content = content.decode('utf-8', errors='ignore').strip()
                            if text_content.startswith('{'):
                                data = json.loads(text_content)
                                preset_name = data.get('name', file_name)
                                embedded_presets[preset_name] = data
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
                    result['machine'] = {'id': machine_id, 'settings': machine_settings}

                # Process (project-level)
                process_base = self.merge_preset_chain(process_id, 'process', embedded_presets) if process_id else {}
                process_settings = self.apply_project_overrides(process_base, project_config, 'process') if resolve_inheritance else (process_base or project_config)
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
                for fid, slot_index in used_filaments.items():
                    filament_base = self.merge_preset_chain(fid, 'filament', embedded_presets, slot_index)
                    filament_settings = self.apply_project_overrides(filament_base, project_config, 'filament', slot_index) if resolve_inheritance else filament_base
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

    def get_object_settings(self, file_path: Path, object_selector: str, include_gcode: bool = False, plate_number: Optional[int] = None) -> Dict[str, Any]:
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

                # Load embedded presets
                embedded_presets = {}
                for file_name in file_list:
                    if 'filament_settings' in file_name and file_name.endswith('.config'):
                        try:
                            content = zf.read(file_name)
                            text_content = content.decode('utf-8', errors='ignore').strip()
                            if text_content.startswith('{'):
                                data = json.loads(text_content)
                                preset_name = data.get('name', file_name)
                                embedded_presets[preset_name] = data
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

                filament_slot = object_extruder - 1
                filament_id = filament_ids[filament_slot] if filament_slot < len(filament_ids) else None

                # Resolve machine settings
                machine_base = self.merge_preset_chain(machine_id, 'machine', embedded_presets)
                machine_settings = self.apply_project_overrides(machine_base, project_config, 'machine')
                if not include_gcode:
                    machine_settings = self.filter_gcode_keys(machine_settings)
                result['machine'] = machine_settings

                # Resolve process settings
                process_base = self.merge_preset_chain(process_id, 'process', embedded_presets)
                process_settings = self.apply_project_overrides(process_base, project_config, 'process')

                # Apply per-object overrides
                obj_overrides = self.parse_object_overrides(project_config, object_index)
                process_settings.update(obj_overrides)

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

        return result

    def filter_gcode_keys(self, settings: Dict) -> Dict:
        """Remove keys containing 'gcode' from settings"""
        return {k: v for k, v in settings.items() if 'gcode' not in k.lower()}
