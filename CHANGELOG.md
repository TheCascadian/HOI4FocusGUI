# Changelog

All notable changes made to **HOI4 Focus GUI** (`v1.0.12`, bumped from `1.0.11`) across this session.

## Build, Performance & Onboarding Updates

* **Parallel PyInstaller build path resolution (`scripts/build_appimage.sh`, `scripts/build_windows_exe.sh`):**

  * Updated `--add-data` and entrypoint paths to use absolute `$ROOT_DIR` paths (and `winepath -w "$ROOT_DIR"` for Wine PyInstaller) so `--specpath` under `build/linux-work` and `build/win64-work` resolves `source/_assets`, `source/fonts`, and `source/version.txt` reliably during `scripts/build_all.sh`.

* **Focus tree generator optimization (`source/_focusGenerator.py`, `source/_focusGUI.py`):**

  * Replaced the 200-iteration `get_unique_title()` rejection-sampling loop and per-node `list(self.library.keys())` rebuild with a cached, pre-shuffled title pool and `O(1)` fallback.

  * Replaced the `O(N^2)` `next((f for f in focuses if f.id == parent['id']), None)` parent lookup with `O(1)` stored `x` / `_id_to_focus` map lookups and `O(1)` swap-and-pop removal from `active_nodes`.

  * Removed the dead `simple_layout` pass in `_layout_subtrees()`, precomputed `_PRECOMPUTED_SIBLING_OFFSETS` and `_poisson_L`, switched `_resolve_collisions()` to per-row `int` sets (`occupied_x`), and used `rnd.getrandbits(24)` in `_next_id()` when a seeded RNG is active (~19x speedup at 5,000 nodes).

  * Replaced the `O(N^2)` ID collision scan in `append_generated_nodes()` (`source/_focusGUI.py`) with a set lookup (`existing_ids`), and switched `apply_to_editor()` prefix fallbacks to `next(..., None)`.

* **Lineage highlight & hover renderer performance (`source/_focusGUI.py`):**

  * Updated `_collect_lineage_ids()` to traverse existing `connections_in` / `connections_out` references in `O(lineage)` instead of scanning all `self.nodes` on every hover.

  * Added an early exit in `clear_highlight()` when `_lineage_active` is `False`, skipped redundant `setOpacity()` calls when opacity is unchanged (`_set_opacity_if_changed()`), and batched view repaints with `setUpdatesEnabled(False/True)` across `highlight_lineage()` and `clear_highlight()`.

* **Interactive, layperson-friendly Starter Tutorial (`source/_tutorial.py`, `source/_focusGUI.py`):**

  * Replaced the blocking static `QDialog` with `InteractiveTutorialCoach`, a non-modal, draggable floating guide paired with a click-through `_SpotlightOverlay` that pulses around target UI widgets.

  * Added plain-English explanations (Tree ID, Country Tag, Focuses, Prerequisites, Mutually Exclusive choices, and Export), live 250 ms action detection ("Try it now" missions that auto-complete as you work), one-click **"Do it for me"** helpers, and a **"Load 3-Focus Mini-Example"** sandbox loader.

* **Focus & Event Properties dialogs not opening (`source/_dialog.py`):**

  * Replaced PyQt5-only `QFontDatabase.FixedFont` with `QFontDatabase.SystemFont.FixedFont` in `FocusEditDialog.setup_ui()` and `EventEditDialog.setup_ui()` so opening properties via double-click, `Enter`, or right-click **Edit Properties** no longer raises `AttributeError` before `exec()`.

* **Repeated `FileNotFoundError` log spam & paint overhead (`source/_utils.py`):**

  * Updated `pixmap_from_file_via_pillow()` to check `os.path.isfile(path)` and negative-cache missing paths in `_MISSING_IMAGE_PATHS` (logging once at `debug` level instead of logging thousands of `FileNotFoundError` tracebacks on every `FocusNode.paint()`).

* **Generate Focuses dialog tooltips (`source/_focusGUI.py`):**

  * Added descriptive tooltips to all Basic and Advanced controls in `show_generate_dialog()` (sibling mutex modes/probability, networks, background execution, randomized effects/modifiers, availability conditions, and effect categories).

## Bug Fixes

* **Auto-save & settings persistence (`source/_focusGUI.py`, `source/_dialog.py`):**

  * Initialized `prefer_app_settings`, `muted`, and `autosave_*` attributes on `HOI4FocusTreeGenerator.__init__`.

  * Restored `prefer_app_settings` in `load_settings()` so `app_settings.json` overrides are honored on startup.

  * Preserved full `preferences` (including `autosave_*` flags) in `app_settings_last.json` inside `save_settings()`.

  * Updated `ensure_app_dirs()` so user-chosen `settings_path` locations inside `Path.home()` or `cwd` are not treated as uninitialized and overwritten.

  * Updated `SettingsDialog` **Apply** to persist settings unconditionally rather than only when `prefer_app_settings` is enabled.

