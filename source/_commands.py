"""Undo/Redo command classes extracted from _focusGUI for separation of concerns.

This module centralizes QUndoCommand subclasses used by the GUI canvas and
main window. It imports shared utilities directly from _utils to avoid circular dependencies.

ERROR HANDLING POLICY:
    QUndoCommand operations use POLICY_SILENT for most operations because:
    1. Undo/redo operations should not disrupt the UI
    2. Partial failures in undo/redo are expected and acceptable
    3. Logging at debug level is sufficient for diagnostics
    
    All error handling is delegated to the centralized error_handler module.
"""
from __future__ import annotations

from _imports import (
    Any, List, Optional, Tuple,
    QPointF, QRectF, QColor, QUndoCommand,
    Event, Focus, clone_focus_pure,
)
from typing import TYPE_CHECKING
from error_handler import (
    handle_exception, silent_operation, safe_operation, catch_and_handle,
    POLICY_SILENT, POLICY_LOG_DEBUG, RecoverableError, ErrorPolicy, PolicyConfig
)

if TYPE_CHECKING:
    from _focusGUI import FocusTreeCanvas, FocusNode, NoteNode, LShapedConnectionLine, EventNode


# Constants
GRID_UNIT = 300.0

# ============================================================================
# region Focus Node Commands
# ============================================================================

class AddFocusCommand(QUndoCommand):
    """Undoable command to add a focus and its node to the canvas."""
    def __init__(self, main_window, focus: Focus, description: str = "Add Focus"):
        super().__init__(description)
        self.main = main_window
        self.canvas = getattr(main_window, 'canvas', None)
        self.focus = focus
        self.node = None

    def redo(self):
        if self.canvas is None:
            return
        # if focus already present, don't duplicate
        if any(f.id == self.focus.id for f in getattr(self.main, 'focuses', [])):
            return
        with silent_operation("AddFocusCommand.redo", log_level="debug"):
            if not getattr(self.focus, '_copied', False):
                with silent_operation("Clear mutually_exclusive"):
                    self.focus.mutually_exclusive = []
                with silent_operation("Update other focuses mutually_exclusive"):
                    for of in getattr(self.main, 'focuses', []):
                        with silent_operation("Check mutual exclusivity"):
                            if getattr(self.focus, 'id', None) in getattr(of, 'mutually_exclusive', []) or False:
                                of.mutually_exclusive = [m for m in getattr(of, 'mutually_exclusive', []) if m != getattr(self.focus, 'id', None)]

            self.main.focuses.append(self.focus)
            self.node = self.canvas.add_focus_node(self.focus)
            with silent_operation("Update status"):
                self.main.update_status()

    def undo(self):
        if self.canvas is None or self.node is None:
            with silent_operation("Remove focus from list"):
                self.main.focuses = [f for f in getattr(self.main, 'focuses', []) if f.id != self.focus.id]
            return
        with silent_operation("AddFocusCommand.undo", log_level="debug"):
            with silent_operation("Remove node from canvas"):
                if self.node.focus.id in self.canvas.nodes:
                    self.canvas.remove_node(self.node)
            with silent_operation("Remove focus from list"):
                self.main.focuses = [f for f in getattr(self.main, 'focuses', []) if f.id != self.focus.id]
            with silent_operation("Update status"):
                self.main.update_status()


