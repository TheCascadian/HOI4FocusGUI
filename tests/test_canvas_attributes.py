"""Attribute-completeness test for FocusTreeCanvas (source/_focusGUI.py).

FocusTreeCanvas.__init__ sets ~125 plain-data attributes (as opposed to
methods / inherited QGraphicsScene API) that later code -- settings
loading (apply_settings), painting (draw_grid), and node creation
(add_focus_node / add_event_node / add_note / _create_note_item) -- reads
back via direct ``self.xxx`` access (not ``getattr(..., default)``,
which would tolerate absence). If __init__ ever stops setting one of
these, those later calls would raise AttributeError the first time a
project is loaded/painted/edited.

The attribute list below was derived by parsing the AST of
FocusTreeCanvas.__init__, apply_settings, draw_grid, add_focus_node,
add_event_node, add_note, and _create_note_item, and collecting every
``self.<name>`` reference, then subtracting method names (callables
that always exist on the class) and inherited QGraphicsScene API
methods (addItem, addLine, items, removeItem, setSceneRect,
setItemIndexMethod, update, views).
"""

from PyQt6.QtWidgets import QMainWindow

from _focusGUI import FocusTreeCanvas

# NOTE: keep in sync with the class if these methods are heavily
# refactored; regenerate via the AST approach described above if so.
EXPECTED_DATA_ATTRIBUTES = [
    "_autosave_in_progress",
    "_autosave_timer",
    "_cull_margin",
    "_cull_timer",
    "_event_event_links",
    "_event_focus_links",
    "_frame_timer",
    "_frame_variant_counters",
    "_frames_dirty",
    "_frames_transition_in_progress",
    "_grid_visible",
    "_hidden_tag_index",
    "_last_autosave_path",
    "_last_scaling_active",
    "_last_zoom_scale",
    "_layer_frame_items",
    "_layout_in_progress",
    "_leaf_colors",
    "_leaf_dirty",
    "_leaf_of_node",
    "_lineage_active",
    "_lineage_colors",
    "_lineage_dirty",
    "_lineage_ids",
    "_lineage_of_node",
    "_note_connections",
    "_note_event_links",
    "_note_focus_links",
    "_notes_by_id",
    "_notes_items",
    "_palette_seed",
    "_precomputed_lineage_rects",
    "_preferences",
    "_prev_frames_enabled",
    "_reflow_timer",
    "_render_stack_overrides",
    "_show_hidden_branches_by_tag",
    "_spatial_index",
    "_temp_link_item",
    "_title_pill_pixmap",
    "_visible_nodes_cache",
    "auto_layout_enabled",
    "autosave_enabled",
    "autosave_interval_min",
    "autosave_overwrite",
    "autosave_rotate",
    "autosave_rotate_count",
    "color_lines_by_lineage",
    "connection_line_width",
    "connection_lod_threshold",
    "connection_mode",
    "connection_start",
    "connections",
    "country_event_mode",
    "country_event_ssaa",
    "culling_enabled",
    "culling_min_connections",
    "culling_min_nodes",
    "default_focus_color",
    "drag_to_link_mode",
    "editor",
    "enable_dynamic_title_icon_scaling",
    "event_desc_font_size",
    "event_desc_offset_x",
    "event_desc_offset_y",
    "event_nodes",
    "event_options_font_size",
    "event_options_offset_x",
    "event_options_offset_y",
    "event_title_font_size",
    "event_title_offset_x",
    "event_title_offset_y",
    "focus_color_overrides",
    "focus_icon_offset_x",
    "focus_icon_offset_y",
    "focus_pill_offset_x",
    "focus_pill_offset_y",
    "focus_title_font_size",
    "focus_title_offset_x",
    "focus_title_offset_y",
    "frames",
    "frames_enabled",
    "icon_supersample_scale",
    "icon_view_icon_max",
    "icon_view_mode",
    "icon_view_show_background",
    "layer_colors",
    "layer_visibility",
    "muted",
    "mutex_connectors",
    "mutex_icon_display_scale",
    "mutex_icon_supersample_scale",
    "network_colors",
    "nodes",
    "note_connection_curve_strength",
    "note_defaults",
    "notes_enabled",
    "parent",
    "prefer_app_settings",
    "prefer_pillow_tga",
    "prereq_link_mode",
    "render_node_ids",
    "show_frame_labels",
    "show_layer_frames",
    "show_lineage_frames",
    "show_network_frames",
    "show_subtree_frames",
    "simple_render_zoom_threshold",
    "title_icon_scale_max_multiplier",
    "title_icon_scale_zoom_threshold",
    "title_outline_color",
    "title_outline_enabled",
    "title_outline_thickness",
    "title_pill_image_path",
    "title_pill_mode",
    "title_pill_padding",
    "visualizer_lineage_mode",
    "z_background",
    "z_connections",
    "z_grid",
    "z_nodes",
    "zoom_cull_margin_factor",
    "zoom_cull_min_margin",
    "zoom_cull_threshold",
    "zoom_culling_enabled",
]


def test_canvas_has_all_expected_attributes_after_construction(qtbot):
    parent = QMainWindow()
    qtbot.addWidget(parent)
    canvas = FocusTreeCanvas(parent)

    missing = [name for name in EXPECTED_DATA_ATTRIBUTES if not hasattr(canvas, name)]
    assert not missing, f"FocusTreeCanvas is missing attributes expected immediately after __init__: {missing}"


def test_canvas_attribute_count_sanity():
    # Guard against this list accidentally being emptied/truncated in a
    # future edit, which would make the test above vacuously pass.
    assert len(EXPECTED_DATA_ATTRIBUTES) > 100
