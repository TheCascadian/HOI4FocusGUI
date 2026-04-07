from __future__ import annotations

from _imports import (
    # Standard library
    os, re,
    # Typing
    Dict, List, Optional,
    # PyQt6
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QTextEdit, QVBoxLayout, QWidget,
    Qt, QAbstractItemView, QToolTip, QCursor
)


# region Default Effects Constants

DEFAULT_EFFECTS = [
    {"id": "add_political_power", "usage": "add_political_power = 50", "desc": "Grants political power to the country."},
    {"id": "add_stability", "usage": "add_stability = 0.05", "desc": "Change national stability (0.0-1.0)."},
    {"id": "add_manpower", "usage": "add_manpower = 10000", "desc": "Increase manpower by value."},
    {"id": "create_equipment", "usage": "create_equipment = { type = infantry_equipment amount = 100 }", "desc": "Creates equipment items for the country."},
    {"id": "country_event", "usage": "country_event = { id = my_event }", "desc": "Trigger a country event by id."},
]

# endregion

# region Effects Parser

def parse_effects_from_markdown(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return DEFAULT_EFFECTS
    try:
        text = open(path, 'r', encoding='utf-8').read()
    except Exception:
        return DEFAULT_EFFECTS

    # Very small markdown parser: look for headings (### name) followed by code blocks or paragraphs
    effects: List[Dict[str, str]] = []
    lines = text.splitlines()
    cur = None
    buf = []
    for ln in lines:
        m = re.match(r'^(#{1,6})\s*(.+)$', ln)
        if m:
            if cur:
                body = '\n'.join(buf).strip()
                usage_match = re.search(r'`([^`]+)`', body)
                usage = usage_match.group(1) if usage_match else ''
                effects.append({
                    'id': cur,
                    'usage': usage,
                    'desc': body.replace('`', '').strip()
                })
            cur = m.group(2).strip()
            buf = []
        else:
            if cur:
                buf.append(ln)
    if cur:
        body = '\n'.join(buf).strip()
        usage_match = re.search(r'`([^`]+)`', body)
        usage = usage_match.group(1) if usage_match else ''
        effects.append({'id': cur, 'usage': usage, 'desc': body.replace('`', '').strip()})

    if not effects:
        return DEFAULT_EFFECTS
    return effects

# endregion

# region Effects Inserter Dialog

class EffectsInserterDialog(QDialog):
    """Dialog to browse and insert HOI4 script effects into a QTextEdit.

    Usage:
        dlg = EffectsInserterDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            snippet = dlg.selected_snippet
    """

    def __init__(self, md_path: Optional[str] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Insert Effect")
        self.resize(640, 420)
        self.md_path = md_path or os.path.join(os.getcwd(), '.MD', 'hoi4_effects_list.md')
        self.effects = parse_effects_from_markdown(self.md_path)
        self.selected_snippet: Optional[str] = None

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search effects...')
        top.addWidget(QLabel('Search:'))
        top.addWidget(self.search)
        layout.addLayout(top)

        mid = QHBoxLayout()
        self.list = QListWidget()
        self.list.setMouseTracking(True)
        self.list.viewport().setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        mid.addWidget(self.list, 3)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumWidth(250)
        mid.addWidget(self.preview, 2)
        layout.addLayout(mid)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        insert_btn = QPushButton('Insert')
        cancel_btn = QPushButton('Cancel')
        btn_row.addWidget(insert_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        insert_btn.clicked.connect(self._on_insert)
        cancel_btn.clicked.connect(self.reject)
        self.search.textChanged.connect(self._on_search)
        self.list.itemClicked.connect(self._on_item_selected)
        # show tooltip on hover
        self.list.itemEntered.connect(self._on_item_hovered)

        self._populate_list()

    def _populate_list(self, filter_text: str = '') -> None:
        self.list.clear()
        ft = filter_text.lower().strip()
        for e in self.effects:
            title = e.get('id') or e.get('usage')
            if ft and ft not in title.lower() and ft not in (e.get('desc') or '').lower():
                continue
            it = QListWidgetItem(title)
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.list.addItem(it)

    def _on_search(self, text: str) -> None:
        self._populate_list(text)

    def _on_item_selected(self, item: QListWidgetItem) -> None:
        e = item.data(Qt.ItemDataRole.UserRole)
        if not e:
            return
        preview_text = f"Effect: {e.get('id')}\n\nUsage:\n{e.get('usage')}\n\nDescription:\n{e.get('desc')}"
        self.preview.setPlainText(preview_text)

    def _on_item_hovered(self, item: QListWidgetItem) -> None:
        e = item.data(Qt.ItemDataRole.UserRole)
        if not e:
            return
        # show tooltip near cursor
        QToolTip.showText(QCursor.pos(), f"{e.get('usage')}\n\n{e.get('desc')}")

    def _on_insert(self) -> None:
        sel = self.list.currentItem()
        if not sel:
            sel = self.list.item(0)
        if not sel:
            return
        e = sel.data(Qt.ItemDataRole.UserRole)
        if not e:
            return
        snippet = e.get('usage') or e.get('id')
        # If snippet is code-like and does not end with newline, ensure newline
        if snippet and not snippet.endswith('\n'):
            snippet = snippet + '\n'
        self.selected_snippet = snippet
        self.accept()

# endregion

__all__ = ["EffectsInserterDialog", "parse_effects_from_markdown"]