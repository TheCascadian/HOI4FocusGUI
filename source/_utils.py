from _imports import (
    # Standard library
    os, wraps,
)

from error_handler import ErrorPolicy, PolicyConfig, handle_exception, silent_operation


# Prevent _imports from re-importing _utils during setup.
os.environ["FOCUS_SKIP_UTILS"] = "1"
from _imports import *
os.environ.pop("FOCUS_SKIP_UTILS", None)

# region Performance & Safety Decorators

def safe_ui_operation(default_return=None, log_errors=False):
    """Decorator for UI operations that may fail gracefully without propagating exceptions.

    Use for non-critical UI operations where failures should return a default value
    rather than crash the application. Reduces visual clutter from excessive try-except blocks.

    Args:
        default_return: Value to return if operation fails (default: None)
        log_errors: If True, log failures at debug level (default: False)

    Usage:
        @safe_ui_operation(default_return=1.0)
        def get_canvas_scale(self):
            return self.canvas.views()[0].transform().m11()

    Complexity: O(1) overhead - single try-except wrapper vs multiple inline blocks
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if log_errors:
                    with silent_operation("Log safe_ui_operation failure"):
                        logger.debug(f"{func.__name__} failed gracefully: {e}")
                return default_return
        return wrapper
    return decorator

def safe_qt_call(default_return=None):
    """Decorator specifically for Qt method calls that may fail due to deleted C++ objects.

    Qt objects may be deleted while Python still holds references, causing RuntimeError.
    This decorator catches those cases and returns a safe default value.

    Args:
        default_return: Value to return if Qt call fails (default: None)

    Usage:
        @safe_qt_call(default_return=False)
        def is_item_visible(self):
            return self.isVisible()

    Complexity: O(1) overhead
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except (RuntimeError, AttributeError, Exception):
                return default_return
        return wrapper
    return decorator

#endregion

# region Error Handling & UI Utilities

def show_error(parent: Optional[QWidget], title: str, message: str, exc: Optional[BaseException] = None):
    """Show an error dialog with optional traceback details, obfuscating sensitive paths."""
    details = None
    try:
        if exc is not None:
            import traceback
            details = traceback.format_exc()
    except Exception:
        details = None
    try:
        try: from _focusGUI import ErrorDialog
        except Exception: logger.error("ErrorDialog class not found in _focusGUI module.")
        dlg = ErrorDialog(title=title, message=message, details=details, parent=parent)
        dlg.exec()
    except Exception:
        # Fallback to QMessageBox (still obfuscate)
        try:
            QMessageBox.critical(parent, title or "Error", obfuscate_text(message))
        except Exception:
            try:
                logger.exception("Failed to show error via QMessageBox")
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

def format_project_file_size(size: int | str | None = None, *, precision: int = 1, show_bytes: bool = False) -> str:
    """Format a file size for display in the Project Manager UI.

    - `size` may be an integer byte count, a filesystem path (str) or None.
    - If a path is supplied and exists, the function will stat() it to get the size.
    - `precision` controls decimal places for the unit conversion (default 1).
    - If `show_bytes` is True, the exact byte count is appended in parentheses.

    Returns a short human-readable string like "1.2 MB" or "1.2 MB (1,234,567 B)".
    For unknown or invalid inputs the function returns '—'.
    """
    try:
        # Resolve path -> bytes if needed
        if isinstance(size, str):
            try:
                if os.path.exists(size):
                    size = os.path.getsize(size)
                else:
                    # try to parse numeric strings
                    size = int(size)
            except Exception:
                return '—'
        if size is None:
            return '—'
        try:
            b = int(size)
        except Exception:
            return '—'
        if b < 0:
            return '—'
        # simple quick path for zero
        if b == 0:
            return '0 B' if not show_bytes else '0 B (0 B)'

        units = ['B', 'KB', 'MB', 'GB', 'TB']
        v = float(b)
        idx = 0
        while v >= 1024.0 and idx < len(units) - 1:
            v /= 1024.0
            idx += 1

        # format with requested precision, but trim unnecessary .0
        fmt = f"{v:.{max(0,int(precision))}f}"
        if '.' in fmt:
            # remove trailing zeros and dot
            fmt = fmt.rstrip('0').rstrip('.')

        human = f"{fmt} {units[idx]}"
        if show_bytes:
            human = f"{human} ({b:,} B)"
        return human
    except Exception:
        with silent_operation("Log format_project_file_size failure"):
            logger.exception("format_project_file_size failed")
        return '—'

# endregion

# region Pixmap Loading Utilities

def pixmap_from_file_via_pillow(path: str) -> Optional[QPixmap]:
    """Attempt to load image via Pillow and convert to QPixmap.

    Returns QPixmap on success, or None on failure. Requires Pillow to be installed.
    """
    if Image is None:
        return None
    try:
        im = Image.open(path)
        # Ensure RGBA
        if im.mode != 'RGBA':
            im = im.convert('RGBA')
        data = im.tobytes('raw', 'RGBA')
        w, h = im.size
        qimg = QImage(data, w, h, QImage.Format.Format_RGBA8888)
        pix = QPixmap.fromImage(qimg)
        # tag for comparison
        with silent_operation("Set pixmap path attribute"):
            setattr(pix, 'path', path)
        return pix
    except Exception:
        with silent_operation("Log pixmap_from_file_via_pillow failure"):
            logger.exception("pixmap_from_file_via_pillow failed")
        return None

