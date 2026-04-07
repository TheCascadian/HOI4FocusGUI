"""
HOI4 Map Converter

Converts HOI4 province polygons + state definitions into the JSON format used
by the State Viewport module.

This converter expects one of two inputs for province geometry:
 - A JSON file with a top-level 'provinces' mapping where each province id maps
   to an object with 'polygons': list of polygons (each polygon is list of [x,y]).
   Example:
   {
     "provinces": {
       "1": {"polygons": [ [[0,0],[10,0],[10,10],[0,10]] ]},
       "2": {"polygons": [ [[12,0],[22,0],[22,8],[12,8]] ]}
     }
   }
 - OR a JSON file already containing a top-level 'states' mapping (then this
   script can be a pass-through or used to remap/inspect).

And a HOI4 states text file, typically found in a mod or base game under
'common/states/*.txt' containing state blocks with province lists. The parser
is resilient to whitespace/comments but not a full Paradox script parser; it
handles common structures.

Output JSON format matches the State Viewport expectations:
{ "states": { "<state_id>": {"name": "...", "polygons": [ ... ], "provinces": [...] } } }

Usage:
    python _hoi4_map_converter.py --provinces provinces.json --states base_states.txt --out states_viewport.json

If your province geometry is only available as a provinces.bmp + definition.csv,
there are community utilities to raster->polygon; this converter assumes polygon
geometry input (already vectorized) for reliability and performance.
"""
# region File (auto-generated)
# endregion

from __future__ import annotations

from _imports import (
    # Standard library
    json, re,
    # Typing
    Any, Dict, List, Optional, Tuple,
)
import argparse


