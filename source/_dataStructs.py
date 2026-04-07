from _imports import (
    # Standard library
    os,
)

import logging
from error_handler import silent_operation


# Avoid circular import when _imports tries to pull _dataStructs types.
# Set the environment flag BEFORE importing _imports so it won't try to import
# this module while it is still being initialized.
os.environ["FOCUS_SKIP_DATASTRUCTS"] = "1"
from _imports import *
os.environ.pop("FOCUS_SKIP_DATASTRUCTS", None)

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Structured Paradox script values captured during parsing/export.
ScriptValue = Union[str, int, float, bool, Dict[str, Any], List[Any]]

def _normalize_string_list(values: Optional[Union[str, Iterable[Any]]]) -> List[str]:
    """Return a defensive list[str] for iterable or scalar inputs."""

    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    with silent_operation("Normalize string list iteration"):
        return [str(item) for item in values if item is not None]
    return [str(values)]

# -------------------------
# Data structures
# -------------------------

# region Command Specification

@dataclass
class CommandSpec:
    """Lightweight command registration record used by the GUI.

    Attributes:
        cid: Stable command identifier.
        label: Human-friendly label for menus/toolbars.
        callback: Callable to invoke when the command is executed.
        default: Default command palette text or alias.
        shortcut: Optional configurable keybinding (text form, e.g. "Ctrl+S").
        qshortcut: Bound QShortcut instance when registered (set by UI code).
        category: Optional grouping/category shown in menus.
        widget_scope: Optional named widget; if provided, the shortcut is scoped
            to that widget and its children only.
    """

    cid: str
    label: str
    callback: callable
    default: Optional[str] = None
    shortcut: Optional[str] = None
    qshortcut: Optional['QShortcut'] = None
    category: Optional[str] = None
    widget_scope: Optional[str] = None

# endregion

# region Focus Data Structures

@dataclass
class Focus:
    """A single national focus node used by the editor, parser, and exporter.

    Attributes:
        id: Script identifier referenced by other focuses and localisation.
        name: Display name (usually localisation key) shown in UI lists.
        x, y: Grid coordinates in the editor canvas.
        cost: Number of days required to complete the focus.
        description: Optional long description (localisation text snippet).
        prerequisites: Focus IDs that must be completed before this one.
        mutually_exclusive: Focus IDs that cannot be active with this focus.
        search_filters: Search hints exported to the `search_filters` block.
        available: Raw HOI4 block controlling availability/visibility.
        visible: Optional raw HOI4 block for explicit `visible` conditions.
        bypass: Raw HOI4 block allowing bypassing the focus.
        completion_reward: Raw HOI4 block executed when completed.
        select_effect: Optional raw HOI4 block executed when selected.
        remove_effect: Optional raw HOI4 block executed when removed.
        cancel: Optional raw HOI4 block executed when cancelled.
        complete_tooltip: Optional structured tooltip block appended in UI.
        ai_will_do: Legacy numeric AI weight or parsed structure for export.
        ai_will_do_block: Structured AI block that supersedes `ai_will_do`.
        allow_branch: Optional allow_branch condition content (e.g., has_completed_focus = ...).
        network_id: Optional network identifier used by the editor layout.
        icon: Optional sprite name referenced by generated .gfx definitions.
        relative_position_id: Focus ID used for relative placement in graph.
        prerequisites_grouped: Whether grouped prerequisites should be exported.
        prerequisites_groups: Rich prerequisite definitions (type/items mapping).
        available_if_capitulated: Exported as `available_if_capitulated = yes`.
        cancel_if_invalid: Exported as `cancel_if_invalid = yes` when True.
        continue_if_invalid: Exported as `continue_if_invalid = yes` when True.
        will_lead_to_war_with: Optional list of country tags referenced by the focus.
        hidden: Whether this focus is hidden in the editor by default.
        hidden_tags: Tag-based grouping for hidden focuses.
        avail_conditions: Parsed availability hints surfaced in the UI.
        imported_category: Optional category derived during import.
        raw_unparsed: Unknown script fragments preserved for round-trip safety.
        has_unparsed: Convenience flag indicating presence of unknown blocks.
        clean_raw: Raw script with unknown blocks stripped for editing.
    """

    id: str
    name: str = ""
    x: int = 0
    y: int = 0
    cost: int = 10
    description: str = ""
    prerequisites: List[str] = field(default_factory=list)
    mutually_exclusive: List[str] = field(default_factory=list)
    search_filters: List[str] = field(default_factory=list)
    available: str = ""
    visible: Optional[str] = None
    bypass: str = ""
    completion_reward: str = ""
    select_effect: Optional[str] = None
    remove_effect: Optional[str] = None
    cancel: Optional[str] = None
    complete_tooltip: Optional[str] = None
    ai_will_do: ScriptValue = 1
    ai_will_do_block: Optional[Dict[str, Any]] = None
    allow_branch: str = ""
    network_id: Optional[int] = None
    icon: Optional[str] = None
    relative_position_id: Optional[str] = None
    prerequisites_grouped: bool = False
    prerequisites_groups: List[dict] = field(default_factory=list)
    available_if_capitulated: bool = False
    cancel_if_invalid: bool = False
    continue_if_invalid: bool = False
    will_lead_to_war_with: List[str] = field(default_factory=list)
    hidden: bool = False
    hidden_tags: List[str] = field(default_factory=list)
    avail_conditions: List[dict] = field(default_factory=list)
    imported_category: Optional[str] = None
    raw_unparsed: List[str] = field(default_factory=list)
    has_unparsed: bool = False
    clean_raw: Optional[str] = None

    def __post_init__(self) -> None:
        """Normalise collection fields for predictable downstream usage."""

        self.prerequisites = _normalize_string_list(self.prerequisites)
        self.mutually_exclusive = _normalize_string_list(self.mutually_exclusive)
        self.search_filters = _normalize_string_list(self.search_filters)
        self.hidden_tags = _normalize_string_list(self.hidden_tags)
        self.will_lead_to_war_with = _normalize_string_list(self.will_lead_to_war_with)

        raw_blocks = self.raw_unparsed
        if not raw_blocks:
            self.raw_unparsed = []
        elif isinstance(raw_blocks, (list, tuple, set)):
            self.raw_unparsed = [str(chunk) for chunk in raw_blocks if chunk is not None]
        else:
            self.raw_unparsed = [str(raw_blocks)]

        if not isinstance(self.prerequisites_groups, list):
            self.prerequisites_groups = list(self.prerequisites_groups or [])