class DeleteFocusCommand(QUndoCommand):
    """Undoable command to delete a focus node and remember its state for restore."""
    def __init__(self, main_window, node: 'FocusNode', description: str = "Delete Focus"):
        desc = description
        with silent_operation("Build delete description"):
            if desc is None or desc == "Delete Focus":
                f = getattr(node, 'focus', None)
                fid = getattr(f, 'id', None) if f is not None else None
                fname = getattr(f, 'name', None) if f is not None else None
                if fid is not None and fname:
                    desc = f"Delete Focus {fid} ({fname})"
                elif fid is not None:
                    desc = f"Delete Focus {fid}"
                else:
                    desc = "Delete Focus"
        super().__init__(desc)
        self.main = main_window
        self.canvas = getattr(main_window, 'canvas', None)
        self.node = node
        self.focus_snapshot = None
        self.connections_snapshot = []

    def redo(self):
        if self.canvas is None or self.node is None:
            return
        with silent_operation("Clone focus for snapshot"):
            self.focus_snapshot = clone_focus_pure(self.node.focus)
        if self.focus_snapshot is None:
            with silent_operation("Fallback focus snapshot"):
                f = self.node.focus
                self.focus_snapshot = Focus(id=f.id, name=f.name, x=f.x, y=f.y)
        
        with silent_operation("Capture connections snapshot"):
            for conn in list(getattr(self.canvas, 'connections', [])):
                with silent_operation("Check connection"):
                    if hasattr(conn, 'start_node') and hasattr(conn, 'end_node') and getattr(conn.start_node, 'focus', None) and getattr(conn.end_node, 'focus', None):
                        sid = conn.start_node.focus.id
                        eid = conn.end_node.focus.id
                        if getattr(self.node, 'focus', None) is not None and (sid == self.node.focus.id or eid == self.node.focus.id):
                            self.connections_snapshot.append((sid, eid))
        
        with silent_operation("DeleteFocusCommand.redo", log_level="debug"):
            focus_id = None
            with silent_operation("Get focus_id"):
                focus_id = getattr(self.node, 'focus', None).id if getattr(self.node, 'focus', None) is not None else None
            
            if focus_id is not None:
                with silent_operation("Remove focus from list"):
                    self.main.focuses = [f for f in getattr(self.main, 'focuses', []) if f.id != focus_id]
                
                with silent_operation("Update prerequisites and mutually_exclusive"):
                    for focus in list(getattr(self.main, 'focuses', [])):
                        with silent_operation("Update single focus refs"):
                            if focus_id in getattr(focus, 'prerequisites', []):
                                focus.prerequisites.remove(focus_id)
                            if focus_id in getattr(focus, 'mutually_exclusive', []):
                                focus.mutually_exclusive.remove(focus_id)
                
                with silent_operation("Remove node from canvas"):
                    node_in_canvas = self.canvas.nodes.get(focus_id)
                    if node_in_canvas is not None:
                        self.canvas.remove_node(node_in_canvas)
                
                with silent_operation("Show status message"):
                    self.main.statusBar().showMessage(f"Deleted focus: {focus_id}")
                
                with silent_operation("Update status"):
                    self.main.update_status()

    def undo(self):
        if self.canvas is None or self.focus_snapshot is None:
            return
        with silent_operation("DeleteFocusCommand.undo", log_level="debug"):
            restored = clone_focus_pure(self.focus_snapshot)
            existing_ids = [f.id for f in getattr(self.main, 'focuses', [])]
            node = None
            if restored.id in existing_ids:
                for f in getattr(self.main, 'focuses', []):
                    if f.id == restored.id:
                        with silent_operation("Restore focus attributes"):
                            f.name = restored.name
                            f.x = restored.x
                            f.y = restored.y
                            f.cost = restored.cost
                            f.description = restored.description
                            f.prerequisites = list(restored.prerequisites)
                            f.mutually_exclusive = list(restored.mutually_exclusive)
                            f.available = restored.available
                            f.bypass = restored.bypass
                            f.completion_reward = restored.completion_reward
                            f.ai_will_do = restored.ai_will_do
                            f.allow_branch = restored.allow_branch
                            f.network_id = restored.network_id
                            f.icon = restored.icon
                        break
                with silent_operation("Get existing node"):
                    node = self.canvas.nodes.get(restored.id)
                if node is not None:
                    with silent_operation("Update node"):
                        node.focus = next((f for f in getattr(self.main, 'focuses', []) if f.id == restored.id), node.focus)
                        node.update()
            else:
                with silent_operation("Append restored focus"):
                    self.main.focuses.append(restored)
                with silent_operation("Add focus node"):
                    node = self.canvas.add_focus_node(restored)

            for a, b in list(self.connections_snapshot):
                with silent_operation(f"Restore connection {a}->{b}"):
                    ids = [f.id for f in getattr(self.main, 'focuses', [])]
                    if a not in ids or b not in ids:
                        continue
                    with silent_operation("Add prerequisite"):
                        to_node = self.canvas.nodes.get(b)
                        if to_node and a not in to_node.focus.prerequisites:
                            to_node.focus.prerequisites.append(a)
                    with silent_operation("Create connection"):
                        self.canvas.create_connection(a, b)
            
            with silent_operation("Update status"):
                self.main.update_status()

# endregion

# ============================================================================
# region Focus Connections & Relationships
# ============================================================================