* **Note editing garbling & missing cursor (`source/_nodes.py`):**

  * Switched `NoteNode`, `NoteNode._title_item`, and `NoteNode._text_item` from `DeviceCoordinateCache` to `NoCache` so active text editing and cursor (`|`) blinking render cleanly without stale cached tiles.

* **Accidental deletions while typing in notes (`source/_nodes.py`):**

  * Guarded `FocusTreeView.keyPressEvent()` when a `QGraphicsTextItem` with `TextEditorInteraction` holds scene focus (or when `event.isAccepted()` is true) so `Backspace`/`Delete` inside notes never deletes selected focuses, events, or notes on the canvas.

  * Updated `Backspace` on the canvas to prompt for confirmation (`confirm=True`), matching `Delete`.

* **Incomplete focus deletion cleanup (`source/_commands.py`, `source/_focusGUI.py`):**

  * Updated `DeleteFocusCommand.redo()` and `delete_focus_node()` to also clean deleted focus IDs out of other focuses' `prerequisites_groups` and `relative_position_id` fields so removed focuses are not left dangling in connections.

* **Live connection graph updates (`source/_commands.py`, `source/_focusGUI.py`):**

  * Added `FocusTreeCanvas.sync_focus_connections()` and wired it into `EditFocusCommand.redo()`/`undo()` and `edit_focus()` so prerequisite `LShapedConnectionLine` items and node visuals update immediately without reloading the project.

* **Disappearing notes (`source/_nodes.py`, `source/_focusGUI.py`):**

  * Added a confirmation prompt before deleting a note via its top-right `X` close button.

  * Automatically enabled `canvas.notes_enabled` in `load_project_from_dict()` when loading a project that contains notes.

* **Position tooltip clipping & focus properties access (`source/_nodes.py`, `source/_focusGUI.py`):**

  * Clamped both `bx` and `by` inside `rect` for the hover coordinate tooltip in `FocusNode.paint()` so the `0, 0` box is never cut off at the bottom edge.

  * Moved the `QGraphicsTextItem` focus guard out of `edit_focus()` into the `Enter` keybind (`_edit_focus_shortcut`) so double-clicking or right-clicking a focus always opens **Edit Properties** even after editing a note.

* **Qt6 compatibility (`source/_nodes.py`):**

  * Removed the Qt5-only `QGraphicsView.OptimizationFlag.DontClipPainter` call in `EnhancedGraphicsView.__init__` that caused `AttributeError` across GUI tests once unmasked.

## Performance & Renderer

* **Zoomed-in rendering & icon/text caching (`source/_nodes.py`, `source/_utils.py`):**

  * Added `_scaled_icon_cache` on `FocusNode` to cache smooth-scaled `QPixmap` icons instead of rescaling on every `paint()`.

  * Added a glyph `QPainterPath` cache in `draw_outlined_text()` so text outlines are translated rather than rebuilt from scratch every frame.

* **Grid & viewport rendering (`source/_focusGUI.py`, `source/_nodes.py`):**

  * Replaced `FocusTreeCanvas.draw_grid()`'s 1,200+ full-scene `QGraphicsLineItem`s with `FocusTreeCanvas.drawBackground()`, drawing only grid lines inside the visible viewport `rect` with muted low-contrast colors (`alpha 45` minor, `alpha 75` major) and skipping minor lines when zoomed far out.

  * Removed `MinimalViewportUpdate`, `DontAdjustForAntialiasing`, and `CacheBackground` on `EnhancedGraphicsView` in favor of `SmartViewportUpdate` + `CacheNone`, keeping `Antialiasing` (down to `0.2` zoom), `TextAntialiasing`, and `SmoothPixmapTransform` enabled and eliminating `scene.invalidate()` on pan release.

  * Replaced `NoteNode`'s per-frame 16px CPU `QGraphicsDropShadowEffect` with a lightweight vector drop shadow drawn inside an expanded `boundingRect()`.

* **Multi-selection performance (`source/_nodes.py`):**

  * Simplified `FocusNode.itemChange(ItemSelectedHasChanged)` and `EventNode.itemChange(ItemSelectedHasChanged)` to `self.update()`, removing per-node `prepareGeometryChange()`, `update_connections()`, and full-scene `invalidate()` calls during box selection.

## UX & UI Improvements