# endregion

# region Event Data Structure

@dataclass
class Event:
    """Represents a simple HOI4 country event to link with focuses.

    Position: x/y are grid coordinates for alignment; free_x/free_y are optional pixel positions
    when the node is moved off-grid (Ctrl-drag). If set, the visual position will use free_x/free_y.
    """
    id: str
    title: str = ""
    description: str = ""
    x: int = 0
    y: int = 0
    # Free placement (scene coordinates in pixels) when off-grid
    free_x: Optional[float] = None
    free_y: Optional[float] = None
    # HOI4 script blocks
    trigger: str = ""
    options_block: str = ""  # Full option blocks like: option = { name = XXX ... }\noption = { ... }

# endregion

# region Focus Branch & Tree Structures

@dataclass
class FocusBranch:
    """A logical branch or group of focuses within a focus tree.

    This structure is intentionally flexible to support multiple producers
    (imported data, heuristics, manual editing). It keeps a canonical field
    ``root_focus_ids`` for the branch roots while maintaining ``focuses`` as a
    list of all member focus IDs for backward compatibility.

    Attributes:
        id: Stable identifier for the branch (often derived from allow_branch).
        name: Human-friendly name of the branch.
        focuses: All member focus IDs in the branch (backwards compatibility).
        root_focus_ids: Focus IDs considered roots of this branch.
        color: Optional color string for visual grouping in the editor.
        hidden: Whether this branch is hidden by default in the editor.
        tags: Arbitrary labels (e.g. ideology tags).
        x_offset, y_offset: Optional visual offsets for grouping overlays.
        metadata: Arbitrary metadata captured during parsing/export operations.
        focus_ids: Alias accepted when importing legacy structures that used the
            ``focus_ids`` field instead of ``focuses``.
    """

    id: str
    name: str = ""
    focuses: List[str] = field(default_factory=list)
    root_focus_ids: List[str] = field(default_factory=list)
    color: Optional[str] = None
    hidden: bool = False
    tags: List[str] = field(default_factory=list)
    x_offset: Optional[int] = None
    y_offset: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    focus_ids: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Normalise list-like fields and support legacy focus_ids input."""

        self.focuses = _normalize_string_list(self.focuses)
        self.root_focus_ids = _normalize_string_list(self.root_focus_ids)
        self.tags = _normalize_string_list(self.tags)

        normalized_focus_ids = _normalize_string_list(self.focus_ids)
        if not normalized_focus_ids and self.focuses:
            normalized_focus_ids = list(self.focuses)
        if normalized_focus_ids and not self.focuses:
            self.focuses = list(normalized_focus_ids)
        self.focus_ids = normalized_focus_ids

        if not isinstance(self.metadata, dict):
            with silent_operation("Convert metadata to dict"):
                self.metadata = dict(self.metadata or {})
            if not isinstance(self.metadata, dict):
                self.metadata = {}

@dataclass
class FocusTree:
    """Represents a complete HOI4 focus tree used by the app and exporter.

    Canonical fields (relied upon across modules):
        id: Script identifier for the tree.
        tree_name: Human-friendly name used for filenames if present.
        focuses: List of Focus nodes belonging to this tree.
        search_filter_priorities: Mapping of search filter keys to priority ints.
        country_factor: Country selection factor (exported under country = { factor = n }).
        is_default: Whether the tree is the default for a country set.
        reset_on_civilwar: Exported as reset_on_civilwar yes/no.
        initial_show_position: Optional focus id to show initially in UI.
        shared_focuses: References to shared_focus nodes.

    Extra editor metadata:
        country_tag: Optional country tag used by the editor.
        branches: Optional manual/heuristic grouping of focuses.
        continuous_focus_position: Free-form map for future extensions.
    """

    id: str
    tree_name: str = ""
    focuses: List[Focus] = field(default_factory=list)
    search_filter_priorities: Dict[str, int] = field(default_factory=dict)
    country_factor: int = 0
    is_default: bool = False
    reset_on_civilwar: bool = False
    initial_show_position: Optional[str] = None
    shared_focuses: List[str] = field(default_factory=list)

    # Editor/auxiliary data
    branches: List[FocusBranch] = field(default_factory=list)
    continuous_focus_position: Dict[str, Any] = field(default_factory=dict)
    country_tag: str = ""

    # ---------------
    # Helper methods
    # ---------------
    def add_branch(self, branch: 'FocusBranch') -> None:
        """Add a branch, replacing any with the same id."""
        if not hasattr(self, 'branches'):
            self.branches = []
        # Remove existing with same id
        self.branches = [b for b in self.branches if getattr(b, 'id', None) != getattr(branch, 'id', None)]
        self.branches.append(branch)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize tree to a JSON-friendly dict."""
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'FocusTree':
        """Deserialize a FocusTree from a dict produced by asdict/to_dict."""
        focuses_data = data.get('focuses', []) or []
        branches_data = data.get('branches', []) or []
        focuses = [Focus(**fd) if not isinstance(fd, Focus) else fd for fd in focuses_data]
        branches = [FocusBranch(**bd) if not isinstance(bd, FocusBranch) else bd for bd in branches_data]

        return FocusTree(
            id=data.get('id', ''),
            tree_name=data.get('tree_name', ''),
            focuses=focuses,
            search_filter_priorities=data.get('search_filter_priorities', {}) or {},
            country_factor=int(data.get('country_factor', 0) or 0),
            is_default=bool(data.get('is_default', False)),
            reset_on_civilwar=bool(data.get('reset_on_civilwar', False)),
            initial_show_position=data.get('initial_show_position'),
            shared_focuses=list(data.get('shared_focuses', []) or []),
            branches=branches,
            continuous_focus_position=data.get('continuous_focus_position', {}) or {},
            country_tag=data.get('country_tag', ''),
        )