class CreateConnectionCommand(QUndoCommand):
    """Undoable create connection between two focus ids."""
    def __init__(self, canvas: 'FocusTreeCanvas', from_id: str, to_id: str, description: str = "Create Connection"):
        super().__init__(description)
        self.canvas = canvas
        self.from_id = from_id
        self.to_id = to_id
        self.line = None

    def redo(self):
        if self.canvas is None:
            return
        # Avoid duplicate connections
        with silent_operation("Check existing connection"):
            exists = any((hasattr(conn, 'start_node') and getattr(conn.start_node, 'focus', None) is not None and
                         conn.start_node.focus.id == self.from_id and hasattr(conn, 'end_node') and
                         conn.end_node.focus.id == self.to_id) for conn in self.canvas.connections)
            if exists:
                return

        with silent_operation("CreateConnectionCommand.redo", log_level="debug"):
            to_node = self.canvas.nodes.get(self.to_id)
            mode = getattr(self.canvas, 'prereq_link_mode', None)

            if to_node and isinstance(self.canvas.nodes.get(self.from_id), type(self.canvas.nodes.get(self.to_id))):
                # Focus-to-focus linking with OR/AND support
                if mode in ('OR', 'AND'):
                    groups = []
                    with silent_operation("Get prerequisites_groups"):
                        groups = list(getattr(to_node.focus, 'prerequisites_groups', []) or [])
                    added = False

                    # Try to find an existing group of same type
                    for g in groups:
                        with silent_operation("Check group type"):
                            gtype = (g.get('type') or 'AND').upper() if isinstance(g, dict) else 'AND'
                            if gtype == mode:
                                items = list(g.get('items', []) or [])
                                if self.from_id not in items:
                                    items.append(self.from_id)
                                    g['items'] = items
                                    added = True
                                    break

                    if not added:
                        with silent_operation("Add new group"):
                            groups.append({'type': mode, 'items': [self.from_id]})

                    with silent_operation("Set prerequisites_groups"):
                        to_node.focus.prerequisites_groups = groups

                    self.line = self.canvas.create_connection(self.from_id, self.to_id, prereq_kind=mode)
                else:
                    # Legacy single prerequisite handling
                    with silent_operation("Add legacy prerequisite"):
                        if to_node and self.from_id not in to_node.focus.prerequisites:
                            to_node.focus.prerequisites.append(self.from_id)
                    self.line = self.canvas.create_connection(self.from_id, self.to_id, prereq_kind=None)
            else:
                # Generic cross-type link
                with silent_operation("Add generic prerequisite"):
                    if to_node and self.from_id not in to_node.focus.prerequisites:
                        to_node.focus.prerequisites.append(self.from_id)
                self.line = self.canvas.create_connection(self.from_id, self.to_id,
                                                          prereq_kind=mode if mode in ('OR', 'AND') else None)

    def undo(self):
        if self.canvas is None:
            return
        with silent_operation("CreateConnectionCommand.undo", log_level="debug"):
            to_node = self.canvas.nodes.get(self.to_id)
            mode = getattr(self.canvas, 'prereq_link_mode', None)

            if to_node:
                # Remove from prerequisites_groups if present
                if getattr(to_node.focus, 'prerequisites_groups', None):
                    with silent_operation("Remove from prerequisites_groups"):
                        groups = list(getattr(to_node.focus, 'prerequisites_groups', []) or [])
                        changed = False

                        for g in list(groups):
                            with silent_operation("Update group items"):
                                items = list(g.get('items', []) or [])
                                if self.from_id in items:
                                    items = [it for it in items if it != self.from_id]
                                    if items:
                                        g['items'] = items
                                    else:
                                        groups.remove(g)
                                    changed = True

                        if changed:
                            with silent_operation("Set updated prerequisites_groups"):
                                to_node.focus.prerequisites_groups = groups

                # Remove legacy single prereq if present
                with silent_operation("Remove legacy prerequisite"):
                    if self.from_id in getattr(to_node.focus, 'prerequisites', []):
                        to_node.focus.prerequisites.remove(self.from_id)

            if self.line is not None:
                with silent_operation("Remove connection line"):
                    self.canvas.remove_connection(self.line)
            else:
                # Try to find and remove matching connection
                for conn in list(self.canvas.connections):
                    with silent_operation("Find and remove connection"):
                        if (hasattr(conn, 'start_node') and hasattr(conn, 'end_node') and
                            conn.start_node.focus.id == self.from_id and conn.end_node.focus.id == self.to_id):
                            self.canvas.remove_connection(conn)
                            break


class RemoveConnectionCommand(QUndoCommand):
    """Undoable removal of a connection."""
    def __init__(self, canvas: 'FocusTreeCanvas', connection: 'LShapedConnectionLine',
                 description: str = "Remove Connection"):
        super().__init__(description)
        self.canvas = canvas
        self.connection = connection
        self.from_id = None
        self.to_id = None

    def redo(self):
        if self.canvas is None or self.connection is None:
            return
        with silent_operation("Capture connection IDs"):
            if hasattr(self.connection, 'start_node') and hasattr(self.connection, 'end_node'):
                self.from_id = self.connection.start_node.focus.id
                self.to_id = self.connection.end_node.focus.id

        with silent_operation("Remove connection"):
            self.canvas.remove_connection(self.connection)

    def undo(self):
        if self.canvas is None or self.from_id is None or self.to_id is None:
            return
        with silent_operation("RemoveConnectionCommand.undo", log_level="debug"):
            to_node = self.canvas.nodes.get(self.to_id)
            if to_node and self.from_id not in to_node.focus.prerequisites:
                to_node.focus.prerequisites.append(self.from_id)
            self.canvas.create_connection(self.from_id, self.to_id)


class MakeMutexCommand(QUndoCommand):
    """Undoable command to make two focuses mutually exclusive (symmetric)."""
    def __init__(self, main_window, a_id: str, b_id: str,
                 description: str = "Make Mutually Exclusive"):
        super().__init__(description)
        self.main = main_window
        self.canvas = getattr(main_window, 'canvas', None)
        self.a_id = a_id
        self.b_id = b_id
        self.before_a = None
        self.before_b = None

    def redo(self):
        with silent_operation("MakeMutexCommand.redo", log_level="debug"):
            fa = None
            fb = None

            # Locate focus objects
            for f in getattr(self.main, 'focuses', []) or []:
                with silent_operation("Find focus objects"):
                    if getattr(f, 'id', None) == self.a_id:
                        fa = f
                    if getattr(f, 'id', None) == self.b_id:
                        fb = f
                    if fa and fb:
                        break

            # Record before state on first execution
            if self.before_a is None:
                with silent_operation("Capture before_a"):
                    self.before_a = list(getattr(fa, 'mutually_exclusive', []) or []) if fa is not None else None
            if self.before_b is None:
                with silent_operation("Capture before_b"):
                    self.before_b = list(getattr(fb, 'mutually_exclusive', []) or []) if fb is not None else None

            if fa is None or fb is None:
                return

            # Ensure lists exist
            if getattr(fa, 'mutually_exclusive', None) is None:
                fa.mutually_exclusive = []
            if getattr(fb, 'mutually_exclusive', None) is None:
                fb.mutually_exclusive = []

            # Add symmetric relationship
            if self.b_id not in fa.mutually_exclusive:
                fa.mutually_exclusive.append(self.b_id)
            if self.a_id not in fb.mutually_exclusive:
                fb.mutually_exclusive.append(self.a_id)

            # Keep reciprocity in sync
            with silent_operation("Sync mutual exclusive"):
                if hasattr(self.main, '_sync_mutual_exclusive'):
                    with silent_operation("Sync a_id"):
                        self.main._sync_mutual_exclusive(self.a_id)
                    with silent_operation("Sync b_id"):
                        self.main._sync_mutual_exclusive(self.b_id)

            with silent_operation("Refresh mutex connectors"):
                if self.canvas is not None and hasattr(self.canvas, 'refresh_mutex_connectors'):
                    self.canvas.refresh_mutex_connectors()

    def undo(self):
        with silent_operation("MakeMutexCommand.undo", log_level="debug"):
            fa = None
            fb = None

            # Locate focus objects
            for f in getattr(self.main, 'focuses', []) or []:
                with silent_operation("Find focus objects"):
                    if getattr(f, 'id', None) == self.a_id:
                        fa = f
                    if getattr(f, 'id', None) == self.b_id:
                        fb = f
                    if fa and fb:
                        break

            # Restore snapshots
            if fa is not None and self.before_a is not None:
                with silent_operation("Restore before_a"):
                    fa.mutually_exclusive = list(self.before_a)
            if fb is not None and self.before_b is not None:
                with silent_operation("Restore before_b"):
                    fb.mutually_exclusive = list(self.before_b)

            with silent_operation("Refresh mutex connectors"):
                if self.canvas is not None and hasattr(self.canvas, 'refresh_mutex_connectors'):
                    self.canvas.refresh_mutex_connectors()

