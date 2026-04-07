# focus_generator.py
"""
Procedural focus-tree generator for HOI4 Focus Tree Generator application.

Intended usage:
    from focus_generator import FocusTreeGenerator
    gen = FocusTreeGenerator(library=window.library)
    focuses = gen.generate(tree_id="auto_tree", root_count=2, max_depth=4, branching=(1,3), seed=42)
    # apply to editor window:
    gen.apply_to_editor(window, focuses)

The generator imports the Focus dataclass from the main UI module (_focusGUI.py) and
returns a list[Focus] objects compatible with the project's save/export flows.
"""
from _imports import (
    # Standard library
    math, uuid,
    # Typing
    Dict, List, Optional, Tuple,
    # Project
    Focus,
)

import random
from dataclasses import dataclass, field
from error_handler import silent_operation


# Focus definition removed as it is imported from _dataStructs

# region Generator Constants

DEFAULT_ADJECTIVES = [
    "National", "Industrial", "Military", "Strategic", "Economic", "Security",
    "Diplomatic", "Technological", "Infrastructure", "Propaganda", "Naval",
    "Aerial", "Logistics", "Reform", "Mobilization"
]

DEFAULT_NOUNS = [
    "Reform", "Expansion", "Program", "Doctrine", "Effort", "Initiative",
    "Command", "Accord", "Drive", "Corps", "Plan", "Act", "Policy", "Campaign"
]

# endregion

# region Focus Tree Generator