# endregion

# -------------------------
# GUI Editor data structures
# -------------------------

# region GUI Data Structures

PixelOrPercent = Union[int, Tuple[str, int]]

@dataclass
class GuiElement:
    """Base class representing a GUI element within a HOI4 .gui file.

    position/size components accept either pixel integers or explicit percentage
    tuples of the form ('pct', int_percent) to preserve authored semantics.
    """

    id: str
    name: str
    position: Tuple[PixelOrPercent, PixelOrPercent] = (0, 0)
    size: Tuple[PixelOrPercent, PixelOrPercent] = (100, 30)
    visible: bool = True
    parent_id: Optional[str] = None
    anchor: Optional[str] = None
    align: Optional[str] = None
    tooltip: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    children: List[str] = field(default_factory=list)

@dataclass
class WindowType(GuiElement):
    """Top-level window definition (windowType in HOI4)."""

    moveable: bool = False
    draggable: bool = False
    background: Optional[str] = None
    focusable: bool = True

@dataclass
class ContainerWindowType(GuiElement):
    """Container window (containerWindowType in HOI4)."""

    scrollable: bool = False
    clip_children: bool = False

@dataclass
class ButtonType(GuiElement):
    """Interactive button (buttonType in HOI4)."""

    text: Optional[str] = None
    texture: Optional[str] = None
    on_click: Optional[str] = None
    on_show: Optional[str] = None