# endregion

# ============================================================================
# region Focus Property Editing
# ============================================================================

class EditFocusCommand(QUndoCommand):
    """Capture focus property edits so they can be undone/redone."""
    def __init__(self, main_window, focus_before: Focus, focus_after: Focus,
                 description: str = "Edit Focus"):
        super().__init__(description)
        self.main = main_window
        self.before = clone_focus_pure(focus_before)
        self.after = clone_focus_pure(focus_after)

    def redo(self):
        with silent_operation("EditFocusCommand.redo", log_level="debug"):
            # Find focus in main window and update fields
            for f in getattr(self.main, 'focuses', []):
                if f.id == self.before.id:
                    # If ID changed, swap by ID
                    if self.before.id != self.after.id:
                        f.id = self.after.id

                    f.name = self.after.name
                    f.cost = self.after.cost
                    f.description = self.after.description
                    f.prerequisites = list(self.after.prerequisites)
                    f.mutually_exclusive = list(self.after.mutually_exclusive)
                    f.available = self.after.available
                    f.bypass = self.after.bypass
                    f.completion_reward = self.after.completion_reward
                    f.ai_will_do = self.after.ai_will_do
                    f.icon = getattr(self.after, 'icon', None)
                    break

            with silent_operation("Sync mutual exclusive"):
                self.main._sync_mutual_exclusive(f.id)
            with silent_operation("Refresh mutex connectors"):
                self.main.canvas.refresh_mutex_connectors()

    def undo(self):
        with silent_operation("EditFocusCommand.undo", log_level="debug"):
            # Find focus in main window and restore fields
            for f in getattr(self.main, 'focuses', []):
                if f.id == self.after.id:
                    # If ID changed, swap back by ID
                    if self.before.id != self.after.id:
                        f.id = self.before.id

                    f.name = self.before.name
                    f.cost = self.before.cost
                    f.description = self.before.description
                    f.prerequisites = list(self.before.prerequisites)
                    f.mutually_exclusive = list(self.before.mutually_exclusive)
                    f.available = self.before.available
                    f.bypass = self.before.bypass
                    f.completion_reward = self.before.completion_reward
                    f.ai_will_do = self.before.ai_will_do
                    f.icon = getattr(self.before, 'icon', None)
                    break

            with silent_operation("Sync mutual exclusive"):
                self.main._sync_mutual_exclusive(f.id)
            with silent_operation("Refresh mutex connectors"):
                self.main.canvas.refresh_mutex_connectors()


class SetIconCommand(QUndoCommand):
    """Set or remove an icon for a focus with undo support."""
    def __init__(self, node: 'FocusNode', new_icon: Optional[str],
                 description: str = "Set Icon"):
        super().__init__(description)
        self.node = node
        self.new_icon = new_icon
        self.old_icon = getattr(node.focus, 'icon', None)

    def redo(self):
        with silent_operation("SetIconCommand.redo", log_level="debug"):
            self.node.focus.icon = self.new_icon
            # Clear cached pixmap if any
            with silent_operation("Clear cached pixmap"):
                if hasattr(self.node.focus, '_cached_icon_pixmap'):
                    with silent_operation("Delete cached pixmap attr"):
                        delattr(self.node.focus, '_cached_icon_pixmap')
            self.node.update()

    def undo(self):
        with silent_operation("SetIconCommand.undo", log_level="debug"):
            self.node.focus.icon = self.old_icon
            with silent_operation("Clear cached pixmap"):
                if hasattr(self.node.focus, '_cached_icon_pixmap'):
                    with silent_operation("Delete cached pixmap attr"):
                        delattr(self.node.focus, '_cached_icon_pixmap')
            self.node.update()

# endregion

# ============================================================================
# region Focus Node Visual Manipulation
# ============================================================================

