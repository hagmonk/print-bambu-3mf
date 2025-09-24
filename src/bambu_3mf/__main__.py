"""
Command-line interface for bambu-3mf
"""

import argparse
import json
import sys
from pathlib import Path

from .extractor import BambuProfileExtractor


def main():
    parser = argparse.ArgumentParser(description='Extract Bambu Studio profiles from 3MF files')
    parser.add_argument('files', nargs='*', help='3MF files to process (default: all *.3mf in current directory)')
    parser.add_argument('--no-inheritance', action='store_true',
                        help='Do not resolve profile inheritance')
    parser.add_argument('--output', '-o', help='Output file (default: stdout)')
    parser.add_argument('--plate', type=int, help='Plate number to extract (required if multiple plates exist)')

    # New arguments
    parser.add_argument('--list', action='store_true',
                        help='List plates and objects in the file')
    parser.add_argument('--object', type=str,
                        help='Only dump settings for specific object by name or index')
    parser.add_argument('--full', action='store_true',
                        help='Include gcode settings (excluded by default)')
    parser.add_argument('--select', type=str, default='machine,filament,process',
                        help='Comma-separated list of settings to include (choose from: machine,filament,process)')

    args = parser.parse_args()

    # Find files to process
    if args.files:
        files = [Path(f) for f in args.files if Path(f).exists()]
    else:
        files = list(Path('.').glob('*.3mf'))

    if not files:
        print("No .3mf files found", file=sys.stderr)
        return

    # Initialize extractor
    extractor = BambuProfileExtractor()

    # Handle --list mode
    if args.list:
        for file_path in files:
            info = extractor.list_objects(file_path)

            print(f"\n{file_path.name}:")
            print("-" * 40)

            # List plates
            if info['plates']:
                print("\nPlates:")
                for plate_id in sorted(info['plates'].keys()):
                    objects = info['plates'][plate_id]
                    if objects:
                        print(f"  Plate {plate_id}:")
                        for obj in objects:
                            print(f"    - {obj}")
                    else:
                        print(f"  Plate {plate_id}: (empty)")

            # List objects with details
            if info['objects']:
                print("\nObjects:")
                for obj in info['objects']:
                    print(f"  [{obj['index']}] {obj['name']}")
                    print(f"      ID: {obj['id']}, Extruder: {obj['extruder']}")
        return

    # Handle --object mode (Mode 3: plate+object -> flattened machine/process/filament)
    if args.object:
        if len(files) != 1:
            print("Error: --object requires exactly one 3MF file", file=sys.stderr)
            sys.exit(1)

        file_path = files[0]
        result = extractor.get_object_settings(file_path, args.object, include_gcode=args.full, plate_number=args.plate)

        # Apply select filtering for object mode
        select_types = {s.strip() for s in args.select.split(',') if s.strip()}
        filtered = {}
        for key in ('machine', 'filament', 'process'):
            if key in select_types and key in result:
                filtered[key] = result[key]
        # If select was empty or invalid, fall back to original result
        if filtered:
            result = filtered

        if 'error' in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

        output_json = json.dumps(result, indent=2, sort_keys=True)

        if args.output:
            with open(args.output, 'w') as f:
                f.write(output_json)
            print(f"Saved to {args.output}", file=sys.stderr)
        else:
            print(output_json)
        return

    # Mode 1 and 2: file-level (all plates and objects) or a specific plate
    all_profiles = {}
    for file_path in files:
        profiles = extractor.extract_structured_from_3mf(
            file_path,
            resolve_inheritance=not args.no_inheritance,
            plate_number=args.plate,
            include_gcode=args.full,
        )
        if profiles:
            # Apply select filtering: remove top-level machine/process/filaments when not requested
            select_types = {s.strip() for s in args.select.split(',') if s.strip()}
            to_emit = {}
            # Always include plates
            if 'plates' in profiles:
                to_emit['plates'] = profiles['plates']
            # Conditionally include selected sections
            if 'machine' in profiles and ('machine' in select_types or not select_types):
                to_emit['machine'] = profiles['machine']
            if 'process' in profiles and ('process' in select_types or not select_types):
                to_emit['process'] = profiles['process']
            if 'filaments' in profiles and ('filament' in select_types or not select_types):
                to_emit['filaments'] = profiles['filaments']
            all_profiles[file_path.name] = to_emit

    # Output results
    output_json = json.dumps(all_profiles, indent=2, sort_keys=True)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(output_json)
        print(f"Saved to {args.output}", file=sys.stderr)
    else:
        print(output_json)


if __name__ == "__main__":
    main()