# endregion

# region Path Display Utilities

def shorten_path_for_display(path: Optional[str], max_len: int = 80) -> str:
    """Return a short, human-friendly representation of a filesystem path.

    - If path is empty/None, returns ''.
    - If path is under the user's home, use obfuscation helper if available.
    - If path length is small, return unchanged.
    - Otherwise return a compact form like '.../parent/file.ext' (Windows drive preserved).
    """
    try:
        if not path:
            return ''
        p = str(path)
        # Prefer obfuscated home display when available
        with silent_operation("Try obfuscated home display"):
            home = os.path.expanduser('~')
            if home and p.startswith(home) and 'obfuscate_user_in_path' in globals():
                return obfuscate_user_in_path(p)
        if len(p) <= max_len:
            return p
        base = os.path.basename(p)
        parent = os.path.basename(os.path.dirname(p)) or ''
        drive, _ = os.path.splitdrive(p)
        if parent:
            if drive:
                return f"{drive}{os.path.sep}...{os.path.sep}{parent}{os.path.sep}{base}"
            return f"...{os.path.sep}{parent}{os.path.sep}{base}"
        # fallback to showing only filename with ellipsis
        if drive:
            return f"{drive}{os.path.sep}...{os.path.sep}{base}"
        return f"...{os.path.sep}{base}"
    except Exception:
        with silent_operation("Log shorten_path_for_display failure"):
            logger.exception("shorten_path_for_display failed")
        return str(path) if path is not None else ''

def set_widget_path_display(widget, path: Optional[str], max_len: int = 80):
    """Set a widget's visible text to a shortened path and attach a tooltip with the full path.

    Works for QLabel and QLineEdit (and others supporting setText / setToolTip).
    """
    try:
        disp = shorten_path_for_display(path, max_len=max_len)
        if hasattr(widget, 'setText'):
            widget.setText(disp)
        if hasattr(widget, 'setToolTip'):
            widget.setToolTip(str(path or ''))
    except Exception:
        with silent_operation("set_widget_path_display fallback"):
            if hasattr(widget, 'setText'):
                widget.setText(str(path or ''))
            logger.exception("set_widget_path_display failed; used fallback")

# endregion

# region Focus Cloning Utilities

def clone_focus_pure(src) -> 'Focus':
    """Create a clean Focus clone using only dataclass fields (no Qt caches).

    This avoids deepcopy() issues with non-picklable attributes like QPixmap that may be
    set as ad-hoc attributes on Focus objects by the UI.
    Updated to dynamically copy all dataclass fields to remain future-proof.
    """
    # Lazy import to avoid circular import issues
    from _dataStructs import Focus

    try:
        clone_data = {}
        # dataclasses expose __dataclass_fields__ mapping
        fields = getattr(type(src), '__dataclass_fields__', {})
        for fname in fields:
            with silent_operation(f"Clone field {fname}"):
                val = getattr(src, fname)
                if isinstance(val, list):
                    clone_data[fname] = list(val)
                elif isinstance(val, dict):
                    clone_data[fname] = dict(val)
                elif isinstance(val, set):
                    clone_data[fname] = set(val)
                else:
                    clone_data[fname] = val
        return Focus(**clone_data)
    except Exception as e:
        with silent_operation("Log clone_focus_pure failure"):
            logger.error(f"Failed to clone focus '{getattr(src, 'id', 'N/A')}': {e}")
        # fallback minimal clone
        with silent_operation("Create minimal focus clone"):
            return Focus(id=str(src.id), name=str(getattr(src, 'name', '') or ''), x=int(getattr(src, 'x', 0) or 0), y=int(getattr(src, 'y', 0) or 0))
        return Focus(id=str(getattr(src, 'id', 'unknown')))

# endregion

# region Obfuscation Utilities

def obfuscate_path(p: Optional[str]) -> str:
    """Return an obfuscated display string for a path.

    Policy: Never show absolute directories. Only show the last component (file or folder name).
    If empty/None, return '<path>'.
    """
    try:
        if not p:
            return '<path>'
        # Normalize slashes and strip trailing
        s = str(p).rstrip('\\/')
        if not s:
            return '<path>'
        # Return only the last component
        base = os.path.basename(s)
        return base or '<path>'
    except Exception:
        with silent_operation("Log obfuscate_path failure"):
            logger.exception("obfuscate_path failed")
        return '<path>'