class MoveNodeCommand(QUndoCommand):
    """Undoable movement of a FocusNode from one scene position to another."""
    def __init__(self, node: 'FocusNode', start_pos: QPointF, end_pos: QPointF,
                 description: str = "Move Node"):
        super().__init__(description)
        self.node = node
        self.start = QPointF(start_pos)
        self.end = QPointF(end_pos)

    def redo(self):
        with silent_operation("MoveNodeCommand.redo", log_level="debug"):
            self.node.setPos(self.end)
            # Update model coordinates
            gx = round(self.end.x() / GRID_UNIT)
            gy = round(self.end.y() / GRID_UNIT)
            self.node.focus.x = gx
            self.node.focus.y = gy
            self.node.update_connections()

    def undo(self):
        with silent_operation("MoveNodeCommand.undo", log_level="debug"):
            self.node.setPos(self.start)
            # Update model coordinates
            gx = round(self.start.x() / GRID_UNIT)
            gy = round(self.start.y() / GRID_UNIT)
            self.node.focus.x = gx
            self.node.focus.y = gy
            self.node.update_connections()


class ColorizeNodesCommand(QUndoCommand):
    """Apply a color to a list of nodes, optionally to their connections."""
    def __init__(self, main_window, node_ids: List[str], color: QColor,
                 apply_to_connections: bool):
        super().__init__(f"Colorize {len(node_ids)} nodes")
        self.main = main_window
        self.node_ids = list(node_ids)
        self.color = QColor(color)
        self.apply_to_connections = bool(apply_to_connections)
        self.prev_overrides = {}
        self.prev_conn_colors = []

    def redo(self):
        with silent_operation("ColorizeNodesCommand.redo", log_level="debug"):
            for nid in self.node_ids:
                self.prev_overrides[nid] = self.main.canvas.focus_color_overrides.get(nid)
                self.main.canvas.focus_color_overrides[nid] = QColor(self.color)

            if self.apply_to_connections:
                for conn in list(self.main.canvas.connections):
                    with silent_operation("Apply connection color"):
                        if (conn.start_node.focus.id in self.node_ids or
                            conn.end_node.focus.id in self.node_ids):
                            self.prev_conn_colors.append((conn, conn.pen().color()))
                            conn.set_color(self.color)

            with silent_operation("Refresh connection colors"):
                self.main.canvas.refresh_connection_colors()

            for nid in self.node_ids:
                with silent_operation("Update node display"):
                    # Try focus node first, then event node
                    node = self.main.canvas.nodes.get(nid)
                    if node:
                        node.update()
                    else:
                        ev = getattr(self.main.canvas, 'event_nodes', {}).get(nid)
                        if ev:
                            ev.update()

    def undo(self):
        with silent_operation("ColorizeNodesCommand.undo", log_level="debug"):
            for nid, prev in self.prev_overrides.items():
                if prev is None:
                    with silent_operation("Remove color override"):
                        if nid in self.main.canvas.focus_color_overrides:
                            del self.main.canvas.focus_color_overrides[nid]
                else:
                    self.main.canvas.focus_color_overrides[nid] = QColor(prev)

            for conn, col in self.prev_conn_colors:
                with silent_operation("Restore connection color"):
                    conn.set_color(col)

            with silent_operation("Refresh connection colors"):
                self.main.canvas.refresh_connection_colors()

            for nid in self.node_ids:
                with silent_operation("Update node display"):
                    node = self.main.canvas.nodes.get(nid)
                    if node:
                        node.update()

# endregion

# ============================================================================
# region Event Commands
# ============================================================================