* **Menu bar & toolbar layout (`source/_focusGUI.py`):**

  * Moved `File`, `Edit`, `Tools`, `View`, `Library`, `Help`, and `Settings` onto the topmost `QMenuBar`.

  * Made `Tree ID` compact (`64px` minimum) and auto-expanding up to `240px` as you type, with `14px`/`10px` spacers separating `Tree ID`, `Country`, and `Show Library`.

  * Replaced the plain mute toggle with a drawn speaker `QIcon` and subtle dark-theme amber/slate styling (`Muted` / `Mute App`) using identical border/padding in both states so toggling never shifts toolbar height.

  * Hidden the **State Viewport** toolbar button by default and added **View → Show State Viewport on Toolbar** to toggle it.

* **Non-blocking feedback & exit safety (`source/_focusGUI.py`):**

  * Replaced routine `QMessageBox.information` popups (save, load, delete-empty, copy, paste, colorize, arrange) with `statusBar().showMessage(...)`.

  * Added an **Unsaved Changes** (`Save` / `Discard` / `Cancel`) prompt in `closeEvent()`.

* **Note & focus editing ergonomics (`source/_nodes.py`, `source/_dialog.py`):**

  * Added `Alt`+drag support anywhere on a `NoteNode` (including over its child text items), pushing a `MoveNoteCommand` onto `undo_stack` on release.

  * Added searchable **Pick…** focus-selector dialogs next to **Prerequisites** and **Mutually Exclusive** in `FocusEditDialog`.

* **Starter Tutorial (`source/_focusGUI.py`):**

  * Added a 5-step **Starter Tutorial** dialog (`show_starter_tutorial`) covering project basics, focus creation/editing, connections, notes/generator, and HOI4 export, shown on startup when `show_tutorial_on_startup` is enabled and accessible anytime via **Help → Starter Tutorial…**.

## Random Focus Tree Generator

* **Opt-in base-game effects & state modifiers (`source/_focusGenerator.py`, `source/_focusGUI.py`):**

  * Added **Randomized Effects & Modifiers (Optional)** controls to the **Generate Project** dialog (`enable_effects`, `enable_modifiers`, `enable_available`, `effects_per_focus` min/max, and `Political`/`Economy`/`Military`/`Research` category toggles), all disabled by default.

  * Implemented `_build_random_reward()` and `_build_random_available()` using generic base-game HOI4 effects and inline `add_state_modifier` blocks, ensuring each focus receives a unique `completion_reward` when enabled without generating events, ideas, characters, or countries.

## Hardening & Code Quality

* **Removed global `QMessageBox` monkeypatching (`source/_focusGUI.py`):**

  * Removed `QMessageBox.information`/`warning`/`critical`/`question` class overrides and replaced them with explicit `show_message_box()` / `_silent_dialog()` helpers.

* **Canonical model & state sync (`source/_focusGUI.py`, `source/_nodes.py`, `source/_commands.py`):**

  * Removed the stray `load_settings()` / `load_database()` calls from `HOI4FocusTreeGenerator.contextMenuEvent()`.

  * Updated `FocusNode.itemChange(ItemPositionHasChanged)` to keep `focus.x`/`focus.y` continuously synced with scene coordinates.

  * Updated `EditFocusCommand` to keep `canvas.nodes` keys and cross-focus `prerequisites`/`mutually_exclusive` references synced on focus ID renames, and updated `sync_focus_positions()` to reconcile `parent.focuses` with `canvas.nodes`.

* **Complete Undo/Redo field coverage (`source/_commands.py`):**

  * Added `_copy_focus_fields()` using `dataclasses.fields(Focus)` with deep-copied containers, used in `DeleteFocusCommand.undo()` and `EditFocusCommand.redo()`/`undo()`.

* **Atomic persistence, dirty tracking & crash recovery (`source/_focusGUI.py`):**

  * Converted `save_settings()` and `save_database()` to atomic temp-file + `os.fsync()` + `os.replace()` writes.

  * Added `_project_dirty` tracking for non-undo edits (`Tree ID`, `Country`, and event changes), cleared on new/load/save and checked in `closeEvent()`.

  * Added a re-entrancy-guarded emergency `.crash_recovery.json` autosave inside `_log_uncaught` (`sys.excepthook`).

  * Surfaced per-focus serialization errors in `_build_project_payload()` instead of silently dropping focuses, and used defensive `getattr()` for `default_focus_color` in `to_settings()`.

* **`silent_operation` cleanup (`source/`):**

  * Unwrapped 713 redundant `with silent_operation(...)` blocks across `_nodes.py`, `_commands.py`, `_dialog.py`, `_focusGUI.py`, `_focusGenerator.py`, `_dataStructs.py`, and `_utils.py` so real exceptions surface cleanly and hot rendering paths avoid context-manager overhead.