@dataclass
class IconType(GuiElement):
    """Icon element (iconType in HOI4)."""

    sprite: Optional[str] = None
    sprite_source: str = "spriteType"
    framed: bool = False

@dataclass
class GenericGuiElement(GuiElement):
    """Fallback element for HOI4 GUI types that do not have a dedicated class."""

    keyword: str = "genericType"

@dataclass
class GuiProject:
    """Root container for a collection of GUI elements."""

    id: str
    elements: Dict[str, GuiElement] = field(default_factory=dict)
    root_windows: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_element(self, element: GuiElement, parent_id: Optional[str] = None) -> None:
        """Insert an element into the project, maintaining parent/child relationships."""

        if element.id in self.elements:
            raise ValueError(f"GUI element with id '{element.id}' already exists in project '{self.id}'.")

        effective_parent: Optional[str] = None
        if parent_id and parent_id != element.id:
            if parent_id in self.elements:
                effective_parent = parent_id
            else:
                logging.warning("GuiProject.add_element: requested parent '%s' not found; promoting '%s' to root", parent_id, element.id)

        element.parent_id = effective_parent
        self.elements[element.id] = element

        if effective_parent:
            parent = self.elements[effective_parent]
            if element.id not in parent.children:
                parent.children.append(element.id)
            # Ensure element is not listed as root when it acquires a parent
            if element.id in self.root_windows:
                self.root_windows.remove(element.id)
        else:
            if element.id not in self.root_windows:
                self.root_windows.append(element.id)

    def remove_element(self, element_id: str) -> None:
        """Remove an element and detach it from its parent."""

        element = self.elements.pop(element_id, None)
        if not element:
            return

        if element.parent_id:
            parent = self.elements.get(element.parent_id)
            if parent:
                parent.children = [cid for cid in parent.children if cid != element_id]
        else:
            self.root_windows = [rid for rid in self.root_windows if rid != element_id]

        for child_id in list(element.children):
            self.remove_element(child_id)

    def reparent_element(self, element_id: str, new_parent_id: Optional[str]) -> None:
        """Move element under a new parent while preserving tree integrity."""

        if element_id not in self.elements:
            raise ValueError(f"Element '{element_id}' not found.")
        if new_parent_id is not None and new_parent_id not in self.elements:
            raise ValueError(f"Parent '{new_parent_id}' not found.")
        if new_parent_id == element_id:
            raise ValueError("An element cannot parent itself.")

        # Prevent cycles by ensuring new parent isn't a descendant of the element
        if new_parent_id is not None:
            to_visit = [new_parent_id]
            visited: set[str] = set()
            while to_visit:
                current = to_visit.pop()
                if current == element_id:
                    raise ValueError("Cannot reparent: target parent is a descendant of the element.")
                if current in visited:
                    continue
                visited.add(current)
                cur_el = self.elements.get(current)
                if cur_el:
                    to_visit.extend(cur_el.children)

        element = self.elements[element_id]
        old_parent_id = element.parent_id

        if old_parent_id:
            old_parent = self.elements.get(old_parent_id)
            if old_parent:
                old_parent.children = [cid for cid in old_parent.children if cid != element_id]
        else:
            self.root_windows = [rid for rid in self.root_windows if rid != element_id]

        element.parent_id = new_parent_id

        if new_parent_id:
            parent = self.elements[new_parent_id]
            if element_id not in parent.children:
                parent.children.append(element_id)
        else:
            if element_id not in self.root_windows:
                self.root_windows.append(element_id)

# endregion