class DeleteEventCommand(QUndoCommand):
    """Undoable command to delete an EventNode and remember its state for restore."""
    def __init__(self, main_window, node: 'EventNode', description: str = "Delete Event"):
        super().__init__(description)
        self.main = main_window
        self.canvas = getattr(main_window, 'canvas', None)
        self.node = node
        self.event_snapshot = None
        self.note_links_snapshot = []
        self.event_links_snapshot = []

    def redo(self):
        if self.canvas is None or self.node is None:
            return

        with silent_operation("DeleteEventCommand.redo", log_level="debug"):
            ev = getattr(self.node, 'event', None)
            if ev is None:
                return

            # Snapshot basic event fields
            with silent_operation("Snapshot event fields"):
                self.event_snapshot = dict(
                    id=ev.id, title=ev.title, description=ev.description, x=ev.x, y=ev.y,
                    free_x=getattr(ev, 'free_x', None), free_y=getattr(ev, 'free_y', None),
                    trigger=getattr(ev, 'trigger', ''),
                    options_block=getattr(ev, 'options_block', '')
                )

            # Capture note->event links referencing this node
            with silent_operation("Capture note->event links"):
                for ne in list(getattr(self.canvas, '_note_event_links', [])):
                    with silent_operation("Check note link"):
                        if getattr(ne, 'event_node', None) is self.node:
                            self.note_links_snapshot.append(getattr(getattr(ne, 'note', None), 'note_id', None))

            # Capture event->event links
            with silent_operation("Capture event->event links"):
                for ee in list(getattr(self.canvas, '_event_event_links', [])):
                    with silent_operation("Check event link"):
                        if getattr(ee, 'a', None) is self.node or getattr(ee, 'b', None) is self.node:
                            a_id = getattr(getattr(ee, 'a', None), 'event', None)
                            b_id = getattr(getattr(ee, 'b', None), 'event', None)
                            ai = getattr(a_id, 'id', None) if a_id is not None else None
                            bi = getattr(b_id, 'id', None) if b_id is not None else None
                            if ai and bi:
                                self.event_links_snapshot.append((ai, bi))

            # Remove from main.events
            with silent_operation("Remove from main.events"):
                eid = getattr(ev, 'id', None)
                self.main.events = [e for e in getattr(self.main, 'events', [])
                                    if getattr(e, 'id', None) != eid]

            # Remove links and node from canvas
            with silent_operation("Remove links for node"):
                if hasattr(self.canvas, 'remove_links_for'):
                    self.canvas.remove_links_for(self.node)
                elif hasattr(self.canvas, 'remove_note_event_links_for'):
                    self.canvas.remove_note_event_links_for(self.node)

            with silent_operation("Remove from event_nodes dict"):
                if getattr(self.canvas, 'event_nodes', None) and getattr(ev, 'id', None) in self.canvas.event_nodes:
                    del self.canvas.event_nodes[ev.id]

            with silent_operation("Remove item from scene"):
                self.canvas.removeItem(self.node)

    def undo(self):
        if self.canvas is None or self.event_snapshot is None:
            return

        with silent_operation("DeleteEventCommand.undo", log_level="debug"):
            data = self.event_snapshot
            ev = Event(id=data.get('id'))
            ev.title = data.get('title', '')
            ev.description = data.get('description', '')
            ev.x = int(data.get('x') or 0)
            ev.y = int(data.get('y') or 0)
            ev.free_x = data.get('free_x')
            ev.free_y = data.get('free_y')
            ev.trigger = data.get('trigger', '')
            ev.options_block = data.get('options_block', '')

            # Restore to main.events
            with silent_operation("Restore to main.events"):
                self.main.events.append(ev)

            # Recreate node and mapping
            node = None
            with silent_operation("Recreate event node"):
                node = self.canvas.add_event_node(ev)

            with silent_operation("Update event_nodes mapping"):
                if node is not None and getattr(ev, 'id', None):
                    self.canvas.event_nodes[ev.id] = node

            # Restore note links
            with silent_operation("Restore note links"):
                for nid in list(self.note_links_snapshot or []):
                    with silent_operation("Restore note link"):
                        note = self.canvas._notes_by_id.get(nid)
                        if note is not None and node is not None:
                            self.canvas.add_note_event_link(note, node)

            # Restore event->event links that referenced this node
            with silent_operation("Restore event->event links"):
                for a_id, b_id in list(self.event_links_snapshot or []):
                    with silent_operation("Restore event link"):
                        try:
                            a_node = self.canvas.event_nodes.get(a_id)
                            b_node = self.canvas.event_nodes.get(b_id)
                            if a_node is not None and b_node is not None:
                                self.canvas.add_event_event_link(a_node, b_node)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

# endregion

# ============================================================================
# region Note Commands
# ============================================================================

class CreateNoteCommand(QUndoCommand):
    """Create a NoteNode on the canvas."""
    def __init__(self, main_window, text: str, pos: QPointF,
                 description: str = "Create Note"):
        super().__init__(description)
        self.main = main_window
        self.canvas = getattr(main_window, 'canvas', None)
        self.text = text
        self.pos = pos
        self.note = None

    def redo(self):
        if self.canvas is None:
            return
        with silent_operation("CreateNoteCommand.redo", log_level="debug"):
            self.note = self.canvas._create_note_item(self.text, self.pos)
            with silent_operation("Add note to items"):
                self.canvas._notes_items.append(self.note)
            with silent_operation("Register note by ID"):
                self.canvas._notes_by_id[self.note.note_id] = self.note
            self.note.set_visible(getattr(self.canvas, 'notes_enabled', True))

    def undo(self):
        if self.canvas is None or self.note is None:
            return
        with silent_operation("CreateNoteCommand.undo", log_level="debug"):
            with silent_operation("Remove note connections"):
                if hasattr(self.canvas, 'remove_note_connections_for'):
                    self.canvas.remove_note_connections_for(self.note)

            with silent_operation("Remove note focus links"):
                if hasattr(self.canvas, 'remove_note_focus_links_for'):
                    self.canvas.remove_note_focus_links_for(self.note)

            with silent_operation("Remove note from scene"):
                item_scene = None
                with silent_operation("Get item scene"):
                    item_scene = getattr(self.note, 'scene')() if callable(getattr(self.note, 'scene', None)) else None

                removed = False
                if item_scene is not None:
                    with silent_operation("Remove via item scene"):
                        if hasattr(item_scene, 'safe_remove_item'):
                            item_scene.safe_remove_item(self.note)
                        else:
                            item_scene.removeItem(self.note)
                        removed = True

                if not removed:
                    sc = self.canvas
                    with silent_operation("Remove via canvas"):
                        if hasattr(sc, 'safe_remove_item'):
                            sc.safe_remove_item(self.note)
                        else:
                            sc.removeItem(self.note)

                with silent_operation("Remove from notes_items"):
                    if getattr(self.canvas, '_notes_items', None) and self.note in self.canvas._notes_items:
                        self.canvas._notes_items.remove(self.note)

                with silent_operation("Remove from notes_by_id"):
                    if getattr(self.canvas, '_notes_by_id', None) and getattr(self.note, 'note_id', None) in self.canvas._notes_by_id:
                        del self.canvas._notes_by_id[self.note.note_id]