def obfuscate_text(s: str) -> str:
    """Obfuscate sensitive path substrings within arbitrary text.

    Replaces occurrences of home directory and known base dirs with '<home>' and collapses absolute paths
    to only show their last component using a conservative regex.
    """
    try:
        if not s:
            return s
        out = str(s)
        # Replace the user's home directory (both separators)
        with silent_operation("Replace home directory in text"):
            home = os.path.expanduser('~')
            if home:
                # raw and normalized variants
                out = out.replace(home, '<home>')
                out = out.replace(home.replace('\\', '/'), '<home>')
        # Collapse Windows absolute paths like C:\something\...\name.ext -> C:\...\name.ext
        def _collapse_windows_path(m: re.Match) -> str:
            full = m.group(0)
            # Keep drive and last component if possible
            last = os.path.basename(full.rstrip('\\/'))
            return f"<path>/{last}" if last else '<path>'
        out = re.sub(r"[A-Za-z]:\\\\[^\s\n\r\t\"']+", _collapse_windows_path, out)
        # Collapse POSIX-like absolute paths /a/b/c -> .../c
        def _collapse_posix_path(m: re.Match) -> str:
            full = m.group(0)
            last = os.path.basename(full.rstrip('/'))
            return f"<path>/{last}" if last else '<path>'
        out = re.sub(r"/(?:[^\s\n\r\t\"']+/)+[^\s\n\r\t\"']+", _collapse_posix_path, out)
        return out
    except Exception:
        with silent_operation("Log obfuscate_text failure"):
            logger.exception("obfuscate_text failed")
        return s

def obfuscate_user_in_path(path: str) -> str:
    """Module-level helper to obfuscate the user's home folder in displayed paths.
    Replaces the home directory with '%USER%' or hides common Windows 'Users' segment.
    This function is for GUI display only and must not be used as a real filesystem path.
    """
    try:
        if not path:
            return ''
        home = os.path.expanduser('~')
        # Normalize for comparison
        norm_path = os.path.normcase(os.path.normpath(path))
        norm_home = os.path.normcase(os.path.normpath(home))
        if norm_home and norm_path.startswith(norm_home):
            parent = os.path.dirname(home)
            obf_prefix = os.path.join(parent, '%USER%')
            rel = path[len(home):]
            if rel.startswith(os.path.sep):
                return obf_prefix + rel
            else:
                return obf_prefix + os.path.sep + rel
        # fallback: try to hide a bare username segment in common Windows paths
        parts = path.split(os.path.sep)
        if len(parts) >= 3 and parts[1].lower() == 'users':
            parts[2] = '%USER%'
            return os.path.sep.join(parts)
        return path
    except Exception:
        with silent_operation("Log obfuscate_user_in_path failure"):
            logger.exception("obfuscate_user_in_path failed")
        return path

# endregion

# region Text Rendering Utilities

def draw_outlined_text(painter: QPainter, rect: QRectF, lines: list[str], font: QFont, outline_th: int, outline_col: QColor, fill_col: QColor, alignment: int = int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine)) -> None:
    """Draw text lines centered in rect with a stroked outline then filled text.

    - painter: active QPainter
    - rect: bounding QRectF
    - lines: list of strings (1 or 2 lines)
    - font: QFont to use (painter.font will be set to this)
    - outline_th: integer outline thickness
    - outline_col: QColor for outline
    - fill_col: QColor for text fill
    - alignment: Qt alignment flags
    """
    painter.save()  # Save painter state to prevent leaking pen/font changes
    try:
        painter.setFont(font)
        fm = QFontMetrics(painter.font())
        line_h = fm.height()
        total_h = line_h * len(lines)
        top = rect.center().y() - (total_h / 2.0)
        left = rect.left()
        w = rect.width()
        for i, ln in enumerate(lines):
            r = QRectF(left, top + i * line_h, w, line_h)
            # compute baseline x and y for addText
            text_w = fm.horizontalAdvance(ln)
            x = r.left() + (r.width() - text_w) / 2.0
            baseline_y = r.top() + (line_h - fm.height()) / 2.0 + fm.ascent()
            if outline_th and outline_th > 0:
                try:
                    path = QPainterPath()
                    path.addText(QPointF(x, baseline_y), painter.font(), ln)
                    pen = QPen(outline_col)
                    pen.setWidth(max(1, int(outline_th) * 2))
                    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    painter.setPen(pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)
                    painter.setPen(fill_col)
                    painter.fillPath(path, QBrush(fill_col))
                except Exception:
                    # fallback: offset drawing
                    painter.setPen(outline_col)
                    offs = outline_th
                    for dx, dy in ((-offs,0),(offs,0),(0,-offs),(0,offs)):
                        rrt = QRectF(r.translated(dx, dy))
                        painter.drawText(rrt, alignment, ln)
                    painter.setPen(fill_col)
                    painter.drawText(r, alignment, ln)
            else:
                painter.setPen(fill_col)
                painter.drawText(r, alignment, ln)
    except Exception:
        # Don't allow drawing errors to break painting
        try:
            painter.setPen(fill_col)  # Ensure pen color is set in fallback path
            for i, ln in enumerate(lines):
                r = QRectF(rect.left(), rect.top() + i * 14, rect.width(), 14)
                painter.drawText(r, alignment, ln)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
    finally:
        painter.restore()  # Always restore painter state

# endregion