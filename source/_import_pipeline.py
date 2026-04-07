"""
Lightweight import pipeline for lenient parsing, preservation of raw blocks,
multi-pass resolution (positions, sanitization), deterministic grid de-conflict,
node/connection creation, simple styling pass, and fit-to-view calculation.

This module is intentionally conservative: unknown blocks are preserved as
raw_text so nothing is lost during import.

API: create ImportPipeline() and call run(text: str) -> dict with keys:
 - nodes: list of Node
 - connections: list of Connection
 - diagnostics: dict

This file implements the recommendations given in the user prompt.
"""

from __future__ import annotations

from _imports import (
    # Standard library
    re,
    # Typing
    Any, Dict, List, Optional, Tuple,
)
from collections import deque
from dataclasses import dataclass, field
from _txt_converter import TxtFocusParser


# region Data Classes

@dataclass
class ParsedNode:
	id: Optional[str]
	parent_id: Optional[str]
	raw_pos: Dict[str, Optional[str]]
	attrs: Dict[str, Any]
	raw_text: str
	depth: int = 0
	abs_x: Optional[float] = None
	abs_y: Optional[float] = None
	audit: List[str] = field(default_factory=list)

@dataclass
class Node:
	id: str
	x: float
	y: float
	attrs: Dict[str, Any]
	raw_text: str

@dataclass
class Connection:
	src: str
	dst: str
	attrs: Dict[str, Any] = field(default_factory=dict)

# endregion

# region Utility Functions

def safe_num(v: Optional[str]) -> Optional[float]:
	if v is None:
		return None
	v = str(v).strip()
	if v == "":
		return None
	try:
		return int(v)
	except Exception:
		try:
			return float(v)
		except Exception:
			return None

# endregion

# region Import Pipeline