class DeleteNoteCommand(QUndoCommand):
    """Delete a note and remember it for undo."""
    def __init__(self, main_window, note: 'NoteNode',
                 description: str = "Delete Note"):
        super().__init__(description)
        self.main = main_window
        self.canvas = getattr(main_window, 'canvas', None)
        self.note = note
        self.snapshot = None
        self.connections_snapshot = []
        self.focus_links_snapshot = []

    def redo(self):
        if self.canvas is None or self.note is None:
            return

        with silent_operation("DeleteNoteCommand.redo", log_level="debug"):
            with silent_operation("Create note snapshot"):
                self.snapshot = self.note.to_dict()

            with silent_operation("Capture note connections"):
                self.connections_snapshot = []
                for ln in list(getattr(self.canvas, '_note_connections', [])):
                    with silent_operation("Capture connection"):
                        if ln.a is self.note or ln.b is self.note:
                            self.connections_snapshot.append({
                                'a': getattr(ln.a, 'note_id', ''),
                                'b': getattr(ln.b, 'note_id', ''),
                                'label': getattr(ln, 'label', '') or '',
                                'manual_offset': float(getattr(ln, 'manual_offset', 0.0) or 0.0),
                                'manual_angle': None if getattr(ln, 'manual_angle', None) is None else float(getattr(ln, 'manual_angle', None)),
                            })

            with silent_operation("Capture focus links"):
                self.focus_links_snapshot = []
                for nf in list(getattr(self.canvas, '_note_focus_links', [])):
                    with silent_operation("Capture focus link"):
                        if getattr(nf, 'note', None) is self.note:
                            fid = None
                            with silent_operation("Get focus ID"):
                                f_obj = getattr(nf, 'focus_node', None)
                                f_focus = getattr(f_obj, 'focus', None)
                                fid = getattr(f_focus, 'id', None)
                            if fid:
                                self.focus_links_snapshot.append(fid)

            with silent_operation("Remove note connections"):
                if hasattr(self.canvas, 'remove_note_connections_for'):
                    self.canvas.remove_note_connections_for(self.note)

            with silent_operation("Remove note focus links"):
                if hasattr(self.canvas, 'remove_note_focus_links_for'):
                    self.canvas.remove_note_focus_links_for(self.note)

            sc = self.canvas
            with silent_operation("Remove note from scene"):
                sc.removeItem(self.note)

            with silent_operation("Remove from notes_items"):
                if getattr(sc, '_notes_items', None) and self.note in sc._notes_items:
                    sc._notes_items.remove(self.note)

            with silent_operation("Remove from notes_by_id"):
                if getattr(sc, '_notes_by_id', None) and getattr(self.note, 'note_id', None) in sc._notes_by_id:
                    del sc._notes_by_id[self.note.note_id]

    def undo(self):
        if self.canvas is None or self.snapshot is None:
            return

        with silent_operation("DeleteNoteCommand.undo", log_level="debug"):
            # Attempt to lazily import NoteNode implementation from the GUI module
            NoteNode = None
            with silent_operation("Import NoteNode"):
                import _focusGUI as _fg
                NoteNode = getattr(_fg, 'NoteNode', None)

            if NoteNode is None:
                return

            note = NoteNode.from_dict(self.snapshot)
            self.canvas.addItem(note)

            with silent_operation("Set note visibility"):
                note.set_visible(getattr(self.canvas, 'notes_enabled', True))

            with silent_operation("Add to notes_items"):
                if getattr(self.canvas, '_notes_items', None) is not None:
                    self.canvas._notes_items.append(note)

            with silent_operation("Add to notes_by_id"):
                if getattr(self.canvas, '_notes_by_id', None) is not None and getattr(note, 'note_id', None):
                    self.canvas._notes_by_id[note.note_id] = note

            with silent_operation("Restore note connections"):
                for ln in list(self.connections_snapshot or []):
                    with silent_operation("Restore connection"):
                        a_id = ln.get('a')
                        b_id = ln.get('b')
                        a = self.canvas._notes_by_id.get(a_id)
                        b = self.canvas._notes_by_id.get(b_id)

                        if a is None and a_id == note.note_id:
                            a = note
                        if b is None and b_id == note.note_id:
                            b = note

                        if a is not None and b is not None and a is not b:
                            nln = self.canvas.add_note_connection(a, b)
                            if nln is not None:
                                with silent_operation("Set connection properties"):
                                    nln.label = ln.get('label', '')
                                    nln.manual_offset = float(ln.get('manual_offset', 0.0) or 0.0)
                                    nln.manual_angle = ln.get('manual_angle', None)
                                    nln.update_path()

            with silent_operation("Restore focus links"):
                for fid in list(self.focus_links_snapshot or []):
                    with silent_operation("Restore focus link"):
                        fnode = self.canvas.nodes.get(fid)
                        if fnode is not None:
                            self.canvas.add_note_focus_link(note, fnode)

            self.note = note


class MoveNoteCommand(QUndoCommand):
    """Undoable movement of a NoteNode."""
    def __init__(self, note: 'NoteNode', start: QPointF, end: QPointF,
                 description: str = "Move Note"):
        super().__init__(description)
        self.note = note
        self.start = QPointF(start)
        self.end = QPointF(end)

    def redo(self):
        with silent_operation("MoveNoteCommand.redo", log_level="debug"):
            self.note.setPos(self.end)

    def undo(self):
        with silent_operation("MoveNoteCommand.undo", log_level="debug"):
            self.note.setPos(self.start)