class FocusTreeGenerator:
    """
    Generates focus trees procedurally.

    Parameters
    ----------
    library : Optional[Dict[str, Dict]]
        Optional library entries from the editor; if present, generator may pick names
        from library entries to increase variety.
    country_tag : str
        Default country tag for naming hints (not required).
    id_prefix : str
        Prefix to use for generated focus IDs.
    """

    def __init__(self, library: Optional[Dict[str, Dict]] = None, country_tag: str = "TAG", id_prefix: str = "gen",
                 theme: Optional[Dict[str, List[str]]] = None):
        self.library = library or {}
        self.country_tag = country_tag
        self.id_prefix = id_prefix
        # Optional theme data (e.g., from madmax_theme.json) with categories -> list[str]
        self.theme: Dict[str, List[str]] = theme or {}
        # Flattened pool for quick sampling
        self._theme_pool: List[str] = []
        for k, v in (self.theme or {}).items():
            with silent_operation("theme_pool_init"):
                if isinstance(v, list):
                    self._theme_pool.extend([str(x) for x in v])
        self._used_theme_names: set = set()
        # layout randomness/current rnd holder for helpers
        self._last_rnd: Optional[random.Random] = None
        self._layout_randomness: float = 0.0

    def _next_id(self, counter: int) -> str:
        """Create a compact unique id using prefix and a short uuid fragment."""
        short = uuid.uuid4().hex[:6]
        return f"{self.id_prefix}_{counter}_{short}"

    def _generate_name(self, use_library: bool, use_theme: bool, rnd: random.Random) -> str:
        """Generate a readable focus name.
        Priority: theme (if requested and available) -> library (if requested and available) -> adjective+noun.
        Ensures minimal duplication within a single generation run.
        """
        # Theme-based names first if requested
        if use_theme and self._theme_pool:
            # Try to avoid duplicates until pool is exhausted
            if len(self._used_theme_names) >= len(self._theme_pool):
                self._used_theme_names.clear()
            # sample with retry
            for _ in range(30):
                title = rnd.choice(self._theme_pool)
                if title not in self._used_theme_names:
                    self._used_theme_names.add(title)
                    return title
            # fallback to any theme name
            return rnd.choice(self._theme_pool)

        # Library names next
        if use_library and self.library:
            keys = list(self.library.keys())
            # select random library entry and use its name (if present) or its id
            entry_key = rnd.choice(keys)
            entry = self.library.get(entry_key, {})
            return entry.get("name") or entry.get("id") or entry_key
        # fallback adjective + noun
        adj = rnd.choice(DEFAULT_ADJECTIVES)
        noun = rnd.choice(DEFAULT_NOUNS)
        return f"{adj} {noun}"

    def generate(
        self,
        tree_id: str = "generated_tree",
        root_count: int = 1,
        max_depth: int = 4,
    branching: Tuple[int, int] = (1, 2),
    spacing: Tuple[int, int] = (1, 1),
        start_x: int = 0,
        start_y: int = 0,
        use_library_names: bool = True,
        use_theme_names: bool = True,
        add_mutex_between_branches: bool = False,
        seed: Optional[int] = None,
        # JS-like parameters (optional): if node_count provided, use growth mode
        node_count: Optional[int] = None,
        branch_density: float = 5.0,
        forced_root_tags: Optional[Dict[int, str]] = None,
        # Child limitation controls
        max_children_per_node: Optional[int] = None,
        # Enforce max_depth in growth mode as well
        enforce_depth_cap: bool = True,
        # Sibling-level mutually exclusive groups
        mutex_siblings: bool = False,
        mutex_sibling_mode: str = 'all',  # 'all' | 'ring' | 'pairs'
        mutex_sibling_probability: float = 1.0,
        # Layout variation (0.0 tidy .. 1.0 chaotic). Controls jitter and ordering randomness.
    layout_randomness: float = 0.3,
    # layout style: 'tidy' | 'organic' | 'clustered' | 'radial' | 'zigzag' | 'wave'
    # When mixing styles across multiple roots, use layout_styles or layout_mix below.
    layout_style: str = 'organic',
    # Optional per-root style assignment (cycles if fewer than root_count)
    layout_styles: Optional[List[str]] = None,
    # Optional weighted mix: list of (style, weight); picked per-root deterministically via seed
    layout_mix: Optional[List[Tuple[str, float]]] = None,
        # Optional theme override for this call
        theme: Optional[Dict[str, List[str]]] = None
    ) -> List[Focus]:
        """
        Produce a list of Focus objects representing a complete tree.

        Parameters
        ----------
        tree_id : str
            Identifying id for the generated tree (informational).
        root_count : int
            Number of independent root focuses (allows multiple trees in one generation).
        max_depth : int
            Maximum depth (levels) below root. Root is depth 0.
        branching : (min_branch, max_branch)
            Inclusive range describing how many children each node may have.
        spacing : (x_spacing, y_spacing)
            Grid spacing (in grid units used by GUI: typically 1 unit = 80 px).
        start_x, start_y : int
            Top-left (or central) coordinates for placement baseline.
        use_library_names : bool
            If True and a library is provided, prefer library names when generating focus names.
        add_mutex_between_branches : bool
            If True, top-level branches will be mutually exclusive (simple policy).
        seed : Optional[int]
            Deterministic seed. If None, use system randomness.

        Returns
        -------
        List[Focus]
            Focus dataclass instances ready to be added to the editor.
        """
        rnd = random.Random(seed)
        # record for helper methods
        self._last_rnd = rnd
        self._layout_randomness = max(0.0, min(1.0, layout_randomness))
        # optionally override theme for this run
        if theme is not None:
            with silent_operation("theme_override"):
                self.theme = theme or {}
                self._theme_pool = []
                for k, v in (self.theme or {}).items():
                    if isinstance(v, list):
                        self._theme_pool.extend([str(x) for x in v])
                self._used_theme_names.clear()
        focuses: List[Focus] = []
        id_counter = 0
        # track roots for mutex policy regardless of generation mode
        roots: List[Focus] = []

        # If node_count is provided, use JS-like stochastic growth algorithm
        if node_count is not None and node_count > 0:
            forced_root_tags = forced_root_tags or {}
            elements = []
            used_titles = set()
            node_categories = {}
            node_ancestry = {}
            node_tags = {}
            # branch index -> chosen layout style for that branch
            branch_style: Dict[int, str] = {}

            def pick_style_for_branch(idx: int) -> str:
                # layout_styles has priority if provided
                if layout_styles and len(layout_styles) > 0:
                    return layout_styles[idx % len(layout_styles)]
                # then weighted mix if provided
                if layout_mix and len(layout_mix) > 0:
                    total = sum(max(0.0, float(w)) for _, w in layout_mix) or 1.0
                    t = (rnd.random() * total)
                    s = 0.0
                    for st, w in layout_mix:
                        s += max(0.0, float(w))
                        if t <= s:
                            return st
                    return layout_mix[-1][0]
                # fallback to global layout_style
                return layout_style

            def get_unique_title(ancestry_categories):
                # prefer library names when requested
                attempts = 0
                while attempts < 200:
                    attempts += 1
                    if use_library_names and self.library:
                        key = rnd.choice(list(self.library.keys()))
                        entry = self.library.get(key, {})
                        title = entry.get('name') or entry.get('id') or key
                    else:
                        adj = rnd.choice(DEFAULT_ADJECTIVES)
                        noun = rnd.choice(DEFAULT_NOUNS)
                        title = f"{adj} {noun}"
                    if title not in used_titles:
                        used_titles.add(title)
                        return title
                # fallback
                title = f"Focus {uuid.uuid4().hex[:6]}"
                used_titles.add(title)
                return title

            # Create starter roots
            active_nodes = []
            nodes_created = 0
            node_index = 0
            for s in range(root_count):
                fid = self._next_id(id_counter); id_counter += 1
                # prefer theme/library if requested
                if use_theme_names and self._theme_pool:
                    title = self._generate_name(False, True, rnd)
                else:
                    title = get_unique_title([])
                # place roots close together; prefer vertical expansion under each root
                fx = Focus(id=fid, name=title, x=start_x + s * spacing[0] * 1, y=start_y, cost=max(5, rnd.randint(7, 25)))
                focuses.append(fx)
                roots.append(fx)
                node_categories[fid] = None
                node_ancestry[fid] = []
                node_tags[fid] = forced_root_tags.get(s) if isinstance(forced_root_tags, dict) else None
                active_nodes.append({'id': fid, 'depth': 0, 'branch': s})
                # choose and record style for this root branch
                st = pick_style_for_branch(s)
                branch_style[s] = st
                with silent_operation("set_branch_style"):
                    setattr(fx, 'branch_style', st)
                nodes_created += 1

            # Growth loop (similar to JS generateTree) with more varied layout
            while nodes_created < node_count and active_nodes:
                parent_idx = rnd.randrange(len(active_nodes))
                parent = active_nodes[parent_idx]
                # If parent already at or beyond max_depth and enforcing cap, stop expanding it
                if enforce_depth_cap and parent.get('depth', 0) >= max_depth:
                    active_nodes.pop(parent_idx)
                    continue
                # Determine number of children using a biased distribution: mostly 1, sometimes 2-4
                mean_children = max(1.0, branch_density / 5.0)
                # sample from Poisson-like (clip at 6)
                lam = max(0.5, mean_children)
                # approx Poisson via Knuth
                k = 0
                p = 1.0
                L = pow(2.718281828, -lam)
                while True:
                    k += 1
                    p *= rnd.random()
                    if p <= L or k >= 6:
                        break
                num_children = max(1, k)
                num_children = min(num_children, 6)
                if isinstance(max_children_per_node, int) and max_children_per_node > 0:
                    num_children = min(num_children, max_children_per_node)
                if num_children <= 0:
                    # no children to add; possibly retire this parent
                    if rnd.random() < 0.8:
                        active_nodes.pop(parent_idx)
                    continue

                # Depth-dependent spacing: deeper nodes compress horizontally a bit
                depth = parent['depth'] + 1
                depth_spacing = max(1, int(spacing[0] * (1.0 - min(0.6, depth * 0.05))))

                # Compute sibling offsets to center children under parent with better distribution
                def sibling_offsets(n):
                    if n <= 1:
                        return [0]
                    # produce offsets like -2,-1,0,1,2 for n=5 or -1.5,-0.5,0.5,1.5 for even counts
                    offsets = []
                    if n % 2 == 1:
                        mid = n // 2
                        for i in range(n):
                            offsets.append(i - mid)
                    else:
                        half = n // 2
                        for i in range(half):
                            offsets.append(i - half - 0.5)
                        for i in range(half):
                            offsets.append(i + 0.5)
                    return offsets

                offsets = sibling_offsets(num_children)

                # Find parent's x (fallback to 0)
                p_focus = next((f for f in focuses if f.id == parent['id']), None)
                parent_x = p_focus.x if p_focus is not None else 0

                # cluster center bias for 'clustered' style
                cluster_center = 0
                parent_branch = parent.get('branch', 0)
                effective_style = branch_style.get(parent_branch, layout_style)
                if effective_style == 'clustered' and rnd.random() < 0.25:
                    # occasional local cluster center near parent
                    cluster_center = rnd.randint(-2, 2)

                created_children: List[Focus] = []
                for i in range(num_children):
                    if nodes_created >= node_count:
                        break
                    fid = self._next_id(id_counter); id_counter += 1
                    # choose title preferring theme
                    if use_theme_names and self._theme_pool:
                        title = self._generate_name(False, True, rnd)
                    else:
                        title = get_unique_title(node_ancestry.get(parent['id'], []))

                    # apply offsets and small jitter to diversify horizontal placement
                    raw_offset = offsets[i]
                    # base child position
                    child_x = parent_x + int(round(raw_offset * depth_spacing))
                    # style-specific perturbations
                    if effective_style == 'organic':
                        # gaussian spread and sine wobble based on depth & index
                        g = int(round(rnd.gauss(0, 1.0) * (1.0 + self._layout_randomness * 2.0)))
                        wobble = int(round(math.sin((nodes_created + i) * 0.5) * (1 + self._layout_randomness)))
                        child_x += g + wobble
                        # Add vertical variation for organic feel
                        if rnd.random() < 0.4:
                            vertical_jitter = rnd.randint(-1, 2) if self._layout_randomness > 0.3 else 0
                            depth += vertical_jitter
                    elif effective_style == 'clustered':
                        child_x += cluster_center + rnd.randint(-1, 1)
                        # Clustered nodes can form at different depths
                        if rnd.random() < 0.25:
                            depth += rnd.choice([-1, 0, 1])
                    elif effective_style == 'radial':
                        # place children in arcs: alternate left/right spread more aggressively
                        ang = (i / max(1, num_children)) * math.pi - (math.pi / 2)
                        radial = int(round(math.cos(ang) * (2 + depth * 0.5)))
                        child_x += radial
                        # Radial layouts: outer edges can go deeper
                        if abs(ang) > math.pi / 4:
                            depth += rnd.randint(0, 1)
                    elif effective_style == 'zigzag':
                        # sharp alternating offsets; also nudge depth
                        direction = -1 if (i % 2 == 0) else 1
                        child_x += direction * (1 + (depth % 2))
                        if rnd.random() < 0.5:
                            depth += 1 if direction > 0 else 0
                    elif effective_style == 'wave':
                        # smoother sin-based lateral and some vertical undulation
                        phase = (i + nodes_created) * 0.7
                        child_x += int(round(math.sin(phase) * (1 + depth * 0.3)))
                        if rnd.random() < 0.4:
                            depth += rnd.choice([0, 1])
                    else:
                        # tidy default jitter controlled by layout_randomness
                        jitter_span = 1 + int(2 * self._layout_randomness)
                        jitter_prob = 0.05 + 0.35 * self._layout_randomness
                        jitter = rnd.randint(-jitter_span, jitter_span) if rnd.random() < jitter_prob else 0
                        child_x += jitter
                        # Even tidy layouts get occasional depth variation
                        if self._layout_randomness > 0.5 and rnd.random() < 0.15:
                            depth += rnd.choice([0, 1])

                    # Ensure depth doesn't go below parent's depth
                    depth = max(parent['depth'] + 1, depth)
                    # Enforce maximum depth
                    if enforce_depth_cap and depth > max_depth:
                        # skip creating this child beyond cap
                        continue
                    child_y = depth

                    # Occasionally place a child side-step (secondary branch) to create less regular trees
                    if rnd.random() < (0.04 + 0.06 * self._layout_randomness):
                        child_x += rnd.choice([-2, 2])
                        # Side-stepped nodes often form at different depths for more interesting branching
                        if rnd.random() < 0.6:
                            depth += rnd.randint(1, 2)
                            child_y = depth

                    # assign subtree_layer as parent's branch index so UI can render distinct frames
                    focus = Focus(id=fid, name=title, x=child_x, y=child_y, cost=max(5, rnd.randint(6, 30)), prerequisites=[parent['id']])
                    with silent_operation("set_subtree_layer_branch_style"):
                        setattr(focus, 'subtree_layer', parent.get('branch', 0))
                        setattr(focus, 'branch_style', effective_style)
                    focuses.append(focus)
                    created_children.append(focus)
                    node_categories[fid] = None
                    node_ancestry[fid] = node_ancestry.get(parent['id'], []) + [node_tags.get(parent['id'])]
                    node_tags[fid] = node_tags.get(parent['id'])
                    # Only expand this child further if depth cap not reached
                    if (not enforce_depth_cap) or (depth < max_depth):
                        active_nodes.append({'id': fid, 'depth': depth, 'branch': parent['branch']})
                    nodes_created += 1

                # Apply sibling-level mutually exclusive constraints if requested
                if mutex_siblings and len(created_children) > 1 and rnd.random() <= max(0.0, min(1.0, mutex_sibling_probability)):
                    ids = [c.id for c in created_children]
                    if mutex_sibling_mode == 'ring':
                        # each child mutex with its next (circular)
                        for idx, fobj in enumerate(created_children):
                            other = ids[(idx + 1) % len(ids)]
                            fobj.mutually_exclusive = list(set((fobj.mutually_exclusive or []) + [other]))
                    elif mutex_sibling_mode == 'pairs':
                        # pair neighbors without wrap
                        for idx in range(0, len(created_children) - 1, 2):
                            a = created_children[idx]
                            b = created_children[idx + 1]
                            a.mutually_exclusive = list(set((a.mutually_exclusive or []) + [b.id]))
                            b.mutually_exclusive = list(set((b.mutually_exclusive or []) + [a.id]))
                    else:
                        # 'all' fully mutually exclusive within the group
                        for fobj in created_children:
                            fobj.mutually_exclusive = list(sorted(set(list((fobj.mutually_exclusive or [])) + [x for x in ids if x != fobj.id])))

                # Occasionally create a cross-link to an earlier node (for variety)
                # reduce cross-link frequency and DO NOT cross-link siblings of the same parent
                if rnd.random() < 0.02 and len(focuses) > 3:
                    target = rnd.choice(focuses[:-1])
                    # skip if the target is the parent itself
                    if target.id == parent['id']:
                        pass
                    else:
                        # skip if target is a sibling under the same parent (prevents intra-sibling links)
                        if parent['id'] in getattr(target, 'prerequisites', []):
                            pass
                        else:
                            if target.id not in node_ancestry.get(parent['id'], []):
                                with silent_operation("append_prerequisite"):
                                    focuses[-1].prerequisites.append(target.id)

                # Remove parent from active_nodes with higher probability as depth increases
                branch_prob = pow(0.75, parent['depth'])
                if rnd.random() > branch_prob:
                    active_nodes.pop(parent_idx)

        else:
            # Keep track of nodes by depth and by branch to help position children beneath parents.
            layers: Dict[int, List[Focus]] = {}

            # Generate roots (depth 0)
            roots: List[Focus] = []
            branch_style: Dict[int, str] = {}

            def pick_style_for_branch(idx: int) -> str:
                if layout_styles and len(layout_styles) > 0:
                    return layout_styles[idx % len(layout_styles)]
                if layout_mix and len(layout_mix) > 0:
                    total = sum(max(0.0, float(w)) for _, w in layout_mix) or 1.0
                    t = (rnd.random() * total)
                    s = 0.0
                    for st, w in layout_mix:
                        s += max(0.0, float(w))
                        if t <= s:
                            return st
                    return layout_mix[-1][0]
                return layout_style

            # Track mapping from node id -> root branch index
            node_branch_index: Dict[str, int] = {}
            for r in range(root_count):
                fid = self._next_id(id_counter); id_counter += 1
                name = self._generate_name(use_library_names, use_theme_names, rnd)
                # keep roots closer to encourage downward growth first
                fx = Focus(id=fid, name=name, x=start_x + r * spacing[0] * 1, y=start_y, cost=max(5, rnd.randint(7, 25)))
                roots.append(fx)
                focuses.append(fx)
                # assign and record style for this root branch
                st = pick_style_for_branch(r)
                branch_style[r] = st
                with silent_operation("set_root_branch_style"):
                    setattr(fx, 'branch_style', st)
                layers.setdefault(0, []).append(fx)
                node_branch_index[fx.id] = r

            # BFS-like generation down to max_depth
            for depth in range(1, max_depth + 1):
                parents = layers.get(depth - 1, [])
                if not parents:
                    break
                layer_nodes: List[Focus] = []
                for p_index, parent in enumerate(parents):
                    # determine number of children for this parent
                    min_b, max_b = branching
                    child_count = rnd.randint(min_b, max_b)
                    if isinstance(max_children_per_node, int) and max_children_per_node > 0:
                        child_count = min(child_count, max_children_per_node)
                    if child_count <= 0:
                        continue
                    # place children horizontally around the parent
                    base_x = parent.x - (child_count - 1) * spacing[0] // 2
                    # determine effective style from this parent's root branch
                    parent_branch = node_branch_index.get(parent.id, 0)
                    effective_style = branch_style.get(parent_branch, layout_style)
                    created_kids: List[Focus] = []
                    for c in range(child_count):
                        fid = self._next_id(id_counter); id_counter += 1
                        name = self._generate_name(use_library_names, use_theme_names, rnd)
                        # base deterministic child placement
                        child_x = base_x + c * spacing[0]
                        child_y = parent.y + spacing[1]
                        # apply style-specific perturbations
                        if effective_style == 'organic':
                            child_x += int(round(rnd.gauss(0, 1.0) * (1.0 + self._layout_randomness)))
                            child_x += int(round(math.sin((depth + c) * 0.6) * (1 + self._layout_randomness)))
                            # Add vertical organic variation
                            if rnd.random() < 0.3:
                                vertical_offset = rnd.randint(-1, 2) if self._layout_randomness > 0.2 else 0
                                child_y += vertical_offset
                        elif effective_style == 'clustered':
                            # bias children toward local cluster of parent
                            child_x += rnd.choice([-1, 0, 1])
                            # Clusters can form at irregular depths
                            if rnd.random() < 0.2:
                                child_y += rnd.choice([0, 1])
                        elif effective_style == 'radial':
                            ang = (c / max(1, child_count)) * math.pi - (math.pi / 2)
                            child_x += int(round(math.cos(ang) * (1 + depth * 0.4)))
                            # Radial depth variation based on position
                            if c == 0 or c == child_count - 1:  # edge children go deeper
                                child_y += rnd.randint(0, 1)
                        elif effective_style == 'zigzag':
                            # alternating lateral and depth pattern
                            direction = -1 if (c % 2 == 0) else 1
                            child_x += direction * (1 + (depth % 2))
                            if rnd.random() < 0.5:
                                child_y += 1 if direction > 0 else 0
                        elif effective_style == 'wave':
                            phase = (c + depth) * 0.7
                            child_x += int(round(math.sin(phase) * (1 + depth * 0.3)))
                            if rnd.random() < 0.3:
                                child_y += rnd.choice([0, 1])
                        else:
                            # tidy jitter
                            if self._layout_randomness > 0:
                                jitter_units = 1 + int(2 * self._layout_randomness)
                                if rnd.random() < (0.2 + 0.4 * self._layout_randomness):
                                    child_x += rnd.randint(-jitter_units, jitter_units)
                                # Add occasional depth variation even in tidy mode
                                if self._layout_randomness > 0.4 and rnd.random() < 0.1:
                                    child_y += rnd.choice([0, 1])
                        focus = Focus(
                            id=fid,
                            name=name,
                            x=child_x,
                            y=child_y,
                            cost=max(5, rnd.randint(6, 30)),
                            prerequisites=[parent.id]
                        )
                        with silent_operation("set_child_subtree_layer"):
                            # Layer children by parent index to allow distinct frames per subtree
                            setattr(focus, 'subtree_layer', p_index)
                            setattr(focus, 'branch_style', effective_style)
                        layer_nodes.append(focus)
                        focuses.append(focus)
                        # propagate branch index for descendants
                        node_branch_index[fid] = parent_branch
                        created_kids.append(focus)
                    # Apply sibling-level mutually exclusive constraints if requested
                    if mutex_siblings and len(created_kids) > 1 and rnd.random() <= max(0.0, min(1.0, mutex_sibling_probability)):
                        ids = [c.id for c in created_kids]
                        if mutex_sibling_mode == 'ring':
                            for idx, fobj in enumerate(created_kids):
                                other = ids[(idx + 1) % len(ids)]
                                fobj.mutually_exclusive = list(set((fobj.mutually_exclusive or []) + [other]))
                        elif mutex_sibling_mode == 'pairs':
                            for idx in range(0, len(created_kids) - 1, 2):
                                a = created_kids[idx]
                                b = created_kids[idx + 1]
                                a.mutually_exclusive = list(set((a.mutually_exclusive or []) + [b.id]))
                                b.mutually_exclusive = list(set((b.mutually_exclusive or []) + [a.id]))
                        else:
                            for fobj in created_kids:
                                fobj.mutually_exclusive = list(sorted(set(list((fobj.mutually_exclusive or [])) + [x for x in ids if x != fobj.id])))
                if not layer_nodes:
                    break
                layers[depth] = layer_nodes

        # Optionally make top-level branches mutually exclusive
        if add_mutex_between_branches and len(roots) > 1:
            root_ids = [r.id for r in roots]
            for r in roots:
                r.mutually_exclusive = [x for x in root_ids if x != r.id]

        # Final pass: compute subtree layout so parents are centered above grouped children
        with silent_operation("layout_subtrees"):
            self._layout_subtrees(focuses, spacing[0])

        # Then compact X positions to avoid collisions on the same grid positions
        self._resolve_collisions(focuses)

        return focuses

    def _resolve_collisions(self, focuses: List[Focus]) -> None:
        """
        Simple collision resolution: if two focuses have same x,y -> shift later ones right.
        Works in grid units; keeps layout deterministic for given order.
        """
        # Group by row (y) and spread nodes to avoid long rightward chains
        by_row: Dict[int, List[Focus]] = {}
        for f in focuses:
            by_row.setdefault(f.y, []).append(f)

        for y, row in by_row.items():
            # sort by x to determine collisions
            row.sort(key=lambda f: f.x)
            occupied = set()
            for f in row:
                if (f.x, f.y) not in occupied:
                    occupied.add((f.x, f.y))
                    continue
                # Collision: try to place symmetrically around the intended x
                base_x = f.x
                offset = 1
                placed = False
                while not placed:
                    # decide exploration order based on randomness to avoid uniform rightward bias
                    rnd = self._last_rnd or random.Random()
                    if rnd.random() < 0.5:
                        candidates = (base_x - offset, base_x + offset)
                    else:
                        candidates = (base_x + offset, base_x - offset)
                    for candidate in candidates:
                        pos = (candidate, y)
                        if pos not in occupied:
                            f.x = candidate
                            occupied.add(pos)
                            placed = True
                            break
                    offset += 1
                    # safety clamp (shouldn't happen normally)
                    if offset > 100:
                        # fall back to pushing right
                        while (f.x, y) in occupied:
                            f.x += 1
                        occupied.add((f.x, y))
                        placed = True

    def _layout_subtrees(self, focuses: List[Focus], spacing_x: int = 2) -> None:
        """
        Arrange nodes so that parents are centered above their children's span.
        This does a tidy bottom-up layout per root; assigns integer x positions in grid units
        with the provided horizontal spacing.
        """
        # Build id -> focus map
        id_map: Dict[str, Focus] = {f.id: f for f in focuses}
        all_ids = set(id_map.keys())

        # Pick a single primary parent for each node to build a forest. Choose the first
        # prerequisite that is at depth y-1 (the most likely real parent) or the first available.
        primary_parent: Dict[str, Optional[str]] = {}
        for f in focuses:
            chosen = None
            for p in f.prerequisites:
                if p in all_ids:
                    parent = id_map.get(p)
                    if parent and parent.y == f.y - 1:
                        chosen = p
                        break
                    if chosen is None:
                        chosen = p
            primary_parent[f.id] = chosen

        # Build children lists from primary_parent mapping
        children: Dict[str, List[str]] = {}
        for child_id, parent_id in primary_parent.items():
            if parent_id:
                children.setdefault(parent_id, []).append(child_id)

        # Chunk wide sibling lists: if a parent's child list exceeds max_siblings,
        # split into groups and push subsequent groups down one row (increase y)
        max_siblings = 4
        moved_nodes = []
        for parent_id, ch_list in list(children.items()):
            if len(ch_list) > max_siblings:
                # partition into chunks
                chunks = [ch_list[i:i+max_siblings] for i in range(0, len(ch_list), max_siblings)]
                # keep first chunk at same depth, for later chunks increase their y and reassign primary_parent
                for ci, chunk in enumerate(chunks[1:], start=1):
                    for cid in chunk:
                        node = id_map.get(cid)
                        if node:
                            node.y += ci  # push down by chunk index
                            # mark moved nodes so later layout picks them at new depth
                            moved_nodes.append(cid)
                # flatten children to only the first chunk for layout purposes (others will be repositioned by updated y)
                children[parent_id] = chunks[0]

        # Identify roots as nodes without a primary parent or at minimal y
        min_y = min((f.y for f in focuses), default=0)
        roots = [f.id for f in focuses if primary_parent.get(f.id) is None or id_map[f.id].y == min_y]

        # Ensure subtree_layer exists on roots and propagate to children where missing
        for r in roots:
            root_node = id_map.get(r)
            if root_node is not None:
                if not hasattr(root_node, 'subtree_layer'):
                    with silent_operation("set_root_subtree_layer"):
                        setattr(root_node, 'subtree_layer', 0)

        # propagate subtree_layer down primary parent chains
        for nid, parent in primary_parent.items():
            if parent and nid in id_map and parent in id_map:
                p_layer = getattr(id_map[parent], 'subtree_layer', None)
                if p_layer is not None and not hasattr(id_map[nid], 'subtree_layer'):
                    with silent_operation("propagate_subtree_layer"):
                        setattr(id_map[nid], 'subtree_layer', p_layer)

        # Compute leaf counts for ordering/stability
        leaf_count: Dict[str, int] = {}

        def compute_leaves(fid: str) -> int:
            if fid in leaf_count:
                return leaf_count[fid]
            ch = children.get(fid, [])
            if not ch:
                leaf_count[fid] = 1
            else:
                leaf_count[fid] = sum(compute_leaves(c) for c in ch)
            return leaf_count[fid]

        for r in roots:
            compute_leaves(r)

        # Order children primarily by leaf count but inject slight randomness for varied layouts
        rnd = self._last_rnd or random.Random()
        lr = self._layout_randomness
        # small jitter helper
        def _rand_key():
            return (rnd.random() - 0.5) * lr

        # Order children by the order they appear (generation order) but stable by leaf count and randomness
        for k in list(children.keys()):
            # pre-shuffle for more variety (deterministic for seed)
            if lr > 0:
                rnd.shuffle(children[k])
            children[k].sort(key=lambda cid: (-leaf_count.get(cid, 0), _rand_key(), cid))

        # First pass: simple left-to-right leaf assignment to compute widths
        next_leaf_x = 0

        def simple_layout(fid: str) -> Tuple[int, int]:
            nonlocal next_leaf_x
            ch = children.get(fid, [])
            if not ch:
                x = next_leaf_x
                id_map[fid].x = x
                next_leaf_x += 1
                return x, x
            ranges = [simple_layout(c) for c in ch]
            left = min(r[0] for r in ranges)
            right = max(r[1] for r in ranges)
            center = int(round((left + right) / 2.0))
            id_map[fid].x = center
            return left, right

        root_gap = 2
        for i, r in enumerate(sorted(roots)):
            if i > 0:
                next_leaf_x += root_gap
            simple_layout(r)

        # Compute subtree widths (leaf units) to reserve horizontal space per subtree
        widths: Dict[str, int] = {}

        def compute_width(fid: str) -> int:
            if fid in widths:
                return widths[fid]
            ch = children.get(fid, [])
            if not ch:
                widths[fid] = 1
            else:
                widths[fid] = sum(compute_width(c) for c in ch)
            return widths[fid]

        for r in roots:
            compute_width(r)

        # Layout using column-major chunking: fill vertically up to max_rows then start new column
        max_rows = 10
        next_leaf_x = 0

        def layout(fid: str) -> Tuple[int, int]:
            nonlocal next_leaf_x
            ch = children.get(fid, [])
            if not ch:
                x = next_leaf_x
                id_map[fid].x = x
                next_leaf_x += widths.get(fid, 1)
                return x, x + widths.get(fid, 1) - 1

            n = len(ch)
            # if small number of children, process sequentially
            if n <= max_rows:
                ranges = [layout(c) for c in ch]
                left = min(r[0] for r in ranges)
                right = max(r[1] for r in ranges)
                center = int(round((left + right) / 2.0))
                id_map[fid].x = center
                return left, right

            # chunk children into vertical columns of up to max_rows each (vertical-first)
            cols = [ch[i:i+max_rows] for i in range(0, n, max_rows)]
            col_ranges = []
            for col_index, col in enumerate(cols):
                # For each child in column, set its y to parent.y + 1 + row_index
                for row_index, cid in enumerate(col):
                    node = id_map.get(cid)
                    if node:
                        node.y = id_map[fid].y + 1 + row_index
                # Reserve column start
                col_start = next_leaf_x
                # Layout each child subtree in this column sequentially; they consume reserved widths
                for cid in col:
                    # layout child; it will consume widths and set x values relative to next_leaf_x
                    _ = layout(cid)
                # After column finished, compute its span
                col_end = next_leaf_x - 1
                col_ranges.append((col_start, col_end))

            # center parent over combined columns
            left = min(r[0] for r in col_ranges)
            right = max(r[1] for r in col_ranges)
            center = int(round((left + right) / 2.0))
            id_map[fid].x = center
            return left, right

        # Layout each root with a small gap between root trees
        root_gap_cols = 2
        for i, r in enumerate(sorted(roots)):
            if i > 0:
                next_leaf_x += root_gap_cols
            layout(r)

        # Apply computed x positions scaled by spacing_x, then optional jitter for variation
        for f in focuses:
            with silent_operation("apply_x_position"):
                f.x = int(round(id_map[f.id].x * spacing_x))

        # inject post-layout jitter controlled by layout_randomness
        if lr > 0:
            jitter_units = max(0, int(round(lr * 3)))
            vertical_jitter = max(0, int(round(lr * 2)))
            if jitter_units > 0 or vertical_jitter > 0:
                for f in focuses:
                    if (self._last_rnd or random).random() < (0.15 + 0.35 * lr):
                        with silent_operation("apply_jitter"):
                            # horizontal jitter
                            if jitter_units > 0:
                                j = (self._last_rnd or random).randint(-jitter_units, jitter_units)
                                f.x += j
                            # vertical jitter for more organic shapes
                            if vertical_jitter > 0 and (self._last_rnd or random).random() < 0.3:
                                v = (self._last_rnd or random).randint(0, vertical_jitter)
                                f.y += v

        # Post-layout smoothing: nudge children groups a bit toward their parent center
        with silent_operation("post_layout_smoothing"):
            # recompute children map according to primary parent policy used earlier
            children_map: Dict[str, List[Focus]] = {}
            for f in focuses:
                for p in f.prerequisites:
                    if p in id_map and id_map[p].y == f.y - 1:
                        children_map.setdefault(p, []).append(f)

            for parent_id, ch_list in children_map.items():
                if not ch_list:
                    continue
                parent = id_map.get(parent_id)
                if not parent:
                    continue
                avg_x = sum(c.x for c in ch_list) / len(ch_list)
                # compute delta to nudge children toward parent's x (small fraction)
                # stronger nudge for closer grouping while preserving some spread
                delta = int(round((parent.x - avg_x) * 0.45))
                if delta != 0:
                    for c in ch_list:
                        c.x += delta

        # Additional proximity smoothing pass: ensure children remain within a window around parent
        with silent_operation("proximity_smoothing"):
            for parent_id, ch_list in children_map.items():
                parent = id_map.get(parent_id)
                if not parent:
                    continue
                # Allowed horizontal window (in grid units)
                window = max(2, int(round(2 + (1.5 * (self._layout_randomness)))))
                for c in ch_list:
                    # clamp child x within parent's x +/- window
                    if c.x < parent.x - window:
                        c.x = parent.x - window
                    elif c.x > parent.x + window:
                        c.x = parent.x + window

    # Integration helper -------------------------------------------------
    def apply_to_editor(self, editor, focuses: List[Focus], shift_if_conflict: bool = True) -> List[Focus]:
        """
        Add generated focuses to a running editor (HOI4FocusTreeGenerator instance).

        Behavior:
          - If a generated focus id collides with existing ids, it will be renamed
            (suffixing _dupN) unless `shift_if_conflict` is False, in which case
            existing focuses are preserved and generator ids will get numeric suffixes.

        Returns
        -------
        List[Focus]
            The list of focuses actually added (ids may differ from the input list).
        """
        added: List[Focus] = []
        existing_ids = {f.id for f in editor.focuses}

        # If editor is empty (new project), shift generated focuses so their
        # minimum x/y start at 0,0 in editor-grid coordinates. This ensures
        # newly-created projects launch with content anchored at the grid origin
        # both in the editor and when exported to HOI4.
        with silent_operation("shift_focuses_to_origin"):
            if len(getattr(editor, 'focuses', [])) == 0 and focuses:
                xs = [getattr(f, 'x', 0) for f in focuses]
                ys = [getattr(f, 'y', 0) for f in focuses]
                min_x = min(xs)
                min_y = min(ys)
                if min_x != 0 or min_y != 0:
                    for f in focuses:
                        with silent_operation("adjust_focus_x"):
                            f.x = int(round(f.x - min_x))
                        with silent_operation("adjust_focus_y"):
                            f.y = int(round(f.y - min_y))

        for f in focuses:
            original_id = f.id
            if f.id in existing_ids:
                # produce a non-colliding id
                counter = 1
                new_id = f"{f.id}_dup{counter}"
                while new_id in existing_ids:
                    counter += 1
                    new_id = f"{f.id}_dup{counter}"
                f.id = new_id
            # append to editor data structures
            editor.focuses.append(f)
            editor.canvas.add_focus_node(f)
            existing_ids.add(f.id)
            added.append(f)

        # recreate connections according to prerequisites
        for f in added:
            for prereq in f.prerequisites:
                # if prereq was renamed in the previous step, map it
                # simple policy: assume prereq exists in editor (it will for internal generation)
                # otherwise skip silently
                with silent_operation("create_prereq_connection"):
                    if prereq in existing_ids:
                        editor.canvas.create_connection(prereq, f.id)
                    else:
                        # attempt to find by prefix match
                        candidates = [e.id for e in editor.focuses if e.id.startswith(prereq)]
                        if candidates:
                            editor.canvas.create_connection(candidates[0], f.id)
                # If this focus has exactly one mutually exclusive partner, also
                # create a visual connection from the prereq to that partner (if present)
                with silent_operation("create_mutex_connection"):
                    mex = getattr(f, 'mutually_exclusive', None)
                    if isinstance(mex, (list, tuple)) and len(mex) == 1:
                        mex_id = str(mex[0])
                        if mex_id in existing_ids:
                            with silent_operation("create_mutex_direct"):
                                editor.canvas.create_connection(prereq, mex_id)
                        else:
                            candidates = [e.id for e in editor.focuses if e.id.startswith(mex_id)]
                            if candidates:
                                with silent_operation("create_mutex_candidate"):
                                    editor.canvas.create_connection(prereq, candidates[0])

        editor.update_status()
        with silent_operation("show_status_message"):
            editor.statusBar().showMessage(f"Applied generated tree ({len(added)} focuses)")

        return added

# endregion

# If module executed directly, produce a small sample file (non-UI) demonstration.
if __name__ == "__main__":
    gen = FocusTreeGenerator()
    sample = gen.generate(tree_id="demo", root_count=2, max_depth=3, branching=(1,2), seed=123)
    for f in sample:
        print(f"ID: {f.id}, NAME: {f.name}, X: {f.x}, Y: {f.y}, PR: {f.prerequisites}")