class ImportPipeline:
	def __init__(self, grid_size: int = 120, max_search_radius: int = 8):
		self.grid_size = grid_size
		self.max_search_radius = max_search_radius
		# cache last successful parse mode for diagnostics/telemetry
		self._last_parse_mode: Optional[str] = None

	def _run_focus_tree_parser(self, text: str) -> Optional[Dict[str, Any]]:
		"""Attempt to parse an HOI4 focus tree via TxtFocusParser.

		Returns the standard run() payload on success, or None when the input
		does not look like a focus tree (fallback to lenient parser).
		"""
		try:
			parser = TxtFocusParser(text)
		except Exception:
			return None

		try:
			parsed = parser.parse()
		except Exception:
			return None

		focuses: List[Dict[str, Any]] = list(parsed.get('focuses', []) or [])
		if not focuses:
			return None

		nodes: List[Node] = []
		connections: List[Connection] = []
		focus_lookup: Dict[str, Dict[str, Any]] = {}
		mutex_pairs: List[Tuple[str, str]] = []
		for focus in focuses:
			fid = str(focus.get('id') or focus.get('name') or '').strip()
			if not fid:
				continue
			try:
				x_val = float(focus.get('x', 0.0))
			except Exception:
				x_val = 0.0
			try:
				y_val = float(focus.get('y', 0.0))
			except Exception:
				y_val = 0.0
			attrs = {}
			for key, value in focus.items():
				if key in ('id', 'x', 'y'):
					continue
				attrs[key] = value
			raw_text = focus.get('clean_raw') or focus.get('raw') or ''
			node = Node(fid, x_val, y_val, attrs, raw_text)
			nodes.append(node)
			focus_lookup[fid] = focus
			for other in focus.get('mutually_exclusive', []) or []:
				try:
					of_id = str(other).strip()
					if of_id:
						mutex_pairs.append(tuple(sorted((fid, of_id))))
				except Exception:
					continue

		# Build prerequisite connections (group-aware)
		for focus in focuses:
			child_id = str(focus.get('id') or '').strip()
			if not child_id:
				continue
			group_defs: List[Dict[str, Any]] = []
			groups = focus.get('prerequisites_groups')
			if isinstance(groups, list) and groups:
				for idx, grp in enumerate(groups, start=1):
					if isinstance(grp, dict):
						items = list(grp.get('items', []) or [])
						kind = str(grp.get('type') or 'OR').upper()
					elif isinstance(grp, list):
						items = list(grp)
						kind = 'OR'
					else:
						items = [grp]
						kind = 'AND'
					items = [str(it).strip() for it in items if str(it).strip()]
					if not items:
						continue
					group_defs.append({'items': items, 'kind': kind, 'index': idx})
			else:
				raw_pr = focus.get('prerequisites') or []
				if raw_pr:
					if all(isinstance(g, list) for g in raw_pr):
						for idx, grp in enumerate(raw_pr, start=1):
							items = [str(it).strip() for it in grp if str(it).strip()]
							if not items:
								continue
							kind = 'OR' if len(items) > 1 else 'AND'
							group_defs.append({'items': items, 'kind': kind, 'index': idx})
					else:
						items = [str(it).strip() for it in raw_pr if str(it).strip()]
						if items:
							group_defs.append({'items': items, 'kind': 'AND', 'index': 1})

			seen_pairs = set()
			for grp in group_defs:
				kind = grp.get('kind', 'AND')
				for parent_id in grp.get('items', []):
					pair_key = (parent_id, child_id)
					if pair_key in seen_pairs:
						continue
					seen_pairs.add(pair_key)
					attributes = {
						'group_kind': kind,
						'group_index': grp.get('index'),
						'source': 'prereq_group'
					}
					connections.append(Connection(parent_id, child_id, attributes))

		moves = self.deconflict_grid(nodes)
		self.apply_styles(nodes, connections)
		fit = self.compute_fit_view(nodes)
		mutex_pairs = sorted(set(mutex_pairs))

		diagnostics = {
			'parsed_count': len(focuses),
			'preserved_raw_segments': [],
			'nodes_moved': moves,
			'unresolved': [],
			'parsed_audits': {},
			'import_mode': 'hoi4_txt_parser',
			'tree': parsed.get('tree_info', {}),
			'mutex_pairs': mutex_pairs,
		}

		self._last_parse_mode = 'hoi4_txt_parser'
		return {
			'nodes': nodes,
			'connections': connections,
			'diagnostics': diagnostics,
			'fit': fit,
		}

	# ---------- Pass 1: Lenient parse (brace counting + regex key:value) ----------
	def parse_lenient(self, text: str) -> List[ParsedNode]:
		nodes: List[ParsedNode] = []
		i = 0
		n = len(text)
		# accept patterns like 'name {', or 'name = {', etc.
		block_re = re.compile(r"(\w+)\s*(?:=\s*)?\{")
		while i < n:
			m = block_re.search(text, i)
			if not m:
				remainder = text[i:].strip()
				if remainder:
					nodes.append(ParsedNode(None, None, {}, {}, remainder))
				break
			name = m.group(1)
			start = m.end()
			# brace counting
			brace = 1
			j = start
			while j < n and brace > 0:
				if text[j] == '{':
					brace += 1
				elif text[j] == '}':
					brace -= 1
				j += 1
			block = text[m.start():j]
			# extract simple key = value pairs
			attrs: Dict[str, Any] = {}
			for kv in re.findall(r"(\w+)\s*=\s*(\"[^\"]*\"|[^,\n}\s]+)", block):
				k, v = kv
				v = v.strip()
				if v.startswith('"') and v.endswith('"'):
					v = v[1:-1]
				attrs[k] = v
			id_ = attrs.get('id') or attrs.get('name') or f"{name}_{len(nodes)}"
			parent = attrs.get('parent')
			raw_pos = {'x': attrs.get('x'), 'y': attrs.get('y')}
			parsed = ParsedNode(id_, parent, raw_pos, attrs, block)
			nodes.append(parsed)
			i = j
		return nodes

	# ---------- Pass 2: Resolve positions and sanitize ----------
	def resolve_positions(self, parsed: List[ParsedNode]) -> None:
		lookup: Dict[str, ParsedNode] = {p.id: p for p in parsed if p.id}

		def compute_depth(p: ParsedNode, seen=None) -> int:
			if p.depth:
				return p.depth
			if seen is None:
				seen = set()
			if p.id in seen:
				p.audit.append('circular-parent-depth')
				p.depth = 0
				return 0
			seen.add(p.id)
			if p.parent_id and p.parent_id in lookup:
				parent = lookup[p.parent_id]
				p.depth = 1 + compute_depth(parent, seen)
			else:
				p.depth = 0
			return p.depth

		for p in parsed:
			# sanitize basic attrs
			for k in list(p.attrs.keys()):
				# convert numeric-like strings to numbers where possible
				if k in ('x', 'y'):
					# keep as raw strings in raw_pos; attrs may contain same
					continue
			# compute depth
			if p.id:
				compute_depth(p)

		# compute absolute positions using parent offsets when available
		def compute_abs(p: ParsedNode, seen=None) -> Tuple[float, float]:
			if p.abs_x is not None and p.abs_y is not None:
				return p.abs_x, p.abs_y
			if seen is None:
				seen = set()
			if p.id in seen:
				p.audit.append('circular-parent')
				p.abs_x, p.abs_y = 0.0, 0.0
				return p.abs_x, p.abs_y
			seen.add(p.id)
			x_val = safe_num(p.raw_pos.get('x'))
			y_val = safe_num(p.raw_pos.get('y'))
			if p.parent_id and p.parent_id in lookup:
				parent = lookup[p.parent_id]
				px, py = compute_abs(parent, seen)
				p.abs_x = (px + (x_val or 0.0))
				p.abs_y = (py + (y_val or 0.0))
			else:
				p.abs_x = 0.0 if x_val is None else x_val
				p.abs_y = 0.0 if y_val is None else y_val
			return p.abs_x, p.abs_y

		for p in parsed:
			if p.id:
				compute_abs(p)

	# ---------- Pass 3: Build node objects then connections ----------
	def build_nodes_and_connections(self, parsed: List[ParsedNode]) -> Tuple[List[Node], List[Connection]]:
		nodes: List[Node] = []
		connections: List[Connection] = []
		lookup: Dict[str, Node] = {}
		for p in parsed:
			if not p.id:
				continue
			x = float(p.abs_x or 0.0)
			y = float(p.abs_y or 0.0)
			node = Node(p.id, x, y, p.attrs.copy(), p.raw_text)
			nodes.append(node)
			lookup[node.id] = node
		for p in parsed:
			if not p.id:
				continue
			if p.parent_id and p.parent_id in lookup:
				connections.append(Connection(p.parent_id, p.id))
		return nodes, connections

	# ---------- Pass 4: Deterministic de-conflict on a grid ----------
	def deconflict_grid(self, nodes: List[Node]) -> List[Tuple[str, Tuple[float, float], Tuple[float, float]]]:
		# returns list of moves: (id, (from_x,from_y), (to_x,to_y))
		# compute grid cells
		cell_of: Dict[str, Tuple[int, int]] = {}
		for node in nodes:
			cx = round(node.x / self.grid_size)
			cy = round(node.y / self.grid_size)
			cell_of[node.id] = (cx, cy)

		# compute depth ordering to place parents first
		# depth can be inferred by attrs if present, else 0
		def node_depth(n: Node) -> int:
			d = n.attrs.get('depth')
			try:
				return int(d)
			except Exception:
				return 0

		ordered = sorted(nodes, key=lambda n: (node_depth(n), n.y, n.x, n.id))
		occupied: Dict[Tuple[int, int], str] = {}
		moves: List[Tuple[str, Tuple[float, float], Tuple[float, float]]] = []

		def neighbors_in_ring(cx: int, cy: int, r: int):
			# deterministic ordering of ring cells
			cells = []
			for dx in range(-r, r + 1):
				for dy in range(-r, r + 1):
					if max(abs(dx), abs(dy)) != r:
						continue
					cells.append((cx + dx, cy + dy))
			cells.sort(key=lambda c: (abs(c[0] - cx) + abs(c[1] - cy), c[0], c[1]))
			return cells

		for n in ordered:
			orig = (n.x, n.y)
			desired = cell_of[n.id]
			if desired not in occupied:
				occupied[desired] = n.id
				# snap to grid center
				n.x = desired[0] * self.grid_size
				n.y = desired[1] * self.grid_size
				moves.append((n.id, orig, (n.x, n.y)))
				continue
			# search for nearest free cell deterministically
			found = False
			for r in range(1, self.max_search_radius + 1):
				for ccell in neighbors_in_ring(desired[0], desired[1], r):
					if ccell not in occupied:
						occupied[ccell] = n.id
						n.x = ccell[0] * self.grid_size
						n.y = ccell[1] * self.grid_size
						moves.append((n.id, orig, (n.x, n.y)))
						found = True
						break
				if found:
					break
			if not found:
				# as fallback, place on next free row below existing max
				if occupied:
					max_row = max(c for (_, c) in occupied.keys())
				else:
					max_row = desired[1]
				new_cell = (desired[0], max_row + 1)
				occupied[new_cell] = n.id
				n.x = new_cell[0] * self.grid_size
				n.y = new_cell[1] * self.grid_size
				moves.append((n.id, orig, (n.x, n.y)))
		return moves

	# ---------- Pass 5: Apply styles (simple rule-based pass) ----------
	def apply_styles(self, nodes: List[Node], connections: List[Connection]) -> None:
		# Example: mark nodes as 'group_and' if attrs contains 'AND' key
		for n in nodes:
			if 'AND' in (k.upper() for k in n.attrs.keys()):
				n.attrs['style'] = 'group_and'

	# ---------- Pass 6: Compute fit-to-view (bounding box & suggested center/scale) ----------
	def compute_fit_view(self, nodes: List[Node], padding: int = 40) -> Dict[str, Any]:
		if not nodes:
			return {'center': (0, 0), 'scale': 1.0, 'bbox': (0, 0, 0, 0)}
		xs = [n.x for n in nodes]
		ys = [n.y for n in nodes]
		min_x, max_x = min(xs), max(xs)
		min_y, max_y = min(ys), max(ys)
		width = max_x - min_x
		height = max_y - min_y
		bbox = (min_x - padding, min_y - padding, max_x + padding, max_y + padding)
		center = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
		# suggested scale: fit the larger dimension into an arbitrary viewport size (e.g. 1000)
		viewport = 1000
		scale = 1.0
		if max(width, height) > 0:
			scale = viewport / max(width, height)
		return {'center': center, 'scale': scale, 'bbox': bbox}

	# ---------- Run full pipeline ----------
	def run(self, text: str) -> Dict[str, Any]:
		# Prefer the focus-tree aware parser when it recognizes the input.
		focus_payload = self._run_focus_tree_parser(text)
		if focus_payload is not None:
			return focus_payload
		self._last_parse_mode = 'lenient'
		parsed = self.parse_lenient(text)
		self.resolve_positions(parsed)
		nodes, connections = self.build_nodes_and_connections(parsed)
		moves = self.deconflict_grid(nodes)
		self.apply_styles(nodes, connections)
		fit = self.compute_fit_view(nodes)
		diagnostics = {
			'parsed_count': len(parsed),
			'preserved_raw_segments': [p.raw_text for p in parsed if p.id is None],
			'nodes_moved': moves,
			'unresolved': [p.id for p in parsed if p.id and (p.abs_x is None or p.abs_y is None)],
			'parsed_audits': {p.id: p.audit for p in parsed if p.id},
		}
		return {'nodes': nodes, 'connections': connections, 'diagnostics': diagnostics, 'fit': fit}

# endregion

if __name__ == '__main__':
	# quick manual smoke test
	sample = '''root = { id = "r" x=0 y=0 }
child = { id = "c1" parent = "r" x=0 y=0 }
child = { id = "c2" parent = "r" x=0 y=0 }'''
	p = ImportPipeline()
	out = p.run(sample)
	print(out['diagnostics'])