class ResizeNoteCommand(QUndoCommand):
    """Undoable resize of a NoteNode's rect."""
    def __init__(self, note: 'NoteNode', start_rect: QRectF, end_rect: QRectF,
                 description: str = "Resize Note"):
        super().__init__(description)
        self.note = note
        self.start = QRectF(start_rect)
        self.end = QRectF(end_rect)

    def redo(self):
        with silent_operation("ResizeNoteCommand.redo", log_level="debug"):
            self.note.prepareGeometryChange()
            self.note._rect = QRectF(self.end)
            self.note._layout_text()
            self.note.update()

    def undo(self):
        with silent_operation("ResizeNoteCommand.undo", log_level="debug"):
            self.note.prepareGeometryChange()
            self.note._rect = QRectF(self.start)
            self.note._layout_text()
            self.note.update()


class NoteCreateLinkCommand(QUndoCommand):
    """Undoable command to create a NoteNode and link it to a FocusNode."""
    def __init__(self, main_window, text: str, pos: QPointF, focus_node: 'FocusNode',
                 description: str = "Create Note and Link"):
        super().__init__(description)
        self.main_window = main_window
        self.canvas = getattr(main_window, 'canvas', None)
        self.text = text
        self.pos = pos
        self.focus_node = focus_node
        self.note = None

    def redo(self):
        if self.canvas is None:
            return
        with silent_operation("NoteCreateLinkCommand.redo", log_level="debug"):
            # Create note
            self.note = self.canvas._create_note_item(self.text, self.pos)
            with silent_operation("Add to notes_items"):
                self.canvas._notes_items.append(self.note)
            with silent_operation("Add to notes_by_id"):
                self.canvas._notes_by_id[self.note.note_id] = self.note
            self.note.set_visible(getattr(self.canvas, 'notes_enabled', True))
            # Link
            with silent_operation("Create note focus link"):
                self.canvas.add_note_focus_link(self.note, self.focus_node)

    def undo(self):
        if self.canvas is None or self.note is None:
            return
        with silent_operation("NoteCreateLinkCommand.undo", log_level="debug"):
            # Remove connectors referencing this note
            with silent_operation("Remove note focus links"):
                if hasattr(self.canvas, 'remove_note_focus_links_for'):
                    self.canvas.remove_note_focus_links_for(self.note)
            # Remove note connections
            with silent_operation("Remove note connections"):
                if hasattr(self.canvas, 'remove_note_connections_for'):
                    self.canvas.remove_note_connections_for(self.note)
            # Remove from scene and registries
            with silent_operation("Remove note from scene"):
                self.canvas.removeItem(self.note)
            with silent_operation("Remove from notes_items"):
                if self.note in getattr(self.canvas, '_notes_items', []):
                    self.canvas._notes_items.remove(self.note)
            with silent_operation("Remove from notes_by_id"):
                if getattr(self.canvas, '_notes_by_id', None) and self.note.note_id in self.canvas._notes_by_id:
                    del self.canvas._notes_by_id[self.note.note_id]


class LinkNoteFocusCommand(QUndoCommand):
    """Link a NoteNode to a FocusNode with undo/redo."""
    def __init__(self, canvas: 'FocusTreeCanvas', note: 'NoteNode', focus_node: 'FocusNode',
                 description: str = "Link Note to Focus"):
        super().__init__(description)
        self.canvas = canvas
        self.note = note
        self.focus = focus_node
        self.connector = None

    def redo(self):
        if self.canvas is None:
            return
        with silent_operation("LinkNoteFocusCommand.redo", log_level="debug"):
            self.connector = self.canvas.add_note_focus_link(self.note, self.focus)

    def undo(self):
        if self.canvas is None:
            return
        with silent_operation("LinkNoteFocusCommand.undo", log_level="debug"):
            self.canvas.remove_note_focus_links_for(self.note)

# endregion

# ============================================================================
# region Utility Commands
# ============================================================================

class MacroCommand(QUndoCommand):
    """A helper QUndoCommand that groups child commands into one macro action."""
    def __init__(self, description: str = "Macro"):
        super().__init__(description)
        self._children = []

    def addCommand(self, cmd: QUndoCommand):
        self._children.append(cmd)

    def redo(self):
        with silent_operation("MacroCommand.redo", log_level="debug"):
            for cmd in self._children:
                with silent_operation(f"Execute child command: {cmd.text()}"):
                    cmd.redo()

    def undo(self):
        with silent_operation("MacroCommand.undo", log_level="debug"):
            for cmd in reversed(self._children):
                with silent_operation(f"Undo child command: {cmd.text()}"):
                    cmd.undo()

# endregion

# ============================================================================
# region Module Exports
# ============================================================================

__all__ = [
    # Focus Node Commands
    'AddFocusCommand',
    'DeleteFocusCommand',

    # Focus Connections & Relationships
    'CreateConnectionCommand',
    'RemoveConnectionCommand',
    'MakeMutexCommand',

    # Focus Property Editing
    'EditFocusCommand',
    'SetIconCommand',

    # Focus Node Visual Manipulation
    'MoveNodeCommand',
    'ColorizeNodesCommand',

    # Event Commands
    'DeleteEventCommand',

    # Note Commands
    'CreateNoteCommand',
    'DeleteNoteCommand',
    'MoveNoteCommand',
    'ResizeNoteCommand',
    'NoteCreateLinkCommand',
    'LinkNoteFocusCommand',

    # Utility Commands
    'MacroCommand',
]

# endregion