def load_province_polygons(json_path: str) -> Dict[str, List[List[List[float]]]]:
    """Load a provinces polygons JSON and return mapping province_id -> polygons.
    Expected to find 'provinces' or 'states' top-level keys. If 'states' exists
    this function will return a flattened province mapping if possible.
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if 'provinces' in data:
        provs = data['provinces']
        # Normalize to string keys and polygons list
        out = {}
        for pid, meta in provs.items():
            try:
                polygons = meta.get('polygons') if isinstance(meta, dict) else None
            except Exception:
                polygons = None
            if polygons is None:
                # maybe the file is in other form, skip
                continue
            out[str(pid)] = polygons
        return out
    elif 'states' in data:
        # flatten provinces referenced by states: states may contain 'provinces'
        out = {}
        for sid, meta in data['states'].items():
            # states may include polygons directly; derive provinces from metadata if present
            if isinstance(meta, dict) and 'provinces' in meta and 'polygons' not in meta:
                # Can't create province polygons from state polygons. Skip.
                continue
        # Can't reliably produce province-level polygons from states input
        raise ValueError("Input JSON contains 'states' but not 'provinces' polygons. Provide province polygons JSON for conversion.")
    else:
        raise ValueError('Provided JSON must contain a top-level "provinces" mapping with polygon lists')

def parse_states_file(states_path: str) -> Dict[str, Dict[str, Any]]:
    """Parse a HOI4 states file and return mapping state_id -> metadata dict.

    Extracts commonly used fields so downstream tooling can manipulate them:
    - name
    - provinces (list of province ids)
    - manpower (int)
    - state_category (str)
    - owner (str)
    - cores (list of tags added via add_core_of)
    - claims (list of tags added via add_claim_by)
    - resources mapping (resource -> int)
    """
    text = open(states_path, 'r', encoding='utf-8', errors='ignore').read()
    # Remove comments (// style)
    text = re.sub(r'//.*', '', text)

    # A crude brace-aware tokenizer to extract state blocks
    tokens = re.split(r'(\{)|\}', text)
    # We'll scan the file for occurrences of 'state' followed by a brace
    state_blocks = []
    # Another approach: find 'state = {' positions via regex and then extract matching braces
    state_iter = re.finditer(r'state\s*=\s*\{', text)
    out = {}
    for m in state_iter:
        start = m.end()
        # find matching closing brace
        depth = 1
        i = start
        L = len(text)
        while i < L and depth > 0:
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            i += 1
        block = text[start:i-1]
        # parse block for id and provinces
        sid = None
        name = None
        provinces = []
        manpower = None
        state_category = None
        owner = None
        cores: List[str] = []
        claims: List[str] = []
        resources: Dict[str, int] = {}
        # find id = <num>
        id_m = re.search(r'\bid\s*=\s*([0-9]+)', block)
        if id_m:
            sid = id_m.group(1)
        # name = "..." or name = NAME
        name_m = re.search(r'name\s*=\s*"([^"]+)"', block)
        if name_m:
            name = name_m.group(1)
        # provinces = { ... }
        prov_m = re.search(r'provinces\s*=\s*\{([^\}]*)\}', block, re.S)
        if prov_m:
            inner = prov_m.group(1)
            # parse numbers
            nums = re.findall(r'(-?\d+)', inner)
            provinces = [str(int(n)) for n in nums]

        manpower_m = re.search(r'\bmanpower\s*=\s*(-?[0-9eE\.]+)', block)
        if manpower_m:
            try:
                manpower = int(float(manpower_m.group(1)))
            except ValueError:
                manpower = None

        cat_m = re.search(r'\bstate_category\s*=\s*([A-Za-z0-9_\.\-]+)', block)
        if cat_m:
            state_category = cat_m.group(1)

        owner_m = re.search(r'\bowner\s*=\s*([A-Z0-9_\.]+)', block)
        if owner_m:
            owner = owner_m.group(1)

        cores.extend(re.findall(r'add_core_of\s*=\s*([A-Z0-9_\.]+)', block))
        claims.extend(re.findall(r'add_claim_by\s*=\s*([A-Z0-9_\.]+)', block))

        res_m = re.search(r'resources\s*=\s*\{([^}]*)\}', block, re.S)
        if res_m:
            inner = res_m.group(1)
            for res_line in re.findall(r'([A-Za-z_]+)\s*=\s*(-?\d+)', inner):
                res_key, res_val = res_line
                try:
                    resources[res_key] = int(res_val)
                except ValueError:
                    continue
        if sid is None:
            # try to find a numeric label before the block like '123 = {' (some variants)
            pre = text[max(0, m.start()-40):m.start()]
            num_pre = re.search(r'([0-9]+)\s*=$', pre)
            if num_pre:
                sid = num_pre.group(1)
        if sid is None:
            # give fallback unique id by position
            sid = f'@{m.start()}'
        out[str(sid)] = {
            'name': name or '',
            'provinces': provinces,
            'manpower': manpower,
            'state_category': state_category,
            'owner': owner,
            'cores': cores,
            'claims': claims,
            'resources': resources,
        }
    if not out:
        raise ValueError('No state blocks parsed from states file. Is this the correct HOI4 states file?')
    return out

def build_states_from_provinces(province_polys: Dict[str, List[List[List[float]]]], states_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Merge province polygons into states. Returns a dict with 'states' mapping suitable for StateViewport.
    For each state, collect polygons of all member provinces (flattened list).

    IMPORTANT: The output includes BOTH a top-level 'provinces' mapping (atomic polygon data)
    AND a 'states' mapping (which references provinces). This allows the viewport to render
    provinces as distinct visual elements and states as boundary groupings.
    """
    states_out = {}
    for sid, meta in states_map.items():
        provs = meta.get('provinces', []) or []
        state_polys = []
        for pid in provs:
            pid_s = str(pid)
            if pid_s in province_polys:
                # append all polygons for that province
                for poly in province_polys[pid_s]:
                    # ensure floats
                    pts = [[float(x), float(y)] for (x, y) in poly]
                    state_polys.append(pts)
            else:
                # missing province geometry; skip but note
                pass
        state_payload: Dict[str, Any] = {
            'name': meta.get('name', '') or f'State {sid}',
            'polygons': state_polys,
            'provinces': [str(p) for p in provs]
        }
        if meta.get('manpower') is not None:
            state_payload['manpower'] = int(meta['manpower'])
        if meta.get('state_category'):
            state_payload['state_category'] = meta['state_category']
        if meta.get('owner'):
            state_payload['owner'] = meta['owner']
        cores = list(dict.fromkeys(meta.get('cores', []) or []))
        if cores:
            state_payload['cores'] = cores
        claims = list(dict.fromkeys(meta.get('claims', []) or []))
        if claims:
            state_payload['claims'] = claims
        resources = meta.get('resources') or {}
        if resources:
            state_payload['resources'] = {k: int(v) for k, v in resources.items()}
        states_out[str(sid)] = state_payload

    # CRITICAL: Include the provinces mapping at the top level so the viewport
    # can render individual province polygons, not just flattened state polygons
    return {
        'provinces': province_polys,
        'states': states_out
    }

def convert(provinces_json: str, states_txt: str, out_json: str) -> None:
    province_polys = load_province_polygons(provinces_json)
    states_map = parse_states_file(states_txt)
    result = build_states_from_provinces(province_polys, states_map)
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f'Wrote {out_json} with {len(result.get("states", {}))} states')

def main(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(description='Convert HOI4 province polygons + states file -> viewport JSON')
    p.add_argument('--provinces', '-p', required=True, help='Path to province polygons JSON')
    p.add_argument('--states', '-s', required=True, help='Path to HOI4 states txt file')
    p.add_argument('--out', '-o', required=True, help='Output JSON path for viewport')
    args = p.parse_args(argv)
    convert(args.provinces, args.states, args.out)

if __name__ == '__main__':
    main()