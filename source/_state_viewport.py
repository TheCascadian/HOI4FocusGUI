"""
State Viewport module

Provides a dockable map viewport that loads exact state/province polygons
(and optional province-to-state mapping) from JSON files and renders them
precisely using QGraphicsPolygonItem. Designed to support HOI4 modded maps
by accepting arbitrary polygon datasets.

ENHANCEMENTS (v2):
- TAG-based color coding: States are colored by their owner TAG using consistent hashing
- Outline-only mode now includes fill colors (not just transparent borders)
- Color legend dialog to show all TAGs and their colors
- Export integration with HOI4StateExporter verified and working
- Dynamic refresh when toggling outline mode

Expected JSON format (minimal):
{
  "states": {
    "<state_id>": {
      "name": "State Name",
      "polygons": [  # list of polygons; each polygon is a list of [x,y] points
        [[x1,y1], [x2,y2], ...]
      ],
      "provinces": [ "<prov_id>", ... ],  # optional
      "owner": "TAG",  # optional - used for color coding
      "cores": ["TAG1", "TAG2"],  # optional
      "manpower": 12345,  # optional
      "state_category": "rural"  # optional
    },
    ...
  }
}

This module intentionally keeps dependencies minimal and uses only PyQt6
objects already used by the application.
"""
# region File (auto-generated)
# endregion

import logging
from concurrent.futures import ThreadPoolExecutor

from _imports import (
    # Standard library
    json, math, os, re, sys, time, Optional,
    hashlib, shutil, threading, uuid,
    # Third-party
    Image,
    # Project
    CommandSpec, show_error,
)
from error_handler import ErrorPolicy, PolicyConfig, handle_exception

# PyQt6 wildcard imports - this module uses many Qt classes
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from PyQt6.QtWidgets import *


RESOURCE_KEYS = ['aluminium', 'chromium', 'oil', 'rubber', 'steel', 'tungsten']
AUTOLOAD_FILENAME = 'state_viewport_autoload.json'

def _write_state_sidecar(path: str, data: dict):
    """Write a state sidecar JSON where each state/province entry is on a single line.

    The output is a valid JSON object. The 'states' and 'provinces' mappings are
    emitted so that each key/value pair appears on its own line which makes
    diffs and reviews much easier.
    """
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('{' + '\n')
            # Write states
            states = data.get('states', {}) or {}
            f.write('  "states": {' + '\n')
            items = list(states.items())
            for i, (sid, entry) in enumerate(items):
                key = json.dumps(str(sid), ensure_ascii=False)
                # compact representation for the value with no internal newlines
                val = json.dumps(entry, ensure_ascii=False, separators=(',', ':'))
                comma = ',' if i < len(items) - 1 else ''
                f.write(f'    {key}: {val}{comma}\n')
            f.write('  }')

            # Write provinces if present
            provinces = data.get('provinces', None)
            if provinces is not None:
                f.write(',\n  "provinces": {\n')
                pitems = list(provinces.items())
                for i, (pid, pval) in enumerate(pitems):
                    pkey = json.dumps(str(pid), ensure_ascii=False)
                    pval_s = json.dumps(pval, ensure_ascii=False, separators=(',', ':'))
                    comma = ',' if i < len(pitems) - 1 else ''
                    f.write(f'    {pkey}: {pval_s}{comma}\n')
                f.write('  }\n')
            else:
                f.write('\n')

            f.write('}' + '\n')
        try:
            os.replace(tmp, path)
        except Exception:
            try:
                shutil.copy2(tmp, path)
                os.remove(tmp)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
    except Exception:
        # Fallback to a safe full dump if anything goes wrong
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, path)
            except Exception:
                shutil.copy2(tmp, path)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

class ConversionWorker(QObject):
    """Background worker to convert provinces->states and stream JSON to disk.

    Emits progress(int) and message(str), and finished(success: bool, out_path_or_error: str).
    """
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, prov_img: str, def_csv: str, states_txts: list, out_json: str, extractor_callable, parent=None):
        super().__init__(parent)
        self.prov_img = prov_img
        self.def_csv = def_csv
        # states_txts: list of paths to .txt state files (supports multi-select)
        self.states_txts = states_txts if isinstance(states_txts, (list, tuple)) else [states_txts]
        self.out_json = out_json
        self.extractor = extractor_callable
        self._cancel = False

    def request_cancel(self):
        self._cancel = True

    def run(self):
        """Minimal, robust conversion runner.

        Calls the provided extractor to obtain province polygons and writes a
        simple JSON file with placeholders for states (empty). This keeps the
        worker functional without relying on complex intermediate variables.
        """
        try:
            start_time = time.time()
            self.message.emit('Starting conversion...')
            if self._cancel:
                self.finished.emit(False, 'Cancelled')
                return

            # Run extractor in a thread to keep UI responsive
            province_polys: dict[str, list] = {}
            extractor_error: Exception | None = None
            done = threading.Event()

            def _run():
                nonlocal province_polys, extractor_error
                try:
                    province_polys = self.extractor(self.prov_img, self.def_csv) or {}
                except Exception as exc:
                    extractor_error = exc
                finally:
                    done.set()

            t = threading.Thread(target=_run, daemon=True)
            t.start()

            pulse = 0
            while not done.is_set():
                if self._cancel:
                    self.finished.emit(False, 'Cancelled')
                    return
                pulse = (pulse + 7) % 100
                try:
                    self.progress.emit(pulse)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                time.sleep(0.2)

            if extractor_error:
                self.finished.emit(False, f'Extractor failed: {extractor_error}')
                return

            # Parse state files to build province->state mapping
            self.message.emit('Parsing state definitions...')
            states_data = {}
            if self.states_txts:
                try:
                    # Import the parser from the converter module
                    import sys
                    import os
                    converter_path = os.path.join(os.path.dirname(__file__), '_hoi4_map_converter.py')
                    if os.path.exists(converter_path):
                        import importlib.util
                        spec = importlib.util.spec_from_file_location("_hoi4_map_converter", converter_path)
                        converter = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(converter)
                        parse_states_file = converter.parse_states_file
                        build_states_from_provinces = converter.build_states_from_provinces
                    else:
                        # Fallback: inline minimal parser
                        def parse_states_file(path):
                            import re
                            states = {}
                            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                                text = f.read()
                            text = re.sub(r'#.*', '', text)  # Remove comments
                            for m in re.finditer(r'state\s*=\s*\{', text):
                                start = m.end()
                                depth = 1
                                i = start
                                while i < len(text) and depth > 0:
                                    if text[i] == '{': depth += 1
                                    elif text[i] == '}': depth -= 1
                                    i += 1
                                block = text[start:i-1]
                                sid = None
                                provinces = []
                                name = None
                                id_m = re.search(r'\bid\s*=\s*([0-9]+)', block)
                                if id_m:
                                    sid = id_m.group(1)
                                name_m = re.search(r'name\s*=\s*"([^"]+)"', block)
                                if name_m:
                                    name = name_m.group(1)
                                prov_m = re.search(r'provinces\s*=\s*\{([^\}]*)\}', block, re.S)
                                if prov_m:
                                    nums = re.findall(r'(-?\d+)', prov_m.group(1))
                                    provinces = [str(int(n)) for n in nums]
                                if sid:
                                    states[sid] = {'name': name or f'State {sid}', 'provinces': provinces}
                            return states

                        def build_states_from_provinces(prov_polys, states_map):
                            states_out = {}
                            for sid, meta in states_map.items():
                                provs = meta.get('provinces', []) or []
                                state_polys = []
                                for pid in provs:
                                    if str(pid) in prov_polys:
                                        state_polys.extend(prov_polys[str(pid)])
                                states_out[sid] = {
                                    'name': meta.get('name', f'State {sid}'),
                                    'polygons': state_polys,
                                    'provinces': [str(p) for p in provs]
                                }
                            return {'provinces': prov_polys, 'states': states_out}

                    # Parse all state files
                    all_states = {}
                    for states_txt in self.states_txts:
                        if self._cancel:
                            self.finished.emit(False, 'Cancelled')
                            return
                        try:
                            parsed = parse_states_file(states_txt)
                            all_states.update(parsed)
                            self.message.emit(f'Parsed {len(parsed)} states from {os.path.basename(states_txt)}')
                        except Exception as e:
                            self.message.emit(f'Warning: Failed to parse {states_txt}: {e}')

                    # Build final output with provinces mapped to states
                    if all_states:
                        final_data = build_states_from_provinces(province_polys, all_states)
                        self.message.emit(f'Built {len(final_data.get("states", {}))} states from {len(province_polys)} provinces')
                    else:
                        self.message.emit('Warning: No states parsed, using provinces only')
                        final_data = {"states": {}, "provinces": province_polys}
                except Exception as e:
                    self.message.emit(f'Warning: State parsing failed ({e}), using provinces only')
                    final_data = {"states": {}, "provinces": province_polys}
            else:
                final_data = {"states": {}, "provinces": province_polys}

            # Write minimal output with one-line-per-item provinces for easier diffs
            try:
                _write_state_sidecar(self.out_json, final_data)
            except Exception:
                tmp_out = f"{self.out_json}.tmp"
                with open(tmp_out, 'w', encoding='utf-8') as f:
                    json.dump(final_data, f, ensure_ascii=False)
                try:
                    os.replace(tmp_out, self.out_json)
                except Exception:
                    try:
                        shutil.copy2(tmp_out, self.out_json)
                        os.remove(tmp_out)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            self.progress.emit(100)
            elapsed = int(time.time() - start_time)
            self.message.emit(f'Conversion finished in {elapsed}s.')
            self.finished.emit(True, self.out_json)
        except Exception as e:
            try:
                self.finished.emit(False, f'Unexpected error: {e}')
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

class ProvincePolygonItem(QGraphicsPolygonItem):
    """Graphics item representing a single province polygon (atomic map unit)."""
    def __init__(self, province_id: str, poly: QPolygonF, state_id: Optional[str] = None, owner_tag: Optional[str] = None, parent=None):
        super().__init__(poly, parent)
        self.province_id = province_id
        self.state_id = state_id
        self.owner_tag = owner_tag
        # LOD cache: store original points and simplified variants by epsilon key
        try:
            self._orig_points = [(poly.at(i).x(), poly.at(i).y()) for i in range(poly.count())]
        except Exception:
            self._orig_points = []
        self._lod_cache: dict[float, QPolygonF] = {}
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsFocusable)
        self.setAcceptHoverEvents(True)
        # Province border - thin dark line
        pen = QPen(QColor(30, 30, 30, 180), 0.3)
        # Province fill - color by owner TAG
        if owner_tag:
            brush = QBrush(self._tag_to_color(owner_tag))
        else:
            brush = QBrush(QColor(200, 200, 220, 100))
        self.setPen(pen)
        self.setBrush(brush)
        self.setZValue(0)  # Provinces at base layer

    def update_for_scale(self, scale: float):
        """Adjust polygon detail based on view scale using RDP simplification.

        At far zoom, reduce point count substantially; at medium zoom, lightly simplify;
        at close zoom, use full geometry. Results are cached per epsilon.
        """
        try:
            if not self._orig_points:
                return
            # Choose epsilon based on scale (tuned heuristics)
            if scale <= 0.1:
                eps = 6.0
            elif scale <= 0.2:
                eps = 3.5
            elif scale <= 0.4:
                eps = 2.0
            elif scale <= 0.8:
                eps = 1.0
            else:
                eps = 0.0  # full detail

            if eps <= 0.0:
                # restore original
                qpoly = QPolygonF([QPointF(x, y) for x, y in self._orig_points])
                self.setPolygon(qpoly)
                return

            if eps in self._lod_cache:
                self.setPolygon(self._lod_cache[eps])
                return

            # Build simplified polygon
            simp = _rdp_simplify(self._orig_points, eps)
            if not simp or len(simp) < 3:
                simp = self._orig_points
            qpoly = QPolygonF([QPointF(x, y) for x, y in simp])
            self._lod_cache[eps] = qpoly
            self.setPolygon(qpoly)
        except Exception:
            # Never break rendering due to LOD issues
            pass

    def _tag_to_color(self, tag: str) -> QColor:
        """Generate a consistent color for a given TAG using hash-based coloring"""
        hash_obj = hashlib.md5(tag.encode())
        hash_bytes = hash_obj.digest()
        r = (hash_bytes[0] % 180) + 60
        g = (hash_bytes[1] % 180) + 60
        b = (hash_bytes[2] % 180) + 60
        return QColor(r, g, b, 140)  # Solid fill for provinces

    def hoverEnterEvent(self, ev):
        try:
            pen = self.pen()
            pen.setWidthF(1.5)
            pen.setColor(QColor(255, 200, 0, 255))
            self.setPen(pen)
            self.setZValue(5)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        super().hoverEnterEvent(ev)

    def hoverLeaveEvent(self, ev):
        try:
            pen = self.pen()
            pen.setWidthF(0.3)
            pen.setColor(QColor(30, 30, 30, 180))
            self.setPen(pen)
            self.setZValue(0)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        super().hoverLeaveEvent(ev)

class StatePolygonItem(QGraphicsPolygonItem):
    """Graphics item representing a state boundary (collection of provinces).

    States are rendered as thick boundary lines around province groups,
    NOT as filled polygons. This allows provinces to remain visible.
    """
    def __init__(self, state_id: str, poly: QPolygonF, owner_tag: Optional[str] = None, parent=None):
        super().__init__(poly, parent)
        self.state_id = state_id
        self.owner_tag = owner_tag
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsFocusable)
        self.setAcceptHoverEvents(True)
        # State border - thick colored line, NO FILL
        pen = QPen(self._tag_to_border_color(owner_tag) if owner_tag else QColor(80, 80, 120, 200), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        # TRANSPARENT FILL - we only show the border
        brush = QBrush(Qt.BrushStyle.NoBrush)
        self.setPen(pen)
        self.setBrush(brush)
        self.setZValue(1)  # State boundaries above provinces

    def _tag_to_border_color(self, tag: str) -> QColor:
        """Generate a darker border color for state boundaries"""
        hash_obj = hashlib.md5(tag.encode())
        hash_bytes = hash_obj.digest()
        # Darker colors for borders
        r = (hash_bytes[0] % 120) + 40
        g = (hash_bytes[1] % 120) + 40
        b = (hash_bytes[2] % 120) + 40
        return QColor(r, g, b, 220)

    def _tag_to_color(self, tag: str) -> QColor:
        """Provide a fill color for legend swatches. Matches ProvincePolygonItem hashing."""
        hash_obj = hashlib.md5(tag.encode())
        hash_bytes = hash_obj.digest()
        r = (hash_bytes[0] % 180) + 60
        g = (hash_bytes[1] % 180) + 60
        b = (hash_bytes[2] % 180) + 60
        return QColor(r, g, b, 180)

    def hoverEnterEvent(self, ev):
        try:
            pen = self.pen()
            pen.setWidthF(3.5)
            pen.setColor(QColor(255, 255, 50, 255))
            self.setPen(pen)
            self.setZValue(10)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        super().hoverEnterEvent(ev)

    def hoverLeaveEvent(self, ev):
        try:
            pen = self.pen()
            pen.setWidthF(2.0)
            if self.owner_tag:
                pen.setColor(self._tag_to_border_color(self.owner_tag))
            else:
                pen.setColor(QColor(80, 80, 120, 200))
            self.setPen(pen)
            self.setZValue(1)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        super().hoverLeaveEvent(ev)

class PanZoomView(QGraphicsView):
    """QGraphicsView with mouse wheel zoom, middle-button pan, and lasso selection.

    Emits view_changed when the viewport/transform changes so callers can
    cull/create visible items.
    """
    view_changed = pyqtSignal()
    lasso_selection_changed = pyqtSignal(list)  # emits list of state_ids

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_wheel = 0
        # Panning state (middle mouse)
        self.panning = False
        self._pan_start_x = 0
        self._pan_start_y = 0
        # Lasso selection state (Shift+Left drag)
        self.lasso_active = False
        self.lasso_path = None  # QGraphicsPathItem for visual feedback
        self.lasso_polygon = []  # List of QPointF for the lasso polygon
        # Zoom limits to avoid excessive rendering work
        self.MIN_SCALE = 0.05
        self.MAX_SCALE = 8.0
        # connect scrollbars to emit view_changed
        try:
            self.horizontalScrollBar().valueChanged.connect(lambda _: self.view_changed.emit())
            self.verticalScrollBar().valueChanged.connect(lambda _: self.view_changed.emit())
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def wheelEvent(self, ev):
        # Zoom centered at mouse position
        try:
            delta = ev.angleDelta().y()
            factor = 1.0 + (0.0015 * delta)
            # clamp resulting scale to MIN_SCALE..MAX_SCALE
            try:
                cur = self.transform().m11()
            except Exception:
                cur = 1.0
            new_scale = cur * factor
            if new_scale > self.MAX_SCALE:
                # adjust factor so we only reach MAX_SCALE
                if cur <= 0:
                    factor = 1.0
                else:
                    factor = self.MAX_SCALE / cur
                    new_scale = self.MAX_SCALE
            elif new_scale < self.MIN_SCALE:
                if cur <= 0:
                    factor = 1.0
                else:
                    factor = self.MIN_SCALE / cur
                    new_scale = self.MIN_SCALE
            # if factor is effectively 1, do nothing
            if abs(factor - 1.0) < 1e-9:
                return
            old_pos = self.mapToScene(ev.position().toPoint())
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
            self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
            self.scale(factor, factor)
            new_pos = self.mapToScene(ev.position().toPoint())
            diff = new_pos - old_pos
            self.translate(diff.x(), diff.y())
            # Adjust render hints based on new scale for performance
            try:
                eff_scale = self.transform().m11()
                self._apply_render_hints_for_scale(eff_scale)
                # LOD update for items currently in the scene
                for item in self.scene().items():
                    upd = getattr(item, 'update_for_scale', None)
                    if callable(upd):
                        upd(eff_scale)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.view_changed.emit()
        except Exception:
            super().wheelEvent(ev)

    def mouseMoveEvent(self, ev):
        # If panning via middle button, translate scrollbars like the main view
        try:
            if getattr(self, 'panning', False):
                delta_x = ev.position().x() - self._pan_start_x
                delta_y = ev.position().y() - self._pan_start_y
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta_x))
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta_y))
                self._pan_start_x = ev.position().x()
                self._pan_start_y = ev.position().y()
                # notify listeners
                try:
                    self.view_changed.emit()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                return
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        # Lasso selection: update polygon path
        try:
            if getattr(self, 'lasso_active', False):
                scene_pos = self.mapToScene(ev.position().toPoint())
                self.lasso_polygon.append(scene_pos)
                # Update visual path
                if self.lasso_path:
                    from PyQt6.QtGui import QPainterPath
                    path = QPainterPath()
                    if self.lasso_polygon:
                        path.moveTo(self.lasso_polygon[0])
                        for pt in self.lasso_polygon[1:]:
                            path.lineTo(pt)
                        # Close the path for visual feedback
                        if len(self.lasso_polygon) > 2:
                            path.lineTo(self.lasso_polygon[0])
                    self.lasso_path.setPath(path)
                return
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        return super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.MiddleButton:
            # Start panning (explicit handling)
            try:
                self.panning = True
                self._pan_start_x = ev.position().x()
                self._pan_start_y = ev.position().y()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # Do not switch drag mode for middle-button; we've handled panning manually
            return
        elif ev.button() == Qt.MouseButton.LeftButton and (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            # Start lasso selection (Shift+Left drag)
            try:
                self.lasso_active = True
                self.lasso_polygon = [self.mapToScene(ev.position().toPoint())]
                # Create visual feedback path
                from PyQt6.QtGui import QPen, QPainterPath
                from PyQt6.QtCore import Qt as QtCore
                if not self.lasso_path:
                    self.lasso_path = self.scene().addPath(QPainterPath())
                    pen = QPen(QtCore.GlobalColor.blue, 2, QtCore.PenStyle.DashLine)
                    self.lasso_path.setPen(pen)
                    self.lasso_path.setZValue(999999)  # Draw on top
                self.setCursor(Qt.CursorShape.CrossCursor)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return
        else:
            # Left-button and others use default behaviour (selection/rubber-band)
            return super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.MiddleButton:
            try:
                self.panning = False
                self.setCursor(Qt.CursorShape.ArrowCursor)
                try:
                    self.view_changed.emit()
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return
        elif ev.button() == Qt.MouseButton.LeftButton and getattr(self, 'lasso_active', False):
            # Complete lasso selection
            try:
                self.lasso_active = False
                self.setCursor(Qt.CursorShape.ArrowCursor)
                # Find all items within the lasso polygon
                selected_ids = self._select_items_in_lasso()
                # Emit signal for the dock to handle
                try:
                    self.lasso_selection_changed.emit(selected_ids)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # Clean up visual path
                if self.lasso_path:
                    try:
                        self.scene().removeItem(self.lasso_path)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.lasso_path = None
                self.lasso_polygon = []
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return
        return super().mouseReleaseEvent(ev)

    def _select_items_in_lasso(self) -> list[str]:
        """Find all StatePolygonItem instances whose centroids fall within the lasso polygon"""
        if len(self.lasso_polygon) < 3:
            return []

        try:
            from PyQt6.QtGui import QPolygonF
            lasso_qpoly = QPolygonF(self.lasso_polygon)

            selected_ids = set()
            for item in self.scene().items():
                # Check if it's a StatePolygonItem
                state_id = getattr(item, 'state_id', None)
                if not state_id:
                    continue

                # Use item's bounding rect center for fast containment test
                center = item.boundingRect().center()
                scene_center = item.mapToScene(center)

                if lasso_qpoly.containsPoint(scene_center, Qt.FillRule.OddEvenFill):
                    selected_ids.add(state_id)

            return list(selected_ids)
        except Exception:
            return []

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        try:
            eff_scale = self.current_scale()
            self._apply_render_hints_for_scale(eff_scale)
            # Update LOD of items on resize as well
            for item in self.scene().items():
                upd = getattr(item, 'update_for_scale', None)
                if callable(upd):
                    upd(eff_scale)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.view_changed.emit()

    def current_scale(self) -> float:
        try:
            return float(self.transform().m11())
        except Exception:
            return 1.0

    def _apply_render_hints_for_scale(self, scale: float):
        """Switch antialiasing off at far zoom for speed; on when zoomed in."""
        try:
            rh = QPainter.RenderHint
            if scale <= 0.2:
                self.setRenderHints(rh.SmoothPixmapTransform)  # no Antialiasing
            else:
                self.setRenderHints(rh.Antialiasing | rh.SmoothPixmapTransform)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

def _rdp_simplify(points, epsilon: float):
    """Simple Ramer-Douglas-Peucker polyline simplifier for display only.
    Keeps first/last and removes points within epsilon distance.
    """
    if not points or epsilon <= 0:
        return points

    def _perp(a, b, c):
        # distance from c to line a-b
        ax, ay = a; bx, by = b; cx, cy = c
        dx = bx - ax; dy = by - ay
        if dx == 0 and dy == 0:
            return math.hypot(cx - ax, cy - ay)
        return abs(dy * cx - dx * cy + bx * ay - by * ax) / math.hypot(dx, dy)

    def _rdp(pts, eps):
        if len(pts) < 3:
            return pts
        maxd = 0.0
        idx = 0
        a = pts[0]; b = pts[-1]
        for i in range(1, len(pts) - 1):
            d = _perp(a, b, pts[i])
            if d > maxd:
                idx = i; maxd = d
        if maxd > eps:
            left = _rdp(pts[:idx+1], eps)
            right = _rdp(pts[idx:], eps)
            return left[:-1] + right
        else:
            return [pts[0], pts[-1]]

    return _rdp(points, epsilon)

def _convex_hull(points: list) -> list:
    """Compute a simple convex hull (Monotone chain) for a list of [x,y] points.

    Returns list of points in hull order. If less than 3 points, returns input.
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 3:
        return [[float(x), float(y)] for x, y in pts]

    pts = sorted(set(pts))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    hull = lower[:-1] + upper[:-1]
    return [[float(x), float(y)] for x, y in hull]

class ResourceAdjustDialog(QDialog):
    """Lightweight dialog to adjust state resource outputs."""

    def __init__(self, parent=None, base: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle('Adjust Resources')
        self.setModal(True)
        layout = QFormLayout(self)
        self._spins = {}
        base = base or {}
        for key in RESOURCE_KEYS:
            spin = QSpinBox(self)
            spin.setRange(-999999, 999999)
            try:
                spin.setValue(int(base.get(key, 0)))
            except Exception:
                spin.setValue(0)
            layout.addRow(key.capitalize(), spin)
            self._spins[key] = spin
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self) -> dict:
        return {key: spin.value() for key, spin in self._spins.items()}

class QuickTransferDialog(QDialog):
    """Dialog allowing the user to move provinces from one state to another."""

    def __init__(self, parent, state_id: str, provinces: list[str], destination_choices: list[tuple[str, str]]):
        super().__init__(parent)
        self.setWindowTitle(f'Quick Transfer - State {state_id}')
        self.setModal(True)
        self._combo = QComboBox(self)
        for sid, name in destination_choices:
            label = f"{sid}: {name}" if name else sid
            self._combo.addItem(label, sid)

        self._list = QListWidget(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        for pid in provinces:
            item = QListWidgetItem(str(pid))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self._list.addItem(item)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Destination State:', self))
        layout.addWidget(self._combo)
        layout.addWidget(QLabel('Select provinces to transfer:', self))
        layout.addWidget(self._list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_provinces(self) -> list[str]:
        result = []
        for idx in range(self._list.count()):
            item = self._list.item(idx)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.text())
        return result

    def _convex_hull(points: list) -> list:
        """Compute a simple convex hull (Monotone chain) for a list of [x,y] points.

        Returns list of points in hull order. If less than 3 points, returns input.
        """
        pts = [(float(p[0]), float(p[1])) for p in points]
        if len(pts) < 3:
            return [list(p) for p in pts]

        pts = sorted(set(pts))

        def cross(o, a, b):
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

        lower = []
        for p in pts:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
                lower.pop()
            lower.append(p)

        upper = []
        for p in reversed(pts):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
                upper.pop()
            upper.append(p)

        hull = lower[:-1] + upper[:-1]
        return [[float(x), float(y)] for x, y in hull]

    def destination_state(self) -> Optional[str]:
        return self._combo.currentData()

class GroupTransferDialog(QDialog):
    """Dialog for bulk transferring state data to a target state."""

    def __init__(self, parent, selected_ids: list[str], state_choices: list[tuple[str, str]]):
        super().__init__(parent)
        self.setWindowTitle('Advanced Group Transfer (Provinces/Metadata)')
        self.setModal(True)
        layout = QVBoxLayout(self)

        self._template_state = selected_ids[0] if selected_ids else None
        instructions = QLabel(
            'Select a target state to receive data from the selected states.\n'
            'You can merge provinces and optionally copy metadata from the template state '
            '(first selected state).' ,
            self
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        self._target_combo = QComboBox(self)
        for sid, name in state_choices:
            label = f"{sid}: {name}" if name else sid
            self._target_combo.addItem(label, sid)
        layout.addWidget(QLabel('Target State:', self))
        layout.addWidget(self._target_combo)

        self._merge_check = QCheckBox('Move provinces from selected states into the target state', self)
        self._merge_check.setChecked(True)
        layout.addWidget(self._merge_check)

        self._copy_owner = QCheckBox('Copy owner from template state to all selected states', self)
        self._copy_category = QCheckBox('Copy state category from template state', self)
        self._copy_manpower = QCheckBox('Copy manpower from template state', self)
        self._copy_resources = QCheckBox('Copy resources from template state', self)
        for cb in (self._copy_owner, self._copy_category, self._copy_manpower, self._copy_resources):
            cb.setChecked(False)
            layout.addWidget(cb)

        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def target_state(self) -> Optional[str]:
        return self._target_combo.currentData()

    def move_provinces(self) -> bool:
        return self._merge_check.isChecked()

    def copy_flags(self) -> dict:
        return {
            'owner': self._copy_owner.isChecked(),
            'state_category': self._copy_category.isChecked(),
            'manpower': self._copy_manpower.isChecked(),
            'resources': self._copy_resources.isChecked(),
        }

class TagTransferDialog(QDialog):
    """Dialog focused on transferring owner/core TAGs across selected states.

    Allows replacing an existing owner/core tag with a different tag for all
    selected states. Other advanced province/metadata options are available in
    a separate "Advanced Group Transfer…" action.
    """

    def __init__(self, parent, selected_ids: list[str], owners_in_sel: set[str], cores_in_sel: set[str]):
        super().__init__(parent)
        self.setWindowTitle('Group Transfer (Tags)')
        self.setModal(True)

        layout = QVBoxLayout(self)
        instructions = QLabel(
            'Replace owner/core TAGs across the selected states.\n'
            'Choose which TAG to replace (From) and what to set it to (To).',
            self
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        form = QFormLayout()

        # Owner controls
        self._owner_chk = QCheckBox('Change Owner tag', self)
        self._owner_chk.setChecked(True)
        layout.addWidget(self._owner_chk)

        self._owner_from = QComboBox(self)
        self._owner_from.setEditable(True)
        for t in sorted([t for t in owners_in_sel if t]):
            self._owner_from.addItem(t)
        if self._owner_from.count() == 0:
            self._owner_from.addItem('')
        self._owner_to = QLineEdit(self)
        self._owner_to.setPlaceholderText('NEW TAG (e.g., GER)')
        form.addRow('Owner: From', self._owner_from)
        form.addRow('Owner: To', self._owner_to)

        # Core controls
        self._core_chk = QCheckBox('Replace Core tag', self)
        self._core_chk.setChecked(False)
        layout.addWidget(self._core_chk)

        self._core_from = QComboBox(self)
        self._core_from.setEditable(True)
        for t in sorted([t for t in cores_in_sel if t]):
            self._core_from.addItem(t)
        if self._core_from.count() == 0:
            self._core_from.addItem('')
        self._core_to = QLineEdit(self)
        self._core_to.setPlaceholderText('NEW TAG (e.g., GER)')
        form.addRow('Core: From', self._core_from)
        form.addRow('Core: To', self._core_to)

        layout.addLayout(form)

        # Enable/disable fields based on checkboxes
        def _sync_enabled():
            owner_enabled = self._owner_chk.isChecked()
            core_enabled = self._core_chk.isChecked()
            self._owner_from.setEnabled(owner_enabled)
            self._owner_to.setEnabled(owner_enabled)
            self._core_from.setEnabled(core_enabled)
            self._core_to.setEnabled(core_enabled)
        self._owner_chk.toggled.connect(_sync_enabled)
        self._core_chk.toggled.connect(_sync_enabled)
        _sync_enabled()

        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # Helpers to retrieve normalized values
    def owner_change(self) -> bool:
        return self._owner_chk.isChecked()

    def owner_from(self) -> str:
        return (self._owner_from.currentText() or '').strip().upper()

    def owner_to(self) -> str:
        return (self._owner_to.text() or '').strip().upper()

    def core_change(self) -> bool:
        return self._core_chk.isChecked()

    def core_from(self) -> str:
        return (self._core_from.currentText() or '').strip().upper()

    def core_to(self) -> str:
        return (self._core_to.text() or '').strip().upper()

class StateViewportDock(QDockWidget):
    """A dockable viewport for exact-state selection and inspection."""

    state_selection_changed = pyqtSignal(list)  # list of selected state ids

    def __init__(self, parent=None):
        super().__init__("State Viewport", parent)
        self.setObjectName('StateViewportDock')
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self._widget = QWidget(self)
        self.setWidget(self._widget)
        layout = QVBoxLayout(self._widget)
        # toolbar row
        row = QHBoxLayout()
        self.load_btn = QPushButton('Load States...')
        row.addWidget(self.load_btn)
        self.load_mod_btn = QPushButton('Load HOI4 Mod...')
        row.addWidget(self.load_mod_btn)
        self.clear_btn = QPushButton('Clear Selection')
        row.addWidget(self.clear_btn)
        self.select_all_btn = QPushButton('Select All')
        row.addWidget(self.select_all_btn)
        self.autoload_btn = QPushButton('Set Startup Map')
        self.autoload_btn.setEnabled(False)
        row.addWidget(self.autoload_btn)
        # Option: persist currently-loaded map as a sidecar next to the project file
        try:
            self.persist_sidecar_chk = QCheckBox('Save map as sidecar')
            self.persist_sidecar_chk.setToolTip('When checked, the current map will be written out as a sidecar "<project>.states.json" when the project is saved. Uncheck to embed states directly into the project JSON instead.')
            # Preserve historical behaviour: default to True (write sidecar)
            self.persist_sidecar_chk.setChecked(True)
            # Expose a simple attribute for external code to read
            self.persist_sidecar = True
            def _toggle_persist_sidecar(chk: bool):
                try:
                    self.persist_sidecar = bool(chk)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.persist_sidecar_chk.toggled.connect(_toggle_persist_sidecar)
            row.addWidget(self.persist_sidecar_chk)
        except Exception:
            # If widgets aren't available for some reason, ensure attribute exists
            self.persist_sidecar = True
        # Outline-only toggle: hide province/internal polygons and render only
        # a single state outline (convex-hull approximation).
        self.outline_only_chk = QCheckBox('Outline only')
        self.outline_only_chk.setToolTip('Hide province boundaries and render only the state outline')
        self.outline_only_chk.setChecked(False)
        row.addWidget(self.outline_only_chk)
        # Color legend button
        self.legend_btn = QPushButton('Legend')
        self.legend_btn.setToolTip('Show color legend for owner TAGs')
        row.addWidget(self.legend_btn)
        # Country tags import button (loads a plain list of TAGs for suggestions)
        self.tags_btn = QPushButton('Country Tags')
        self.tags_btn.setToolTip('Import a country_tags.txt file to provide TAG suggestions')
        row.addWidget(self.tags_btn)
    # Dock-local zoom label (shows current view zoom) — attached to this dock's toolbar row
        row.addStretch()
        try:
            self.view_zoom_label = QLabel('100%')
            self.view_zoom_label.setToolTip('Viewport zoom')
            # keep a small fixed width so it aligns nicely
            try:
                self.view_zoom_label.setFixedWidth(56)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            row.addWidget(self.view_zoom_label)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        layout.addLayout(row)

        # Inline progress controls for the dock (hidden by default)
        self._progress_label = QLabel('', self._widget)
        self._progress_label.setVisible(False)
        self._progress_bar = QProgressBar(self._widget)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setVisible(False)
        prog_row = QHBoxLayout()
        prog_row.addWidget(self._progress_label)
        prog_row.addWidget(self._progress_bar)
        layout.addLayout(prog_row)

        # Graphics view/scene
        self.scene = QGraphicsScene(self)
        self.view = PanZoomView(self.scene, self._widget)
        self.view.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        # update visible items when view changes
        try:
            self.view.view_changed.connect(lambda: self.update_visible_items())
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Register these commands with the application's KeybindsManager if present
        try:
            cmds = []
            from _dataStructs import CommandSpec
            def _reg(cid, label, cb, default=None):
                cmds.append(CommandSpec(cid=cid, label=label, callback=cb, default=default, category='State Viewport'))

            _reg('state.select_all', 'Select All States', self.select_all, 'Ctrl+A')
            _reg('state.load', 'Load States', self.load_states_dialog, 'Ctrl+L')
            _reg('state.load_mod', 'Load HOI4 Mod', self.load_hoi4_mod_dialog, None)
            _reg('state.quick_transfer', 'Quick Transfer Provinces', self._op_quick_transfer, 'Ctrl+T')
            _reg('state.group_transfer', 'Group Transfer Provinces', self._op_group_transfer, 'Ctrl+Shift+T')
            _reg('state.remove_cores', 'Remove All Cores', self._op_remove_all_cores, 'Delete')
            _reg('state.manpower_x2', 'Increase Manpower x2', lambda: self._op_increase_manpower(2), 'Ctrl+Shift+M')
            _reg('state.manpower_reset', 'Reset Manpower', lambda: self._op_increase_manpower(0), 'Ctrl+Shift+R')
            _reg('state.toggle_outline', 'Toggle Outline Only', lambda: self.outline_only_chk.toggle(), 'Space')

            # Find a top-level window that has a keybinds manager
            win = None
            try:
                # prefer parent/parentWidget route
                win = getattr(self, 'parent', None) or getattr(self, 'parentWidget', lambda: None)()
            except Exception:
                win = None
            if win is None:
                # fallback: scan QApplication top-level widgets
                try:
                    from PyQt6.QtWidgets import QApplication
                    for w in QApplication.topLevelWidgets():
                        if hasattr(w, 'keybinds') and getattr(w, 'keybinds', None) is not None:
                            win = w
                            break
                except Exception:
                    win = None

            if win is not None and hasattr(win, 'keybinds') and getattr(win, 'keybinds', None) is not None:
                try:
                    win.keybinds.register_commands(cmds)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # update local zoom label when view changes
        try:
            self.view.view_changed.connect(lambda: self._update_view_zoom())
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        layout.addWidget(self.view, 1)

        # internal storage
        self.state_items = {}  # state_id -> list of StatePolygonItem (boundaries only)
        self.province_items = {}  # province_id -> list of ProvincePolygonItem (filled polygons)
        self._state_meta = {}  # state_id -> meta (name, provinces)
        self._dirty_states = set()
        self._province_polys: dict[str, list] = {}
        # Optional list of country TAGs imported from a plain text file
        self._country_tags: list[str] = []
        self._states_needing_refresh: set[str] = set()
        self._current_states_path: Optional[str] = None
        self._map_loaded = False

        # Optimization: spatial index for faster viewport culling
        # Maps state_id -> QRectF bounds for quick intersection tests
        self._spatial_index: dict[str, QRectF] = {}
        # Cache for rendered items to avoid redundant creation
        self._item_cache_generation = 0  # Increment when state data changes

        # connections
        self.load_btn.clicked.connect(self.load_states_dialog)
        self.load_mod_btn.clicked.connect(self.load_hoi4_mod_dialog)
        self.clear_btn.clicked.connect(self.clear_selection)
        self.select_all_btn.clicked.connect(self.select_all)
        self.scene.selectionChanged.connect(self._on_scene_selection_changed)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._open_context_menu)
        self.state_selection_changed.connect(self._update_operations_state)
        self.autoload_btn.clicked.connect(self._set_startup_map_from_current)
        # Connect lasso selection signal
        try:
            self.view.lasso_selection_changed.connect(self._handle_lasso_selection)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Connect outline-only toggle to refresh the view
        try:
            self.outline_only_chk.stateChanged.connect(lambda: self.update_visible_items(force=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Connect legend button
        try:
            self.legend_btn.clicked.connect(self._show_tag_legend)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.tags_btn.clicked.connect(self._import_country_tags_dialog)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self._update_operations_state([])
        QTimer.singleShot(0, self._load_autoload_map_if_available)

    def get_command_specs(self) -> list:
        """Return a list of CommandSpec objects describing keybinds for the State Viewport.

        This is used by the application's KeybindsManager to surface these commands
        in the central keybindings editor under the 'State Viewport' category.
        """
        try:
            from _dataStructs import CommandSpec
            cmds = []
            def _reg(cid, label, cb, default=None):
                cmds.append(CommandSpec(cid=cid, label=label, callback=cb, default=default, category='State Viewport'))

            _reg('state.select_all', 'Select All States', self.select_all, 'Ctrl+A')
            _reg('state.load', 'Load States', self.load_states_dialog, 'Ctrl+L')
            _reg('state.load_mod', 'Load HOI4 Mod', self.load_hoi4_mod_dialog, None)
            _reg('state.quick_transfer', 'Quick Transfer Provinces', self._op_quick_transfer, 'Ctrl+T')
            _reg('state.group_transfer', 'Group Transfer Provinces', self._op_group_transfer, 'Ctrl+Shift+T')
            _reg('state.remove_cores', 'Remove All Cores', self._op_remove_all_cores, 'Delete')
            _reg('state.manpower_x2', 'Increase Manpower x2', lambda: self._op_increase_manpower(2), 'Ctrl+Shift+M')
            _reg('state.manpower_reset', 'Reset Manpower', lambda: self._op_increase_manpower(0), 'Ctrl+Shift+R')
            _reg('state.toggle_outline', 'Toggle Outline Only', lambda: self.outline_only_chk.toggle(), 'Space')
            return cmds
        except Exception:
            return []

        # ---- Quick keybind actions (scoped to this dock) ----
        try:
            # Select all (Ctrl+A)
            act_select_all = QAction('Select All', self)
            act_select_all.setShortcut(QKeySequence('Ctrl+A'))
            act_select_all.setToolTip('Select all states (Ctrl+A)')
            act_select_all.triggered.connect(self.select_all)
            self.addAction(act_select_all)

            # Load states (Ctrl+L)
            act_load = QAction('Load States', self)
            act_load.setShortcut(QKeySequence('Ctrl+L'))
            act_load.setToolTip('Load states from JSON (Ctrl+L)')
            act_load.triggered.connect(self.load_states_dialog)
            self.addAction(act_load)

            # Quick Transfer (Ctrl+T)
            act_quick_transfer = QAction('Quick Transfer', self)
            act_quick_transfer.setShortcut(QKeySequence('Ctrl+T'))
            act_quick_transfer.setToolTip('Quick transfer provinces from selected state (Ctrl+T)')
            act_quick_transfer.triggered.connect(self._op_quick_transfer)
            self.addAction(act_quick_transfer)

            # Group Transfer (Ctrl+Shift+T)
            act_group_transfer = QAction('Group Transfer', self)
            act_group_transfer.setShortcut(QKeySequence('Ctrl+Shift+T'))
            act_group_transfer.setToolTip('Group transfer provinces between multiple selected states (Ctrl+Shift+T)')
            act_group_transfer.triggered.connect(self._op_group_transfer)
            self.addAction(act_group_transfer)

            # Remove All Cores (Delete)
            act_remove_cores = QAction('Remove All Cores', self)
            act_remove_cores.setShortcut(QKeySequence(Qt.Key.Key_Delete))
            act_remove_cores.setToolTip('Remove all core TAGs from selected states (Delete)')
            act_remove_cores.triggered.connect(self._op_remove_all_cores)
            self.addAction(act_remove_cores)

            # Increase manpower x2 (Ctrl+Shift+M) and reset manpower (Ctrl+Shift+R)
            act_manpow_x2 = QAction('Increase Manpower x2', self)
            act_manpow_x2.setShortcut(QKeySequence('Ctrl+Shift+M'))
            act_manpow_x2.setToolTip('Multiply selected states manpower by 2 (Ctrl+Shift+M)')
            act_manpow_x2.triggered.connect(lambda: self._op_increase_manpower(2))
            self.addAction(act_manpow_x2)

            act_manpow_reset = QAction('Reset Manpower', self)
            act_manpow_reset.setShortcut(QKeySequence('Ctrl+Shift+R'))
            act_manpow_reset.setToolTip('Set manpower of selected states to 0 (Ctrl+Shift+R)')
            act_manpow_reset.triggered.connect(lambda: self._op_increase_manpower(0))
            self.addAction(act_manpow_reset)

            # Toggle outline-only (Space)
            act_toggle_outline = QAction('Toggle Outline Only', self)
            act_toggle_outline.setShortcut(QKeySequence('Space'))
            act_toggle_outline.setToolTip('Toggle outline-only mode (Space)')
            def _toggle_outline():
                try:
                    self.outline_only_chk.setChecked(not self.outline_only_chk.isChecked())
                    self.update_visible_items(force=True)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            act_toggle_outline.triggered.connect(_toggle_outline)
            self.addAction(act_toggle_outline)

        except Exception:
            # If shortcuts or actions fail for any reason, continue without them
            pass

    # ---- Undo/Redo helpers ----
    def _deep_copy_meta(self, meta: dict) -> dict:
        """Return a deep copy of a meta dict using JSON round-trip for simplicity/robustness."""
        try:
            return json.loads(json.dumps(meta)) if isinstance(meta, dict) else {}
        except Exception:
            # Fallback shallow copy
            try:
                return dict(meta)
            except Exception:
                return {}

    def _get_undo_stack(self):
        """Retrieve the application's undo stack if available (from the main window)."""
        try:
            parent = getattr(self, 'parent', None) or getattr(self, 'parentWidget', lambda: None)()
            if parent is None:
                parent = getattr(self, 'parent', None)
            return getattr(parent, 'undo_stack', None)
        except Exception:
            return None

    def _apply_meta_mapping(self, mapping: dict[str, dict], affected: list[str]):
        """Apply provided meta mapping to self._state_meta, mark dirty, and refresh visuals/tooltips."""
        for sid in affected:
            try:
                self._state_meta[sid] = self._deep_copy_meta(mapping.get(sid, {}))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self._mark_states_dirty(affected)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self._refresh_states(affected)
        except Exception:
            try:
                # minimal visible update if refresh fails
                self.update_visible_items(force=True)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _push_state_meta_edit(self, before_map: dict[str, dict], after_map: dict[str, dict], affected: list[str], description: str):
        """Push a state-meta edit on the undo stack if available; otherwise apply immediately."""
        undo_stack = self._get_undo_stack()
        if undo_stack is None:
            # No undo stack; apply directly
            self._apply_meta_mapping(after_map, affected)
            return

        # Local QUndoCommand implementation bound to this dock
        dock = self
        class StateMetaEditCommand(QUndoCommand):
            def __init__(self, desc: str):
                super().__init__(desc)
                # store deep copies for safety
                self._before = {sid: dock._deep_copy_meta(before_map.get(sid, {})) for sid in affected}
                self._after = {sid: dock._deep_copy_meta(after_map.get(sid, {})) for sid in affected}
                self._affected = list(affected)

            def redo(self):
                try:
                    dock._apply_meta_mapping(self._after, self._affected)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            def undo(self):
                try:
                    dock._apply_meta_mapping(self._before, self._affected)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        undo_stack.push(StateMetaEditCommand(description))

    def _focus_appdata_root(self) -> str:
        # Use platform-conventional writable data locations.
        # Windows: LOCALAPPDATA/APPDATA\FocusTool
        # Linux:   XDG_DATA_HOME/focus_tool or ~/.local/share/focus_tool
        if sys.platform.startswith('win'):
            root = os.getenv('LOCALAPPDATA') or os.getenv('APPDATA') or os.path.expanduser('~')
            focus_root = os.path.join(root, 'FocusTool')
        else:
            xdg_data = os.getenv('XDG_DATA_HOME')
            if xdg_data:
                focus_root = os.path.join(xdg_data, 'focus_tool')
            else:
                focus_root = os.path.join(os.path.expanduser('~'), '.local', 'share', 'focus_tool')
        try:
            os.makedirs(focus_root, exist_ok=True)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return focus_root

    def _autoload_path(self) -> str:
        return os.path.join(self._focus_appdata_root(), AUTOLOAD_FILENAME)

    def _load_autoload_map_if_available(self):
        # Allow tests or env to disable autoload to avoid heavy import-time loading
        try:
            if os.environ.get('FOCUS_DISABLE_AUTOLOAD'):
                return
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        path = self._autoload_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.load_states_from_dict(data, source_path=path, quiet=True)
        except Exception as exc:
            QMessageBox.warning(self, 'Auto-load Failed', f'Could not auto-load startup map at\n{path}\nReason: {exc}')

    def _show_tag_legend(self):
        """Show a dialog with all unique owner TAGs and their corresponding colors"""
        # Collect all unique owner tags from loaded states
        tags = set()
        for meta in self._state_meta.values():
            owner = meta.get('owner')
            if owner:
                tags.add(owner)

        if not tags:
            QMessageBox.information(self, 'Color Legend', 'No owner TAGs found in loaded states.')
            return

        # Create dialog
        dialog = QDialog(self)
        dialog.setWindowTitle('Owner TAG Color Legend')
        dialog.setModal(False)  # Allow non-modal so users can keep it open while working
        layout = QVBoxLayout(dialog)

        info_label = QLabel('States are colored by their owner TAG using consistent hashing:')
        layout.addWidget(info_label)

        # Create a scroll area for the legend items
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)

        # Sort tags for consistent display
        for tag in sorted(tags):
            # Create a sample StatePolygonItem to get its color
            sample_item = StatePolygonItem('sample', QPolygonF(), owner_tag=tag)
            color = sample_item._tag_to_color(tag)

            # Create a color swatch + label
            row = QHBoxLayout()
            color_label = QLabel()
            color_label.setFixedSize(40, 20)
            color_label.setStyleSheet(f'background-color: rgb({color.red()}, {color.green()}, {color.blue()}); border: 1px solid black;')
            row.addWidget(color_label)

            tag_label = QLabel(tag)
            tag_label.setFixedWidth(100)
            row.addWidget(tag_label)

            # Count states with this owner
            count = sum(1 for meta in self._state_meta.values() if meta.get('owner') == tag)
            count_label = QLabel(f'({count} state{"s" if count != 1 else ""})')
            row.addWidget(count_label)
            row.addStretch()

            scroll_layout.addLayout(row)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)

        dialog.resize(350, 400)
        dialog.show()

    # ---- Inline progress helpers ----
    def _show_progress(self, message: str = '', value: int = 0):
        try:
            if getattr(self, '_progress_label', None) is not None:
                self._progress_label.setText(message)
                self._progress_label.setVisible(True)
            if getattr(self, '_progress_bar', None) is not None:
                self._progress_bar.setValue(value)
                self._progress_bar.setVisible(True)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _hide_progress(self):
        try:
            if getattr(self, '_progress_label', None) is not None:
                self._progress_label.setVisible(False)
                self._progress_label.setText('')
            if getattr(self, '_progress_bar', None) is not None:
                self._progress_bar.setVisible(False)
                self._progress_bar.setValue(0)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _set_progress(self, value: int):
        try:
            if getattr(self, '_progress_bar', None) is not None:
                self._progress_bar.setValue(int(value))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _set_progress_message(self, message: str):
        try:
            if getattr(self, '_progress_label', None) is not None:
                self._progress_label.setText(str(message))
                self._progress_label.setVisible(True)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _set_autoload_map_from_path(self, path: str):
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, 'Startup Map', 'Current map file is not accessible; cannot set startup map.')
            return
        dest = self._autoload_path()
        try:
            if os.path.abspath(path) != os.path.abspath(dest):
                shutil.copy2(path, dest)
            else:
                # ensure folder exists even if already using startup file
                os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                self._write_state_payload(dest)
            except Exception as exc:
                show_error(self, 'Startup Map Error', 'Failed to write updated map data.', exc=exc)
                return
            # Do NOT remove the original file after copying. Previously we
            # deleted the user-provided converted JSON which led to files
            # disappearing from project folders. Keep the source intact.
            # if os.path.abspath(path) != os.path.abspath(dest):
            #     with suppress(Exception):
            #         os.remove(path)
            self._current_states_path = dest
            self.autoload_btn.setEnabled(True)
            QMessageBox.information(self, 'Startup Map', f'Startup map set to:\n{dest}')
        except Exception as exc:
            show_error(self, 'Startup Map Error', 'Failed to set startup map.', exc=exc)

    def _set_startup_map_from_current(self):
        if not self._current_states_path:
            QMessageBox.information(self, 'Startup Map', 'Load or import a map before setting it as the startup map.')
            return
        self._set_autoload_map_from_path(self._current_states_path)

    def _prompt_set_startup_map(self, path: str):
        result = QMessageBox.question(
            self,
            'Use Map on Startup?',
            f'Do you want to rename this converted map to "{AUTOLOAD_FILENAME}"\n'
            'and load it automatically next time the application starts?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if result == QMessageBox.StandardButton.Yes:
            self._set_autoload_map_from_path(path)

    def load_states_dialog(self):
        fn, _ = QFileDialog.getOpenFileName(self, 'Load states JSON', '', 'JSON Files (*.json);;All Files (*)')
        if not fn:
            return
        try:
            # show inline loading indicator for larger files
            try:
                self._show_progress('Loading state JSON...', 5)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            with open(fn, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            show_error(self, 'Load Error', 'Failed to load state JSON.', exc=e)
            try:
                self._hide_progress()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            return
        try:
            self.load_states_from_dict(data, source_path=fn)
            try:
                # complete and hide
                self._set_progress(100)
                self._set_progress_message('Loaded')
                QTimer.singleShot(800, lambda: self._hide_progress())
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            show_error(self, 'Parse Error', 'Failed to parse state JSON.', exc=e)
            try:
                self._hide_progress()
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def load_hoi4_mod_dialog(self):
        """Prompt for a mod or game folder and attempt to autodetect map assets and build polygons."""
        folder = QFileDialog.getExistingDirectory(self, 'Select HOI4 mod or game folder')
        if not folder:
            return
        try:
            # Attempt to find provinces bitmap and definition csv
            candidates = [
                os.path.join(folder, 'map', 'provinces.bmp'),
                os.path.join(folder, 'map', 'provinces.png'),
                os.path.join(folder, 'map', 'provinces.png'),
                os.path.join(folder, 'map', 'definition.csv'),
            ]
            # allow selecting files manually if autodetect fails
            prov_img = None
            def_csv = None
            for root, dirs, files in os.walk(folder):
                for f in files:
                    if f.lower().startswith('provinces') and f.lower().endswith(('.bmp', '.png', '.tga')):
                        prov_img = os.path.join(root, f)
                    if f.lower() == 'definition.csv' or f.lower().endswith('definition.csv'):
                        def_csv = os.path.join(root, f)
                if prov_img and def_csv:
                    break

            if not prov_img or not def_csv:
                # ask user to supply both
                QMessageBox.information(self, 'HOI4 Import', 'Could not autodetect provinces image and definition.csv. Please select them manually.')
                prov_img, _ = QFileDialog.getOpenFileName(self, 'Select provinces image (provinces.bmp/png/tga)', folder, 'Image Files (*.bmp *.png *.tga);;All Files (*)')
                if not prov_img:
                    return
                def_csv, _ = QFileDialog.getOpenFileName(self, 'Select definition.csv', folder, 'CSV Files (*.csv);;All Files (*)')
                if not def_csv:
                    return

            # Copy files to an AppData temp folder so we can safely manipulate them
            try:
                # Reuse the same platform-aware app data root for import scratch space.
                focus_temp_root = self._focus_appdata_root()
                os.makedirs(focus_temp_root, exist_ok=True)
                # unique folder per import
                import uuid
                uid = uuid.uuid4().hex
                work_dir = os.path.join(focus_temp_root, f'import_{uid}')
                os.makedirs(work_dir, exist_ok=True)

                # copy provinces image and definition.csv
                prov_img_copy = os.path.join(work_dir, os.path.basename(prov_img))
                def_csv_copy = os.path.join(work_dir, os.path.basename(def_csv))
                try:
                    import shutil
                    shutil.copy2(prov_img, prov_img_copy)
                    shutil.copy2(def_csv, def_csv_copy)
                except Exception:
                    # fallback to simple open/write
                    with open(prov_img, 'rb') as r, open(prov_img_copy, 'wb') as w:
                        w.write(r.read())
                    with open(def_csv, 'rb') as r, open(def_csv_copy, 'wb') as w:
                        w.write(r.read())

                # Ask for one or more states files (allow multi-select)
                states_txts, _ = QFileDialog.getOpenFileNames(self, 'Select HOI4 states .txt file(s) (common/states/*.txt)', folder, 'Text Files (*.txt);;All Files (*)')
                if not states_txts:
                    return
                states_txt_copies = []
                for stf in states_txts:
                    dst = os.path.join(work_dir, os.path.basename(stf))
                    try:
                        shutil.copy2(stf, dst)
                    except Exception:
                        with open(stf, 'rb') as r, open(dst, 'wb') as w:
                            w.write(r.read())
                    states_txt_copies.append(dst)

                # Prepare output path and run conversion in background to keep UI responsive
                out_json = os.path.join(work_dir, f'states_{uid}.json')

                # Inform user where files will be written and open the working folder so they can inspect files
                try:
                    from PyQt6.QtGui import QDesktopServices
                    from PyQt6.QtCore import QUrl
                    QDesktopServices.openUrl(QUrl.fromLocalFile(work_dir))
                except Exception:
                    # if opening Explorer fails, still show an info dialog
                    QMessageBox.information(self, 'Import working folder', f'Working folder: {work_dir}\nOutput states file will be: {out_json}\nConversion will run in the background.')

                worker = ConversionWorker(prov_img_copy, def_csv_copy, states_txt_copies, out_json, self._extract_province_polygons_from_raster)
                thread = QThread(self)
                worker.moveToThread(thread)

                progress = QProgressDialog('Converting HOI4 mod...', 'Cancel', 0, 100, self)
                progress.setWindowModality(Qt.WindowModality.WindowModal)
                progress.setWindowTitle('Conversion Progress')
                progress.setMinimumDuration(200)

                # update modal dialog
                worker.progress.connect(progress.setValue)
                # show the output path in the initial label
                worker.message.connect(lambda s: progress.setLabelText(f"{s}\nOutput: {out_json}"))
                # also update inline dock-level progress bar and label
                worker.progress.connect(lambda v: self._set_progress(v))
                worker.message.connect(lambda s: self._set_progress_message(f"{s} Output: {out_json}"))

                def on_cancel():
                    worker.request_cancel()
                    progress.setLabelText('Cancelling...')
                    # reflect on dock
                    try:
                        self._set_progress_message('Cancelling...')
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

                progress.canceled.connect(on_cancel)

                def on_finished(success: bool, payload: str):
                    thread.quit()
                    thread.wait()
                    progress.hide()
                    # hide inline progress
                    try:
                        self._hide_progress()
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    if success:
                        try:
                            with open(payload, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                            self.load_states_from_dict(data, source_path=payload)
                            QMessageBox.information(self, 'HOI4 Import', f'Imported and saved converted state JSON to:\n{payload}')
                            self._prompt_set_startup_map(payload)
                        except Exception as e:
                            show_error(self, 'Load Error', 'Failed to load produced state JSON.', exc=e)
                    else:
                        QMessageBox.warning(self, 'Conversion failed', payload)

                    worker.deleteLater()

                worker.finished.connect(on_finished)
                thread.started.connect(worker.run)
                thread.start()

                # show inline progress in the dock as well
                try:
                    self._show_progress(f'Converting... Output: {out_json}')
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                progress.show()
            except Exception as e:
                show_error(self, 'Import Error', 'Failed to import HOI4 mod folder.', exc=e)
                return

        except Exception as e:
            show_error(self, 'Import Error', 'Failed to import HOI4 mod folder.', exc=e)

    def _extract_province_polygons_from_raster(self, provinces_image_path: str, definition_csv_path: str) -> dict:
        """Extracts province polygons from a provinces image and definition.csv.

        Returns mapping province_id -> list of polygons ([[[x,y],...], ...])
        Requires Pillow + numpy + opencv-python available. If not present, raises ImportError.
        """
        # Lazy imports with user-friendly errors
        try:
            from PIL import Image
        except Exception:
            raise ImportError('Pillow is required for raster->vector conversion')
        try:
            import numpy as np
        except Exception:
            raise ImportError('numpy is required for raster->vector conversion')
        try:
            import cv2
        except Exception:
            raise ImportError('opencv-python (cv2) is required for raster->vector conversion')

        # Robustly parse definition.csv and build a color->province id mapping.
        prov_color_to_id = {}
        prov_ids_seen = set()
        with open(definition_csv_path, 'r', encoding='utf-8', errors='ignore') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                # try splitting by common delimiters
                cols = [c.strip() for c in re.split(r'[;,\t]|\s+', line) if c.strip()]
                if not cols:
                    continue
                pid = cols[0]
                prov_ids_seen.add(str(pid))
                r = g = b = None
                # prefer the first 3 integer tokens after the id
                ints = [int(x) for x in cols[1:] if re.fullmatch(r'\d{1,3}', x)]
                if len(ints) >= 3:
                    r, g, b = ints[0], ints[1], ints[2]
                else:
                    # fallback: search for hex tokens like 0xRRGGBB or #RRGGBB anywhere
                    for p in cols[1:]:
                        m = re.match(r'^(?:0x|#)?([0-9a-fA-F]{6})$', p)
                        if m:
                            hexv = m.group(1)
                            r = int(hexv[0:2], 16)
                            g = int(hexv[2:4], 16)
                            b = int(hexv[4:6], 16)
                            break
                if r is not None:
                    # clamp and store
                    try:
                        rr, gg, bb = int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF
                        prov_color_to_id[(rr, gg, bb)] = str(pid)
                    except Exception:
                        # ignore malformed entries
                        continue

        img = Image.open(provinces_image_path).convert('RGBA')
        arr = np.array(img)
        h, w = arr.shape[:2]

        # Fast pipeline: convert RGB to uint32 color codes, map codes to province ids,
        # build a compact index image (per-pixel index into unique codes), then
        # iterate only over mapped province ids extracting contours per id.
        rgb_arr = arr[:, :, :3].astype(np.uint32)
        alpha = arr[:, :, 3]
        if rgb_arr.size == 0:
            raise ValueError('Provinces image appears empty')

        # build 32-bit color code per pixel: 0xRRGGBB
        code_image = (rgb_arr[:, :, 0] << 16) | (rgb_arr[:, :, 1] << 8) | (rgb_arr[:, :, 2])
        flat_codes = code_image.ravel()

        # Compute unique codes and an inverse mapping to rebuild index image
        unique_codes, inverse = np.unique(flat_codes, return_inverse=True)
        code_index_image = inverse.reshape(code_image.shape)

        # Build mapping from unique code -> province id index
        # Convert prov_color_to_id keys (r,g,b) to code ints
        colorcode_to_pid = {}
        for (r, g, b), pid in prov_color_to_id.items():
            code = (int(r) << 16) | (int(g) << 8) | int(b)
            colorcode_to_pid[code] = pid

        # Map unique_codes to pid_idx (0 = unmapped)
        pid_to_idx = {}
        pid_idx_counter = 1
        map_unique_to_pididx = np.zeros(len(unique_codes), dtype=np.int32)
        for i, code in enumerate(unique_codes):
            pid = colorcode_to_pid.get(int(code))
            if pid is None:
                # try BGR fallback
                r = (int(code) >> 16) & 0xFF
                g = (int(code) >> 8) & 0xFF
                b = int(code) & 0xFF
                bgr_code = (b << 16) | (g << 8) | r
                pid = colorcode_to_pid.get(int(bgr_code))
            if pid is not None:
                if pid not in pid_to_idx:
                    pid_to_idx[pid] = pid_idx_counter
                    pid_idx_counter += 1
                map_unique_to_pididx[i] = pid_to_idx[pid]

        # Build pid index image where each pixel has small integer representing province id index (0=unmapped)
        pid_index_image = map_unique_to_pididx[code_index_image]

        province_polys = {}

        entries = list(pid_to_idx.items())
        max_workers = max(1, min(32, (os.cpu_count() or 4)))

        def _extract_single(args):
            pid, idx = args
            coords = np.argwhere(pid_index_image == idx)
            if coords.size == 0:
                return pid, None
            min_r, min_c = coords.min(axis=0)
            max_r, max_c = coords.max(axis=0)
            sub_section = pid_index_image[min_r:max_r+1, min_c:max_c+1]
            mask = (sub_section == idx).astype(np.uint8) * 255
            contours_data = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if len(contours_data) == 3:
                _, contours, _ = contours_data
            else:
                contours, _ = contours_data
            polys = []
            for cnt in contours:
                if cnt is None or len(cnt) < 3:
                    continue
                pts = cnt.reshape(-1, 2)
                if pts.size == 0:
                    continue
                pts = pts + np.array([[min_c, min_r]], dtype=pts.dtype)
                pts_list = pts.tolist()
                if len(pts_list) < 3:
                    continue
                poly = [[float(xy[0]), float(xy[1])] for xy in pts_list]
                polys.append(poly)
            return pid, polys if polys else None

        if entries:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for pid, polys in executor.map(_extract_single, entries):
                    if polys:
                        province_polys[pid] = polys

        if not province_polys:
            sample = [((int(c) >> 16) & 0xFF, (int(c) >> 8) & 0xFF, int(c) & 0xFF) for c in unique_codes[:20]]
            raise ValueError(f'No province polygons extracted - check definition.csv mapping and image format.\nFound unique colors (sample): {sample}\nDefined provinces in CSV: {len(prov_ids_seen)}')

        return province_polys

    def load_states_from_dict(self, data: dict, source_path: Optional[str] = None, quiet: bool = False):
        """Load states from a parsed JSON dict. Clears any previous data."""
        self.scene.clear()
        self.state_items.clear()
        self.province_items.clear()
        self._state_meta.clear()
        self._dirty_states.clear()
        self._states_needing_refresh.clear()
        self._spatial_index.clear()  # Clear spatial index
        self._current_states_path = source_path
        self._map_loaded = True
        self.autoload_btn.setEnabled(bool(source_path))

        provinces = data.get('provinces') or {}
        if isinstance(provinces, dict):
            # Handle both formats:
            # 1. pid -> [polygon_list] (new correct format)
            # 2. pid -> {'polygons': [polygon_list]} (old object format)
            self._province_polys = {}
            for pid, value in provinces.items():
                if isinstance(value, list):
                    # New format: direct list of polygons
                    self._province_polys[str(pid)] = [
                        [[float(pt[0]), float(pt[1])] for pt in poly]
                        for poly in value
                    ]
                elif isinstance(value, dict) and 'polygons' in value:
                    # Old format: object with 'polygons' key
                    polys = value['polygons']
                    if isinstance(polys, list):
                        self._province_polys[str(pid)] = [
                            [[float(pt[0]), float(pt[1])] for pt in poly]
                            for poly in polys
                        ]
        else:
            self._province_polys = {}

        # We'll store raw polygons on disk in memory but lazily create QGraphicsItems
        # only for items intersecting the view. Each state entry keeps a list of
        # polygons (list of points) and a cached bounding rect for quick culling.
        states = data.get('states', {})

        # REMOVED SYNTHESIS: If no states are provided, the user needs to load proper
        # state definitions. Creating one pseudo-state per province (5000+ states)
        # is not a valid workaround and conflates provinces with states.
        if not states or len(states) == 0:
            if not quiet:
                try:
                    from PyQt6.QtWidgets import QMessageBox
                    QMessageBox.warning(
                        self,
                        'No States Defined',
                        f'The loaded data contains {len(self._province_polys)} provinces but no state definitions.\n\n'
                        'States are administrative groupings of provinces. To see the map properly:\n'
                        '1. Use "Load HOI4 Mod..." to load both provinces AND state files\n'
                        '2. Or provide a JSON file with both "provinces" and "states" mappings\n\n'
                        'The viewport cannot render without state groupings.'
                    )
                except Exception:
                    logger.warning('[StateViewport] provinces loaded but no states defined count=%s', len(self._province_polys))
            return

        for sid, meta in states.items():
            name = meta.get('name', '')
            # Prefer to reconstruct state polygons from the top-level provinces
            # mapping when it's available. Some converter outputs include both
            # 'states' and a 'provinces' map; using the provinces ensures that
            # a state's polygons are the union of its provinces rather than
            # accidentally using pre-broken per-province polygons present in
            # the 'polygons' field.
            provinces = meta.get('provinces', []) or []
            if self._province_polys and provinces:
                polygons = self._collect_polygons_for_provinces(provinces)
            else:
                polygons = meta.get('polygons', []) or []
            manpower = meta.get('manpower')
            state_category = meta.get('state_category')
            owner = meta.get('owner')
            cores = meta.get('cores', []) or []
            claims = meta.get('claims', []) or []
            resources = meta.get('resources', {}) or {}
            res_map = {}
            if isinstance(resources, dict):
                for r_key, r_val in resources.items():
                    try:
                        res_map[r_key] = int(float(r_val))
                    except Exception:
                        continue
            rect = self._compute_bounds(polygons)
            self._state_meta[sid] = {
                'name': name,
                'provinces': [str(p) for p in provinces],
                'polygons': polygons,
                'bounds': rect,
                'manpower': manpower,
                'state_category': state_category,
                'owner': owner,
                'cores': list(cores),
                'claims': list(claims),
                'resources': res_map,
                '_dirty': False,
            }
            # Populate spatial index for optimized viewport culling
            if rect:
                self._spatial_index[sid] = rect
            # no graphics items yet; state_items will be populated on demand
            self.state_items[sid] = []

        # initial culling/creation
        # Debug: surface counts to console to help diagnose blank-load issues
        try:
            logger.info("[StateViewport] Loaded %s states; spatial_index size=%s; province_polys size=%s", len(self._state_meta), len(self._spatial_index), len(self._province_polys))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        # Detailed diagnostic sample: report first few states' polygon/province counts
        try:
            sample_keys = list(self._state_meta.keys())[:10]
            single_prov_states = 0
            multi_poly_states = 0
            for sid in sample_keys:
                meta = self._state_meta.get(sid, {})
                pcount = len(meta.get('provinces') or [])
                polycount = len(meta.get('polygons') or [])
                if pcount == 1:
                    single_prov_states += 1
                if polycount > 1:
                    multi_poly_states += 1
                try:
                    logger.debug("[StateViewport] Sample state %s: provinces=%s, polygons=%s", sid, pcount, polycount)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # summary across all loaded states
            try:
                total = len(self._state_meta)
                if total:
                    # compute approximate percent that are single-province (cheap pass)
                    single_est = sum(1 for m in self._state_meta.values() if len(m.get('provinces') or []) == 1)
                    logger.info("[StateViewport] Summary: total_states=%s, single_province_states=%s (%.1f%%)", total, single_est, (single_est/total*100) if total else 0.0)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            # If we've built a spatial index of state bounds, fit the view to the
            # union of those bounds. Relying on scene.itemsBoundingRect() is
            # unreliable here because no QGraphicsItems exist yet (they are
            # created lazily in update_visible_items). Use the spatial index
            # so the view is centered/zoomed to the loaded map immediately.
            union_rect = None
            for r in self._spatial_index.values():
                try:
                    if union_rect is None:
                        union_rect = QRectF(r)
                    else:
                        union_rect = union_rect.united(r)
                except Exception:
                    continue

            if union_rect is not None and not union_rect.isNull():
                # Slightly expand to provide margin
                try:
                    expanded = QRectF(union_rect.adjusted(-20, -20, 20, 20))
                    self.scene.setSceneRect(expanded)
                    self.view.fitInView(expanded, Qt.AspectRatioMode.KeepAspectRatio)
                except Exception:
                    # Fallback to scene bounds if anything goes wrong
                    try:
                        self.view.fitInView(self.scene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            else:
                # No spatial index (no bounds) — fall back to scene's bounding rect
                try:
                    self.view.fitInView(self.scene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

            # Create visible items now that the view transform is positioned over the map
            self.update_visible_items(force=True)

        # Finalize: ensure the view shows the map and repaint
        try:
            # After creating items, refit to any real items bounding rect for a tighter view
            try:
                items_rect = self.scene.itemsBoundingRect()
                if not items_rect.isNull():
                    self.view.fitInView(items_rect, Qt.AspectRatioMode.KeepAspectRatio)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.scene.update()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        try:
            self.view.viewport().update()
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        if quiet:
            self._update_operations_state([])
        else:
            self.state_selection_changed.emit([])
        # ensure zoom label reflects initial transform after loading
        try:
            QTimer.singleShot(0, lambda: self._update_view_zoom())
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _update_view_zoom(self):
        """Update the dock-local zoom percentage label based on the view transform."""
        try:
            t = self.view.transform()
            # m11 is horizontal scale; m22 is vertical. Use m11 for zoom percentage.
            scale = getattr(t, 'm11', None)
            if callable(scale):
                s = t.m11()
            else:
                # fallback: try to access as attribute
                s = float(t.m11)
            percent = int(round(s * 100))
            if hasattr(self, 'view_zoom_label'):
                try:
                    self.view_zoom_label.setText(f"{percent}%")
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception:
            try:
                # fallback: show 100% if label exists
                if hasattr(self, 'view_zoom_label'):
                    try:
                        self.view_zoom_label.setText('100%')
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _on_scene_selection_changed(self):
        # build list of selected state ids (unique)
        sel = set()
        for it in self.scene.selectedItems():
            sid = getattr(it, 'state_id', None)
            if sid:
                sel.add(sid)
        self.state_selection_changed.emit(list(sel))

    def get_selected_states(self) -> list[str]:
        return [sid for sid in {getattr(it, 'state_id', None) for it in self.scene.selectedItems()} if sid]

    def _collect_polygons_for_provinces(self, provinces) -> list:
        result = []
        if not provinces:
            return result
        for pid in provinces:
            pid_str = str(pid)
            polys = self._province_polys.get(pid_str)
            if not polys:
                continue
            for poly in polys:
                try:
                    result.append([[float(pt[0]), float(pt[1])] for pt in poly])
                except Exception:
                    cleaned = []
                    for pt in poly:
                        try:
                            cleaned.append([float(pt[0]), float(pt[1])])
                        except Exception:
                            continue
                    if len(cleaned) >= 3:
                        result.append(cleaned)
        return result

    def _compute_bounds(self, polygons: list) -> Optional[QRectF]:
        rect = None
        for poly_points in polygons:
            if not poly_points:
                continue
            xs = [float(p[0]) for p in poly_points]
            ys = [float(p[1]) for p in poly_points]
            if xs and ys:
                b = QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
                rect = b if rect is None else rect.united(b)
        return rect

    def _build_state_tooltip(self, sid: str, meta: dict) -> str:
        lines = [f"{sid}: {meta.get('name', '')}"]
        provs = meta.get('provinces') or []
        lines.append(f"Provinces: {len(provs)}")
        owner = meta.get('owner')
        if owner:
            lines.append(f"Owner: {owner}")
        manpower = meta.get('manpower')
        if manpower is not None:
            lines.append(f"Manpower: {manpower}")
        category = meta.get('state_category')
        if category:
            lines.append(f"Category: {category}")
        resources = meta.get('resources') or {}
        if resources:
            res_parts = [f"{k}={v}" for k, v in resources.items() if v]
            if res_parts:
                lines.append('Resources: ' + ', '.join(res_parts))
        cores = meta.get('cores') or []
        if cores:
            lines.append('Cores: ' + ', '.join(cores))
        return '\n'.join(lines)

    def _update_state_tooltips(self, states: list[str]):
        for sid in states:
            meta = self._state_meta.get(sid)
            if not meta:
                continue
            tooltip = self._build_state_tooltip(sid, meta)
            for item in self.state_items.get(sid, []):
                try:
                    item.setToolTip(tooltip)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _refresh_states(self, states: list[str]):
        changed = []
        for sid in states:
            meta = self._state_meta.get(sid)
            if meta is None:
                continue
            if self._province_polys:
                meta['polygons'] = self._collect_polygons_for_provinces(meta.get('provinces'))
            meta['bounds'] = self._compute_bounds(meta.get('polygons', []))
            items = self.state_items.get(sid, [])
            for it in items:
                try:
                    self.scene.removeItem(it)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            self.state_items[sid] = []
            self._states_needing_refresh.add(sid)
            changed.append(sid)
        if changed:
            self.update_visible_items()
            self._update_state_tooltips(changed)
            # Persist changes to project file if available
            try:
                parent = getattr(self, 'parent', None) or getattr(self, 'parentWidget', lambda: None)()
                if parent is None:
                    parent = getattr(self, 'parent', None)
                current_path = None
                try:
                    current_path = getattr(parent, 'current_project_path', None)
                except Exception:
                    current_path = None
                if current_path and os.path.isfile(current_path):
                    try:
                        self._persist_states_to_project(current_path)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _select_states(self, states: list[str]):
        try:
            self.scene.blockSignals(True)
            for it in list(self.scene.selectedItems()):
                try:
                    it.setSelected(False)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            for sid in states:
                for it in self.state_items.get(sid, []):
                    try:
                        it.setSelected(True)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        finally:
            self.scene.blockSignals(False)
        self.state_selection_changed.emit(states)

    def _serialize_state_payload(self) -> dict:
        states_payload = {}
        for sid, meta in self._state_meta.items():
            entry = {
                'name': meta.get('name', ''),
                'polygons': meta.get('polygons', []),
                'provinces': list(meta.get('provinces', [])),
            }
            if meta.get('manpower') is not None:
                entry['manpower'] = meta.get('manpower')
            if meta.get('state_category'):
                entry['state_category'] = meta.get('state_category')
            if meta.get('owner'):
                entry['owner'] = meta.get('owner')
            cores = [str(c) for c in meta.get('cores', []) if c]
            if cores:
                entry['cores'] = cores
            claims = [str(c) for c in meta.get('claims', []) if c]
            if claims:
                entry['claims'] = claims
            resources = meta.get('resources') or {}
            if resources:
                entry['resources'] = dict(resources)
            states_payload[str(sid)] = entry
        payload = {'states': states_payload}
        if self._province_polys:
            payload['provinces'] = self._province_polys
        return payload

    def _write_state_payload(self, path: str):
        data = self._serialize_state_payload()
        tmp = path + '.tmp'
        try:
            _write_state_sidecar(path, data)
        except Exception:
            # Fallback: write whole JSON normally
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, path)
            except Exception:
                try:
                    shutil.copy2(tmp, path)
                    os.remove(tmp)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _persist_states_to_project(self, project_path: str):
        """Merge current states/provinces into the given project JSON file.

        Writes the 'states' and optional 'provinces' keys into the project file
        atomically so edits persist across app restarts.
        """
        try:
            if not project_path or not os.path.isfile(project_path):
                return
            try:
                with open(project_path, 'r', encoding='utf-8') as f:
                    proj = json.load(f)
            except Exception:
                return
            if not isinstance(proj, dict):
                return
            state_payload = self._serialize_state_payload()
            if not isinstance(state_payload, dict):
                return
            # Merge state keys into project dict
            for k, v in state_payload.items():
                try:
                    proj[k] = v
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            # atomic write
            tmp = project_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(proj, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, project_path)
            except Exception:
                try:
                    shutil.copy2(tmp, project_path)
                    os.remove(tmp)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def update_visible_items(self, force: bool=False):
        """Create QGraphics items only for states whose bounds intersect the view rect.

        This reduces rendering and memory overhead for world maps.
        Optimized with spatial indexing for faster culling.
        """
        if force:
            self._states_needing_refresh.update(self._state_meta.keys())
            self._item_cache_generation += 1

        try:
            view_rect = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        except Exception:
            view_rect = None

        # Fast path: if view_rect is None, skip intersection tests
        if view_rect is None:
            return

        # Expand view rect slightly to avoid pop-in at edges
        margin = 50  # pixels in scene coordinates
        expanded_rect = view_rect.adjusted(-margin, -margin, margin, margin)

        # Use spatial index for faster culling
        visible_state_ids = set()
        # If spatial index is empty (e.g., bounds couldn't be computed), fall
        # back to marking all loaded states as visible so items are created.
        if not self._spatial_index:
            visible_state_ids = set(self._state_meta.keys())
        else:
            for sid, bounds in self._spatial_index.items():
                if bounds and expanded_rect.intersects(bounds):
                    visible_state_ids.add(sid)

        # Process visible states
        for sid in visible_state_ids:
            meta = self._state_meta.get(sid)
            if not meta:
                continue

            currently = self.state_items.get(sid, [])
            needs_refresh = sid in self._states_needing_refresh

            if needs_refresh and currently:
                # Remove old state boundary items
                for it in currently:
                    try:
                        self.scene.removeItem(it)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.state_items[sid] = []

                # Also remove old province items for this state
                for prov_id in meta.get('provinces', []) or []:
                    prov_id_str = str(prov_id)
                    prov_items = self.province_items.get(prov_id_str, [])
                    for pit in prov_items:
                        try:
                            self.scene.removeItem(pit)
                        except Exception as e:
                            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                    self.province_items[prov_id_str] = []

                currently = []
                self._states_needing_refresh.discard(sid)

            if not currently:
                # Create items (with optimization: reuse cached simplified polygons)
                items = []
                owner = meta.get('owner')
                provinces_list = meta.get('provinces', []) or []

                # Render PROVINCES as filled polygons (the actual map detail)
                if not (getattr(self, 'outline_only_chk', None) is not None and self.outline_only_chk.isChecked()):
                    # Normal mode: render individual province polygons
                    for prov_id in provinces_list:
                        prov_id_str = str(prov_id)
                        prov_polys = self._province_polys.get(prov_id_str, [])
                        for poly_points in prov_polys:
                            simp = _rdp_simplify([[float(x), float(y)] for x, y in poly_points], epsilon=0.5)
                            if len(simp) < 3:
                                continue
                            qpoly = QPolygonF()
                            for x, y in simp:
                                qpoly.append(QPointF(float(x), float(y)))
                            # Create province item with TAG-based coloring
                            prov_item = ProvincePolygonItem(prov_id_str, qpoly, state_id=sid, owner_tag=owner)
                            prov_item.setToolTip(f"Province {prov_id_str}\nState: {sid} - {meta.get('name', '')}")
                            self.scene.addItem(prov_item)
                            # Track province items separately
                            if prov_id_str not in self.province_items:
                                self.province_items[prov_id_str] = []
                            self.province_items[prov_id_str].append(prov_item)

                # Render STATE BOUNDARY as thick outline (collection of provinces)
                if getattr(self, 'outline_only_chk', None) is not None and self.outline_only_chk.isChecked():
                    # Outline-only mode: show convex hull of state
                    all_pts = []
                    for poly_points in meta.get('polygons', []):
                        try:
                            for x, y in poly_points:
                                all_pts.append([float(x), float(y)])
                        except Exception:
                            continue
                    if len(all_pts) >= 3:
                        hull = _convex_hull(all_pts)
                        simp = _rdp_simplify(hull, epsilon=1.0)
                        if len(simp) >= 3:
                            qpoly = QPolygonF()
                            for x, y in simp:
                                qpoly.append(QPointF(float(x), float(y)))
                            item = StatePolygonItem(sid, qpoly, owner_tag=owner)
                            item.setToolTip(self._build_state_tooltip(sid, meta))
                            self.scene.addItem(item)
                            items.append(item)
                else:
                    # Normal mode: draw state boundary as union of province boundaries
                    # Compute outer boundary from all province polygons
                    all_pts = []
                    for poly_points in meta.get('polygons', []):
                        try:
                            for x, y in poly_points:
                                all_pts.append([float(x), float(y)])
                        except Exception:
                            continue
                    if len(all_pts) >= 3:
                        # Use convex hull for state boundary
                        hull = _convex_hull(all_pts)
                        simp = _rdp_simplify(hull, epsilon=2.0)
                        if len(simp) >= 3:
                            qpoly = QPolygonF()
                            for x, y in simp:
                                qpoly.append(QPointF(float(x), float(y)))
                            item = StatePolygonItem(sid, qpoly, owner_tag=owner)
                            item.setToolTip(self._build_state_tooltip(sid, meta))
                            self.scene.addItem(item)
                            items.append(item)

                self.state_items[sid] = items

        # Remove items for states now outside the view
        for sid in list(self.state_items.keys()):
            if sid not in visible_state_ids:
                items = self.state_items.get(sid, [])
                for it in items:
                    try:
                        self.scene.removeItem(it)
                    except Exception as e:
                        handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                self.state_items[sid] = []

                # Also remove associated province items
                meta = self._state_meta.get(sid)
                if meta:
                    for prov_id in meta.get('provinces', []) or []:
                        prov_id_str = str(prov_id)
                        prov_items = self.province_items.get(prov_id_str, [])
                        for pit in prov_items:
                            try:
                                self.scene.removeItem(pit)
                            except Exception as e:
                                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                        if prov_id_str in self.province_items:
                            del self.province_items[prov_id_str]

        # optionally emit selection change if needed
        self._on_scene_selection_changed()

    def clear_selection(self):
        try:
            for it in list(self.scene.selectedItems()):
                it.setSelected(False)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.state_selection_changed.emit([])

    def select_all(self):
        try:
            for sid, items in self.state_items.items():
                for it in items:
                    it.setSelected(True)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        self.state_selection_changed.emit(list(self.state_items.keys()))

    def _open_context_menu(self, pos):
        # Map view pos to scene pos, find topmost item under cursor
        scene_pt = self.view.mapToScene(pos)
        items = self.scene.items(scene_pt)

        # IMPORTANT: Get current selection FIRST and preserve it
        selected_ids = self.get_selected_states()

        # If nothing is selected but the user right-clicked on a state,
        # add that state to the selection (not replace)
        if not selected_ids:
            if items:
                item = items[0]
                sid = getattr(item, 'state_id', None)
                if sid:
                    # Select the clicked state and continue
                    self._select_single(sid)
                    selected_ids = [sid]
            if not selected_ids:
                # nothing to act on
                return
        else:
            # Selection exists: check if clicked item is part of selection
            # If not, add it to selection (advanced users can build up selections)
            if items:
                item = items[0]
                sid = getattr(item, 'state_id', None)
                if sid and sid not in selected_ids:
                    # Add clicked state to existing selection
                    for it in self.state_items.get(sid, []):
                        it.setSelected(True)
                    selected_ids.append(sid)

        menu = QMenu(self.view)

        # Show selection count in menu for batch operations
        selection_info = f"Selection: {len(selected_ids)} state(s)"
        info_action = menu.addAction(selection_info)
        info_action.setEnabled(False)  # Just informational

        # Optional quick focus for clicked state
        if items:
            item0 = items[0]
            sid0 = getattr(item0, 'state_id', None)
            if sid0:
                menu.addAction(f"Focus on State {sid0}", lambda sid=sid0: self._center_on_state(sid))
                menu.addSeparator()

        # Selection utilities
        menu.addAction('Clear Selection', self.clear_selection)
        menu.addAction('Select All', self.select_all)
        menu.addSeparator()

        # # Batch Transfer submenu for advanced users (only show if multiple states selected)
        # if len(selected_ids) > 1:
        #     # batch_menu = menu.addMenu(f'Batch Transfer ({len(selected_ids)} states)')
        #     # batch_menu.setToolTip('Advanced: Multi-state operations (tags and metadata)')
        #     # Repurposed: perform owner/core TAG transfers in batch
        #     # batch_menu.addAction('Batch Tag Transfer (Owner/Core)…', self._op_group_tag_transfer)
        #     # batch_menu.addAction('Batch Metadata Copy...', self._op_batch_metadata_copy)
        #     batch_menu.addSeparator()
        #     # Advanced group transfer (original dialog with provinces/metadata)
        #     # batch_menu.addAction('Advanced Group Transfer (Provinces/Metadata)…', self._op_group_transfer)
        #     menu.addSeparator()

        ops_menu = menu.addMenu('State Operations')
        ops_menu.setEnabled(True)

        # Conditional menu items based on selection count
        if len(selected_ids) == 1:
            ops_menu.addAction('Quick Transfer…', self._op_quick_transfer)
        if len(selected_ids) >= 2:
            # New simpler tag-focused group transfer
            ops_menu.addAction('Group Transfer (Tags)…', self._op_group_tag_transfer)
            # Advanced (original) dialog with provinces/metadata options
            ops_menu.addAction('Advanced Group Transfer (Provinces/Metadata)…', self._op_group_transfer)

        ops_menu.addSeparator()

        # Core-related actions grouped
        core_menu = ops_menu.addMenu('Core/Coring')
        core_menu.addAction('Add Core...', self._op_add_core)
        core_menu.addAction('Remove Core...', self._op_remove_core)
        core_menu.addAction('Remove All Core(s)', self._op_remove_all_cores)
        core_menu.addSeparator()
        core_menu.addAction('Find Multi-core States...', self._op_find_multi_core_states)
        core_menu.addAction('Batch Replace Core TAG...', self._op_batch_replace_core)

        # Manpower adjustments grouped
        inc_menu = ops_menu.addMenu('Increase Manpower')
        for factor in (2, 5, 10, 100):
            inc_menu.addAction(f'x{factor}', lambda checked=False, f=factor: self._op_increase_manpower(f))

        ops_menu.addSeparator()
        ops_menu.addAction('Change State Category...', self._op_change_state_category)
        ops_menu.addAction('Adjust Resources...', self._op_adjust_resources)

        # Export submenu for dirty states
        export_menu = menu.addMenu('Export States')
        export_menu.addAction('Export Selected (HOI4 .txt)...', self._op_export_selected)
        export_menu.addAction('Export Dirty States (HOI4 .txt)...', self._op_export_dirty)
        export_menu.addAction('Export All States (HOI4 .txt)...', self._op_export_all)

        menu.exec(self.view.mapToGlobal(pos))

    def _select_single(self, state_id: str):
        self.clear_selection()
        for it in self.state_items.get(state_id, []):
            it.setSelected(True)
        self.state_selection_changed.emit([state_id])

    def _center_on_state(self, state_id: str):
        items = self.state_items.get(state_id, [])
        if not items:
            return
        rect = None
        for it in items:
            if rect is None:
                rect = it.boundingRect().translated(it.pos())
            else:
                rect = rect.united(it.boundingRect().translated(it.pos()))
        if rect is not None:
            self.view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def _update_operations_state(self, selected_ids: list[str]):
        has_selection = bool(selected_ids)
        try:
            self.clear_btn.setEnabled(has_selection)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _ensure_selection(self) -> Optional[list[str]]:
        sel = self.get_selected_states()
        if not sel:
            QMessageBox.information(self, 'State Operations', 'Please select at least one state to continue.')
            return None
        return sel

    def _find_multi_core_states(self, scope: str = 'loaded') -> dict:
        """Return a dict of state_id -> list_of_cores for states that have multiple core TAGs.

        scope: 'loaded' to scan only currently loaded states, 'all' to scan all states in _state_meta.
        """
        result = {}
        try:
            items = self._state_meta.items() if scope == 'all' else ((k, v) for k, v in self._state_meta.items() if k in self.state_items)
            for sid, meta in items:
                cores = meta.get('cores') or []
                if isinstance(cores, list) and len(cores) > 1:
                    result[sid] = list(cores)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return result

    def _op_find_multi_core_states(self):
        """Context-menu action: show multi-core states with counts and allow quick navigation."""
        try:
            multi = self._find_multi_core_states(scope='loaded')
            if not multi:
                QMessageBox.information(self, 'Multi-core States', 'No states with multiple cores were found in the loaded map.')
                return

            dialog = QDialog(self)
            dialog.setWindowTitle('Multi-core States')
            layout = QVBoxLayout(dialog)

            info = QLabel(f'Found {len(multi)} multi-core state(s). Click an entry to center on it.')
            layout.addWidget(info)

            list_widget = QListWidget()
            for sid in sorted(multi.keys(), key=lambda x: int(x) if str(x).isdigit() else x):
                cores = ', '.join(multi[sid])
                item = QListWidgetItem(f"{sid}: {self._state_meta.get(sid, {}).get('name','')} — cores: {cores}")
                item.setData(Qt.ItemDataRole.UserRole, sid)
                list_widget.addItem(item)
            layout.addWidget(list_widget)

            btn_row = QHBoxLayout()
            center_btn = QPushButton('Center on Selected')
            def _center():
                it = list_widget.currentItem()
                if not it:
                    return
                sid = it.data(Qt.ItemDataRole.UserRole)
                if sid:
                    self._center_on_state(sid)
            center_btn.clicked.connect(_center)
            btn_row.addWidget(center_btn)

            close_btn = QPushButton('Close')
            close_btn.clicked.connect(dialog.accept)
            btn_row.addWidget(close_btn)
            layout.addLayout(btn_row)

            dialog.resize(600, 400)
            dialog.exec()
        except Exception as exc:
            show_error(self, 'Multi-core Check Error', 'Failed to check multi-core states.', exc=exc)

    def _op_batch_replace_core(self):
        """Prompt user for FROM core TAG(s) and TO core TAG and perform replacement across selected or all multi-core states.

        Allows selecting multiple FROM cores to remove/replace in a state; uses imported country tags (if any) as suggestions for the TO tag.
        """
        try:
            # Ask whether to operate on selection or all loaded states
            choice, ok = QInputDialog.getItem(self, 'Scope', 'Operate on:', ['Selected states', 'Loaded map (all states)'], 0, False)
            if not ok or not choice:
                return
            scope = 'selection' if choice.startswith('Selected') else 'loaded'

            # Determine target state set
            if scope == 'selection':
                sel = self._ensure_selection()
                if sel is None:
                    return
                target_states = {s: self._state_meta.get(s, {}) for s in sel}
            else:
                target_states = dict(self._state_meta)

            # Gather all core tags available in target set and identify multi-core states
            tags = set()
            multi_states = {}
            for sid, meta in target_states.items():
                cores = meta.get('cores') or []
                if isinstance(cores, list) and cores:
                    for c in cores:
                        tags.add(c)
                    if len(cores) > 1:
                        multi_states[sid] = list(cores)

            if not tags:
                QMessageBox.information(self, 'Batch Replace Cores', 'No core TAGs found in the target states.')
                return

            # Ask FROM tag(s) - allow multi-select via a dialog list
            from_dialog = QDialog(self)
            from_dialog.setWindowTitle('Replace Core TAG(s) - From')
            fd_layout = QVBoxLayout(from_dialog)
            fd_layout.addWidget(QLabel('Select one or more core TAGs to replace (states containing any of these will be affected):'))
            from_list = QListWidget()
            from_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            for t in sorted(tags):
                it = QListWidgetItem(t)
                it.setData(Qt.ItemDataRole.UserRole, t)
                from_list.addItem(it)
            fd_layout.addWidget(from_list)
            fbtns = QHBoxLayout()
            ok_btn = QPushButton('OK')
            cancel_btn = QPushButton('Cancel')
            ok_btn.clicked.connect(from_dialog.accept)
            cancel_btn.clicked.connect(from_dialog.reject)
            fbtns.addWidget(ok_btn); fbtns.addWidget(cancel_btn)
            fd_layout.addLayout(fbtns)
            if from_dialog.exec() != QDialog.DialogCode.Accepted:
                return
            sel_items = from_list.selectedItems()
            if not sel_items:
                QMessageBox.information(self, 'Batch Replace Cores', 'Please select at least one core TAG to replace.')
                return
            from_tags = [it.data(Qt.ItemDataRole.UserRole) for it in sel_items]
            # Ask TO tag. If we have imported country tags, present them in a combo editable control
            if self._country_tags:
                to_dialog = QDialog(self)
                to_dialog.setWindowTitle('Replace Core TAG - To')
                td_layout = QVBoxLayout(to_dialog)
                td_layout.addWidget(QLabel(f'Replace occurrences of {", ".join(from_tags)} with:'))
                to_combo = QComboBox()
                to_combo.setEditable(True)
                to_combo.addItems(sorted(self._country_tags))
                td_layout.addWidget(to_combo)
                tbtns = QHBoxLayout()
                ok_btn = QPushButton('OK')
                cancel_btn = QPushButton('Cancel')
                ok_btn.clicked.connect(to_dialog.accept)
                cancel_btn.clicked.connect(to_dialog.reject)
                tbtns.addWidget(ok_btn); tbtns.addWidget(cancel_btn)
                td_layout.addLayout(tbtns)
                if to_dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                to_tag = str(to_combo.currentText()).strip().upper()
            else:
                to_tag, ok = QInputDialog.getText(self, 'Replace Core TAG', f'Replace occurrences of {", ".join(from_tags)} with:')
                if not ok or not to_tag:
                    return
                to_tag = to_tag.strip().upper()

            # Confirm operation and show preview count
            # Count affected states: any multi-core state containing any from_tag
            affected = 0
            for sid, cores in multi_states.items():
                if any(ft in cores for ft in from_tags):
                    affected += 1

            if affected == 0:
                QMessageBox.information(self, 'Batch Replace Cores', f'No occurrences of "{", ".join(from_tags)}" found in multi-core states to replace.')
                return

            ans = QMessageBox.question(self, 'Confirm Replace', f'Replace core(s) "{", ".join(from_tags)}" with "{to_tag}" in {affected} multi-core state(s)?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return

            # Build undoable before/after maps
            before_map: dict[str, dict] = {}
            after_map: dict[str, dict] = {}
            changed_states: list[str] = []
            for sid, cores in multi_states.items():
                if any(ft in cores for ft in from_tags):
                    meta = self._state_meta.get(sid, {})
                    before_map[sid] = self._deep_copy_meta(meta)
                    # Replace any matching tags from the selected set
                    new_cores = [to_tag if c in from_tags else c for c in list(meta.get('cores') or [])]
                    # Deduplicate while preserving order
                    seen = set()
                    deduped = []
                    for c in new_cores:
                        if c not in seen:
                            seen.add(c)
                            deduped.append(c)
                    new_meta = self._deep_copy_meta(meta)
                    new_meta['cores'] = deduped
                    after_map[sid] = new_meta
                    changed_states.append(sid)

            if changed_states:
                self._push_state_meta_edit(before_map, after_map, changed_states, f'Batch Replace Core(s) → {to_tag}')

            QMessageBox.information(self, 'Batch Replace Cores', f'Replaced core(s) "{", ".join(from_tags)}" → "{to_tag}" in {len(changed_states)} state(s).')
        except Exception as exc:
            show_error(self, 'Batch Replace Error', 'Failed to perform batch core replacement.', exc=exc)

    def _import_country_tags_dialog(self):
        """Import a plain text file (one TAG per line) to populate TAG suggestions for replacement."""
        try:
            fn, _ = QFileDialog.getOpenFileName(self, 'Import country_tags.txt', '', 'Text Files (*.txt);;All Files (*)')
            if not fn:
                return
            tags = []
            try:
                with open(fn, 'r', encoding='utf-8') as f:
                    for ln in f:
                        t = ln.strip()
                        if not t:
                            continue
                        # Normalize common formats: allow 'TAG' or 'TAG = Name'
                        if '=' in t:
                            t = t.split('=', 1)[0].strip()
                        t = t.upper()
                        if t and t not in tags:
                            tags.append(t)
            except Exception as e:
                show_error(self, 'Import Error', f'Failed to read file: {fn}', exc=e)
                return
            if not tags:
                QMessageBox.information(self, 'Country Tags', 'No tags were found in the selected file.')
                return
            self._country_tags = tags
            QMessageBox.information(self, 'Country Tags', f'Imported {len(tags)} tags from:\n{fn}')
        except Exception as e:
            show_error(self, 'Country Tags Import', 'Failed to import country tags.', exc=e)

    def _mark_states_dirty(self, state_ids: list[str]):
        for sid in state_ids:
            meta = self._state_meta.get(sid)
            if meta is None:
                continue
            meta['_dirty'] = True
            self._dirty_states.add(sid)
        # If possible, persist the updated states into the active project file
        try:
            parent = getattr(self, 'parent', None) or getattr(self, 'parentWidget', lambda: None)()
            if parent is None:
                parent = getattr(self, 'parent', None)
            current_path = None
            try:
                current_path = getattr(parent, 'current_project_path', None)
            except Exception:
                current_path = None
            if current_path and os.path.isfile(current_path):
                try:
                    self._persist_states_to_project(current_path)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    def _op_quick_transfer(self):
        sel = self._ensure_selection()
        if not sel:
            return
        if len(sel) != 1:
            QMessageBox.information(self, 'Quick Transfer', 'Quick transfer works with a single selected state. Use Group Transfer for multi-state operations.')
            return
        if not self._province_polys:
            QMessageBox.information(self, 'Quick Transfer', 'Province geometry not available for this map; quick transfer requires a map generated with the latest converter.')
            return
        source_id = sel[0]
        meta = self._state_meta.get(source_id)
        if not meta:
            return
        provinces = list(meta.get('provinces', []))
        if not provinces:
            QMessageBox.information(self, 'Quick Transfer', f'State {source_id} has no provinces to transfer.')
            return
        destination_choices = [(sid, self._state_meta[sid].get('name', '')) for sid in self._state_meta.keys() if sid != source_id]
        if not destination_choices:
            QMessageBox.information(self, 'Quick Transfer', 'No other states available to receive provinces.')
            return

        dlg = QuickTransferDialog(self, source_id, provinces, destination_choices)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target_id = dlg.destination_state()
        if not target_id or target_id == source_id:
            return
        target_meta = self._state_meta.get(target_id)
        if not target_meta:
            return
        selected = [str(pid) for pid in dlg.selected_provinces()]
        if not selected:
            QMessageBox.information(self, 'Quick Transfer', 'No provinces selected for transfer.')
            return

        # Build undoable edit for source and target
        before_map: dict[str, dict] = {
            source_id: self._deep_copy_meta(meta),
            target_id: self._deep_copy_meta(target_meta)
        }
        new_source = self._deep_copy_meta(meta)
        source_prev = list(new_source.get('provinces', []))
        new_source['provinces'] = [p for p in source_prev if p not in selected]
        new_target = self._deep_copy_meta(target_meta)
        dest_list = new_target.setdefault('provinces', [])
        for pid in selected:
            if pid not in dest_list:
                dest_list.append(pid)
        after_map: dict[str, dict] = {
            source_id: new_source,
            target_id: new_target
        }
        self._push_state_meta_edit(before_map, after_map, [source_id, target_id], 'Quick Transfer Provinces')
        # Visual/UI focus remains similar
        self._select_states([target_id])
        QMessageBox.information(self, 'Quick Transfer', f'Moved {len(selected)} province(s) from state {source_id} to state {target_id}.')

    def _op_group_transfer(self):
        sel = self._ensure_selection()
        if not sel:
            return
        if len(sel) < 2:
            QMessageBox.information(self, 'Group Transfer', 'Select two or more states to use Group Transfer.')
            return
        if not self._province_polys:
            QMessageBox.information(self, 'Group Transfer', 'Province geometry not available for this map; group transfer requires a map generated with the latest converter.')
            return

        choices = [(sid, self._state_meta[sid].get('name', '')) for sid in self._state_meta.keys()]
        dlg = GroupTransferDialog(self, sel, choices)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target_id = dlg.target_state()
        if not target_id or target_id not in self._state_meta:
            QMessageBox.information(self, 'Group Transfer', 'Select a valid target state.')
            return

        template_id = sel[0]
        template_meta = self._state_meta.get(template_id)
        affected = set()

        # Prepare before snapshot for all potentially affected
        before_map: dict[str, dict] = {sid: self._deep_copy_meta(self._state_meta.get(sid, {})) for sid in sel}
        before_map[target_id] = self._deep_copy_meta(self._state_meta.get(target_id, {}))

        if dlg.move_provinces():
            dest_meta = self._deep_copy_meta(self._state_meta[target_id])
            dest_list = dest_meta.setdefault('provinces', [])
            dest_set = set(dest_list)
            after_temp: dict[str, dict] = {}
            for sid in sel:
                if sid == target_id:
                    continue
                src_meta = self._deep_copy_meta(self._state_meta.get(sid, {}))
                if not src_meta:
                    continue
                src_provs = list(src_meta.get('provinces', []))
                if not src_provs:
                    continue
                for pid in src_provs:
                    if pid not in dest_set:
                        dest_list.append(pid)
                        dest_set.add(pid)
                src_meta['provinces'] = []
                after_temp[sid] = src_meta
                affected.add(sid)
            after_temp[target_id] = dest_meta
            # Merge back after_temp into state meta for flags copy step below
            for sid, m in after_temp.items():
                self._state_meta[sid] = self._deep_copy_meta(m)
            affected.add(target_id)

        flags = dlg.copy_flags()
        if template_meta:
            for sid in sel:
                if sid == template_id:
                    continue
                meta = self._state_meta.get(sid)
                if not meta:
                    continue
                new_meta = self._deep_copy_meta(meta)
                if flags.get('owner'):
                    new_meta['owner'] = template_meta.get('owner')
                if flags.get('state_category'):
                    new_meta['state_category'] = template_meta.get('state_category')
                if flags.get('manpower'):
                    new_meta['manpower'] = template_meta.get('manpower')
                if flags.get('resources'):
                    tmpl_res = template_meta.get('resources') or {}
                    new_meta['resources'] = dict(tmpl_res)
                self._state_meta[sid] = new_meta
                affected.add(sid)

        if not affected:
            QMessageBox.information(self, 'Group Transfer', 'No changes were applied.')
            return

        # Build after snapshot and push undo command
        after_map: dict[str, dict] = {sid: self._deep_copy_meta(self._state_meta.get(sid, {})) for sid in affected}
        self._push_state_meta_edit(before_map, after_map, list(affected), 'Group Transfer')
        self._select_states([target_id])
        QMessageBox.information(self, 'Group Transfer', f'Group transfer complete. Updated {len(affected)} state(s).')

    def _op_group_tag_transfer(self):
        """Replace owner/core TAGs across selected states.

        - Owner: if a state's owner matches FROM, it will be set to TO.
        - Core: if FROM is present in cores, it will be replaced by TO.
        """
        sel = self._ensure_selection()
        if not sel:
            return
        if len(sel) < 2:
            QMessageBox.information(self, 'Group Transfer', 'Select two or more states to use Group Transfer.')
            return

        owners_in_sel: set[str] = set()
        cores_in_sel: set[str] = set()
        for sid in sel:
            meta = self._state_meta.get(sid) or {}
            o = (meta.get('owner') or '').strip().upper()
            if o:
                owners_in_sel.add(o)
            for t in (meta.get('cores') or []):
                t = (t or '').strip().upper()
                if t:
                    cores_in_sel.add(t)

        dlg = TagTransferDialog(self, sel, owners_in_sel, cores_in_sel)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        do_owner = dlg.owner_change()
        do_core = dlg.core_change()
        if not do_owner and not do_core:
            QMessageBox.information(self, 'Group Transfer', 'No operations selected.')
            return

        changed: set[str] = set()
        owner_from = dlg.owner_from() if do_owner else ''
        owner_to = dlg.owner_to() if do_owner else ''
        core_from = dlg.core_from() if do_core else ''
        core_to = dlg.core_to() if do_core else ''

        # Basic validation
        if do_owner and (not owner_from or not owner_to or owner_from == owner_to):
            QMessageBox.information(self, 'Group Transfer', 'Provide distinct FROM and TO owner TAGs.')
            return
        if do_core and (not core_from or not core_to or core_from == core_to):
            QMessageBox.information(self, 'Group Transfer', 'Provide distinct FROM and TO core TAGs.')
            return

        owner_changes = 0
        core_changes = 0
        before_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        for sid in sel:
            meta = self._state_meta.get(sid)
            if not meta:
                continue
            before_map[sid] = self._deep_copy_meta(meta)
            new_meta = self._deep_copy_meta(meta)
            # Owner
            if do_owner:
                cur_owner = (new_meta.get('owner') or '').strip().upper()
                if cur_owner == owner_from:
                    new_meta['owner'] = owner_to
                    changed.add(sid)
                    owner_changes += 1
            # Core
            if do_core:
                cores = list(new_meta.get('cores') or [])
                normalized = [str(c).strip().upper() for c in cores]
                if core_from in normalized:
                    # Remove core_from occurrences
                    cores = [c for c in cores if str(c).strip().upper() != core_from]
                    # Add core_to if not already present
                    if core_to not in [str(c).strip().upper() for c in cores]:
                        cores.append(core_to)
                    new_meta['cores'] = cores
                    changed.add(sid)
                    core_changes += 1
            after_map[sid] = new_meta

        if not changed:
            QMessageBox.information(self, 'Group Transfer', 'No changes were applied.')
            return

        self._push_state_meta_edit(before_map, after_map, list(changed), 'Group Transfer (Tags)')
        # Keep the selection as-is
        msg_parts = []
        if do_owner:
            msg_parts.append(f'owner updated in {owner_changes} state(s)')
        if do_core:
            msg_parts.append(f'cores replaced in {core_changes} state(s)')
        QMessageBox.information(self, 'Group Transfer', 'Done: ' + '; '.join(msg_parts) + '.')

    def _op_add_core(self):
        sel = self._ensure_selection()
        if not sel:
            return
        tag, ok = QInputDialog.getText(self, 'Add Core', 'Enter country tag to add as core:')
        if not ok:
            return
        tag = tag.strip().upper()
        if not tag:
            return
        before_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        changed: list[str] = []
        for sid in sel:
            meta = self._state_meta.get(sid)
            if meta is None:
                continue
            before_map[sid] = self._deep_copy_meta(meta)
            new_meta = self._deep_copy_meta(meta)
            cores = new_meta.setdefault('cores', [])
            if tag not in cores:
                cores.append(tag)
                changed.append(sid)
            after_map[sid] = new_meta
        if changed:
            self._push_state_meta_edit(before_map, after_map, changed, f'Add Core {tag}')
        QMessageBox.information(self, 'Add Core', f'Added core {tag} to {len(sel)} state(s).')

    def _op_remove_core(self):
        sel = self._ensure_selection()
        if not sel:
            return
        available = sorted({core for sid in sel for core in (self._state_meta.get(sid, {}).get('cores') or [])})
        if not available:
            QMessageBox.information(self, 'Remove Core', 'Selected states have no cores to remove.')
            return
        tag, ok = QInputDialog.getItem(self, 'Remove Core', 'Select core to remove:', available, 0, False)
        if not ok or not tag:
            return
        before_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        changed: list[str] = []
        for sid in sel:
            meta = self._state_meta.get(sid)
            if meta is None:
                continue
            before_map[sid] = self._deep_copy_meta(meta)
            new_meta = self._deep_copy_meta(meta)
            cores = new_meta.get('cores') or []
            new_list = [c for c in cores if c != tag]
            if new_list != cores:
                new_meta['cores'] = new_list
                changed.append(sid)
            after_map[sid] = new_meta
        if changed:
            self._push_state_meta_edit(before_map, after_map, changed, f'Remove Core {tag}')
        QMessageBox.information(self, 'Remove Core', f'Removed core {tag} from {len(sel)} state(s).')

    def _op_remove_all_cores(self):
        """Remove all core TAGs from the selected states after confirmation."""
        sel = self._ensure_selection()
        if not sel:
            return
        # Check if any selected state has cores
        any_cores = False
        for sid in sel:
            meta = self._state_meta.get(sid, {})
            if meta and (meta.get('cores') or []):
                any_cores = True
                break
        if not any_cores:
            QMessageBox.information(self, 'Remove All Cores', 'Selected states have no cores to remove.')
            return
        ans = QMessageBox.question(self, 'Remove All Cores',
                                   f'Remove all core TAGs from {len(sel)} selected state(s)?',
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        before_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        changed_ids: list[str] = []
        for sid in sel:
            meta = self._state_meta.get(sid)
            if meta is None:
                continue
            before_map[sid] = self._deep_copy_meta(meta)
            if (meta.get('cores') or []):
                new_meta = self._deep_copy_meta(meta)
                new_meta['cores'] = []
                after_map[sid] = new_meta
                changed_ids.append(sid)
            else:
                after_map[sid] = self._deep_copy_meta(meta)
        if changed_ids:
            self._push_state_meta_edit(before_map, after_map, changed_ids, 'Remove All Cores')
        QMessageBox.information(self, 'Remove All Cores', f'Removed cores from {len(changed_ids)} state(s).')

    def _op_increase_manpower(self, factor: int):
        sel = self._ensure_selection()
        if not sel:
            return
        before_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        changed_ids: list[str] = []
        for sid in sel:
            meta = self._state_meta.get(sid)
            if meta is None:
                continue
            current = meta.get('manpower')
            if current is None:
                continue
            try:
                new_val = max(0, int(float(current)) * factor)
            except Exception:
                continue
            before_map[sid] = self._deep_copy_meta(meta)
            new_meta = self._deep_copy_meta(meta)
            new_meta['manpower'] = new_val
            after_map[sid] = new_meta
            changed_ids.append(sid)
        if changed_ids:
            self._push_state_meta_edit(before_map, after_map, changed_ids, f'Increase Manpower x{factor}')
        QMessageBox.information(self, 'Increase Manpower', f'Applied x{factor} multiplier to manpower for {len(changed_ids)} state(s).')

    def _op_change_state_category(self):
        sel = self._ensure_selection()
        if not sel:
            return
        first_meta = self._state_meta.get(sel[0], {})
        default = (first_meta.get('state_category') or 'rural').strip()
        # Constrained list of categories
        categories = [
            'megalopolis', 'metropolis', 'large_city', 'city', 'large_town', 'town',
            'rural', 'pastoral', 'small_island', 'enclave', 'tiny_island', 'wasteland'
        ]
        # Prefer default if present; otherwise fallback to 'rural'
        current_index = categories.index(default) if default in categories else categories.index('rural')

        # Build a simple combo dialog
        dlg = QDialog(self)
        dlg.setWindowTitle('Change State Category')
        vbox = QVBoxLayout(dlg)
        vbox.addWidget(QLabel('Select new state category:'))
        combo = QComboBox(dlg)
        combo.addItems(categories)
        combo.setCurrentIndex(current_index)
        vbox.addWidget(combo)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=dlg)
        vbox.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        value = combo.currentText().strip()
        if not value:
            return
        before_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        changed_ids: list[str] = []
        for sid in sel:
            meta = self._state_meta.get(sid)
            if meta is None:
                continue
            before_map[sid] = self._deep_copy_meta(meta)
            new_meta = self._deep_copy_meta(meta)
            new_meta['state_category'] = value
            after_map[sid] = new_meta
            changed_ids.append(sid)
        if changed_ids:
            self._push_state_meta_edit(before_map, after_map, changed_ids, f'Change State Category → {value}')
        QMessageBox.information(self, 'Change State Category', f'Set state category to {value} for {len(sel)} state(s).')

    def _op_adjust_resources(self):
        sel = self._ensure_selection()
        if not sel:
            return
        first_meta = self._state_meta.get(sel[0], {})
        dialog = ResourceAdjustDialog(self, base=first_meta.get('resources'))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        before_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        changed_ids: list[str] = []
        for sid in sel:
            meta = self._state_meta.get(sid)
            if meta is None:
                continue
            before_map[sid] = self._deep_copy_meta(meta)
            new_meta = self._deep_copy_meta(meta)
            new_meta['resources'] = dict(values)
            after_map[sid] = new_meta
            changed_ids.append(sid)
        if changed_ids:
            self._push_state_meta_edit(before_map, after_map, changed_ids, 'Adjust Resources')
        QMessageBox.information(self, 'Adjust Resources', f'Updated resources for {len(sel)} state(s).')

    def _handle_lasso_selection(self, state_ids: list[str]):
        """Handle lasso selection from the view"""
        if not state_ids:
            return
        # Select all states within the lasso
        self._select_states(state_ids)

    def _op_export_selected(self):
        """Export selected states to HOI4 .txt files"""
        sel = self._ensure_selection()
        if not sel:
            return

        output_dir = QFileDialog.getExistingDirectory(self, 'Select output directory for state files')
        if not output_dir:
            return

        try:
            states_to_export = {sid: self._state_meta[sid] for sid in sel if sid in self._state_meta}
            success, failed = self._export_states_to_dir(states_to_export, output_dir)
            QMessageBox.information(self, 'Export Complete', f'Exported {success} state(s) successfully.\n{failed} failed.')
        except Exception as e:
            QMessageBox.warning(self, 'Export Failed', f'Error exporting states: {e}')

    def _op_export_dirty(self):
        """Export only dirty (edited) states to HOI4 .txt files"""
        if not self._dirty_states:
            QMessageBox.information(self, 'Export Dirty States', 'No edited states to export.')
            return

        output_dir = QFileDialog.getExistingDirectory(self, 'Select output directory for dirty state files')
        if not output_dir:
            return

        try:
            states_to_export = {sid: self._state_meta[sid] for sid in self._dirty_states if sid in self._state_meta}
            success, failed = self._export_states_to_dir(states_to_export, output_dir)
            QMessageBox.information(self, 'Export Complete', f'Exported {success} dirty state(s) successfully.\n{failed} failed.')
        except Exception as e:
            QMessageBox.warning(self, 'Export Failed', f'Error exporting dirty states: {e}')

    def _op_export_all(self):
        """Export all loaded states to HOI4 .txt files"""
        if not self._state_meta:
            QMessageBox.information(self, 'Export All States', 'No states loaded.')
            return

        # Offer choice of using the app's exports folder (convenient default) or selecting a folder
        try:
            choice = QMessageBox.question(self, 'Export All States', 'Export all states to the application exports folder?\n(Choose NO to pick a custom folder)', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
            if choice == QMessageBox.StandardButton.Yes:
                # Delegate to main window helper if available
                parent = getattr(self, 'parent', None) or getattr(self, 'parentWidget', lambda: None)()
                if parent is None:
                    parent = getattr(self, 'parent', None)
                try:
                    if parent is not None and getattr(parent, 'export_all_states', None) is not None:
                        succ, fail = parent.export_all_states()
                        QMessageBox.information(self, 'Export Complete', f'Exported {succ} state(s) successfully.\n{fail} failed.')
                        return
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
                # fallback: compute app exports folder using parent's logic
                try:
                    abd = getattr(parent, 'app_base_dir', None) if parent is not None else None
                    if not abd:
                        abd = os.path.join(str(Path.home()), '.focus_tool')
                    output_dir = os.path.join(abd, 'exports')
                except Exception:
                    output_dir = None
                if not output_dir:
                    QMessageBox.warning(self, 'Export Failed', 'Could not determine application exports folder. Please choose a folder manually.')
                    return
            else:
                output_dir = QFileDialog.getExistingDirectory(self, 'Select output directory for all state files')
                if not output_dir:
                    return

            try:
                success, failed = self._export_states_to_dir(self._state_meta, output_dir)
                QMessageBox.information(self, 'Export Complete', f'Exported {success} state(s) successfully.\n{failed} failed.')
            except Exception as e:
                QMessageBox.warning(self, 'Export Failed', f'Error exporting all states: {e}')
            return
        except Exception:
            QMessageBox.warning(self, 'Export Failed', 'An unexpected error occurred during export.')
            return

    def _export_states_to_dir(self, states: dict, output_dir: str, template: str = '{id}.txt') -> tuple[int, int]:
        """Write provided states to individual HOI4 .txt files using the required naming template.

        Args:
            states: mapping of state_id -> state_meta
            output_dir: directory to write files into
            template: filename template with {id} and optionally {name}

        Returns:
            (success_count, failed_count)
        """
        try:
            from _exporter import HOI4StateExporter
            exporter = HOI4StateExporter()
        except Exception:
            exporter = None
        os.makedirs(output_dir, exist_ok=True)
        success = 0
        failed = 0
        for sid, meta in states.items():
            try:
                # Build filename using template; allow {name} if available
                name_safe = str(meta.get('name', '') or '').replace('\n', ' ').strip()
                fname = template.format(id=sid, name=name_safe)
                # sanitize common problematic characters
                for ch in '<>:\\"/\\|?*':
                    fname = fname.replace(ch, '_')
                out_path = os.path.join(output_dir, fname)
                # Prefer using exporter to create content if available
                if exporter is not None:
                    content = exporter.state_to_string(str(sid), meta)
                else:
                    # Fallback: simple manual serialization similar to HOI4StateExporter
                    # Minimal but consistent format
                    lines = []
                    ind = "\t"
                    lines.append("state = {")
                    lines.append(f"{ind}id = {sid}")
                    lines.append(f"{ind}name = \"{meta.get('name', '')}\"")
                    manpower = meta.get('manpower')
                    if manpower is not None:
                        lines.append(f"{ind}manpower = {manpower}")
                    lines.append(f"{ind}state_category = {meta.get('state_category','rural')}")
                    lines.append("")
                    lines.append(f"{ind}history = {{")
                    owner = meta.get('owner')
                    if owner:
                        lines.append(f"{ind}{ind}owner = {owner}")
                    for core in meta.get('cores', []) or []:
                        lines.append(f"{ind}{ind}add_core_of = {core}")
                    for claim in meta.get('claims', []) or []:
                        lines.append(f"{ind}{ind}add_claim_by = {claim}")
                    lines.append(f"{ind}{ind}buildings = {{")
                    lines.append(f"{ind}{ind}{ind}infrastructure = 1")
                    lines.append(f"{ind}{ind}}}")
                    lines.append(f"{ind}}}")
                    provinces = meta.get('provinces', []) or []
                    if provinces:
                        lines.append("")
                        lines.append(f"{ind}provinces = {{")
                        pstrs = [str(p) for p in provinces]
                        for i in range(0, len(pstrs), 10):
                            chunk = pstrs[i:i+10]
                            lines.append(f"{ind}{ind}{' '.join(chunk)}")
                        lines.append(f"{ind}}}")
                    lines.append("}")
                    content = '\n'.join(lines)
                with open(out_path, 'w', encoding='utf-8-sig') as f:
                    f.write(content)
                success += 1
            except Exception:
                failed += 1
                try:
                    logger = logging.getLogger(__name__)
                    logger.exception('Failed to export state %s', sid)
                except Exception as e:
                    handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        return success, failed

    def _op_batch_province_transfer(self):
        """Deprecated: repurposed to perform batch owner/core TAG transfer.

        Delegates to the same tag-focused flow as Group Transfer (Tags)… so this
        action never manipulates provinces anymore.
        """
        # Reuse the new tag transfer dialog/logic
        return self._op_group_tag_transfer()

    def _op_batch_metadata_copy(self):
        """Copy metadata from template state to all selected states"""
        sel = self._ensure_selection()
        if not sel or len(sel) < 2:
            QMessageBox.information(self, 'Batch Metadata Copy', 'Select at least 2 states.')
            return

        # First selected state is template
        template_id = sel[0]
        template_meta = self._state_meta.get(template_id)
        if not template_meta:
            return

        # Dialog to choose what to copy
        dialog = QDialog(self)
        dialog.setWindowTitle('Batch Metadata Copy')
        layout = QVBoxLayout(dialog)

        label = QLabel(f'Copy metadata from {template_id} to {len(sel)-1} other state(s):')
        layout.addWidget(label)

        copy_owner = QCheckBox('Owner')
        copy_owner.setChecked(True)
        copy_category = QCheckBox('State Category')
        copy_category.setChecked(True)
        copy_manpower = QCheckBox('Manpower')
        copy_resources = QCheckBox('Resources')
        copy_cores = QCheckBox('Cores')
        copy_claims = QCheckBox('Claims')

        for cb in [copy_owner, copy_category, copy_manpower, copy_resources, copy_cores, copy_claims]:
            layout.addWidget(cb)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Apply to all selected states except template
        before_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        changed = 0
        for sid in sel[1:]:
            meta = self._state_meta.get(sid)
            if not meta:
                continue
            before_map[sid] = self._deep_copy_meta(meta)
            new_meta = self._deep_copy_meta(meta)
            if copy_owner.isChecked():
                new_meta['owner'] = template_meta.get('owner')
            if copy_category.isChecked():
                new_meta['state_category'] = template_meta.get('state_category')
            if copy_manpower.isChecked():
                new_meta['manpower'] = template_meta.get('manpower')
            if copy_resources.isChecked():
                new_meta['resources'] = dict(template_meta.get('resources') or {})
            if copy_cores.isChecked():
                new_meta['cores'] = list(template_meta.get('cores') or [])
            if copy_claims.isChecked():
                new_meta['claims'] = list(template_meta.get('claims') or [])
            after_map[sid] = new_meta
            changed += 1
        if changed:
            self._push_state_meta_edit(before_map, after_map, list(after_map.keys()), f'Batch Metadata Copy from {template_id}')
        QMessageBox.information(self, 'Batch Metadata Copy', f'Copied metadata to {changed} state(s) from template {template_id}.')

if __name__ == '__main__':
    # quick manual test harness
    app = QApplication(sys.argv)
    w = StateViewportDock()
    w.show()
    # load example minimal data
    example = {
        "states": {
            "1": {"name": "TestLand", "polygons": [[[0,0],[100,0],[100,100],[0,100]]]},
            "2": {"name": "Other", "polygons": [[[120,0],[220,0],[220,80],[120,80]]]}
        }
    }
    w.load_states_from_dict(example)
    sys.exit(app.exec())
