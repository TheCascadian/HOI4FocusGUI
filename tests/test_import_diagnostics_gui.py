"""Tests for the GUI side of import diagnostics (source/_focusGUI.py).

The import action must hand the user structured, actionable parse problems --
file, line, column, offending token, expected construct, source context -- and
let them decide whether to import what survived recovery. It must never collapse
that into a single "failed to parse" message.
"""

from pathlib import Path

from PyQt6.QtWidgets import QDialog

from _focusGUI import _show_parse_diagnostics, format_parse_diagnostics
from _txt_converter import convert_txt_to_project_dict

INVALID_DIR = Path(__file__).resolve().parent / "fixtures" / "paradox" / "invalid"


def _diagnostics(name: str):
    path = INVALID_DIR / name
    result = convert_txt_to_project_dict(
        path.read_text(encoding="utf-8"), filename=name
    )
    return result["diagnostics"]


def test_formatted_diagnostics_carry_every_field_a_user_needs():
    records = _diagnostics("unclosed_block.txt")
    assert records

    text = format_parse_diagnostics(records)
    record = records[0]

    assert f"{record['filename']}:{record['line']}:{record['column']}" in text
    assert record["code"] in text
    assert record["message"] in text
    assert record["expected"] in text
    assert "found:" in text
    # The caret line from the diagnostic context makes it into the report.
    assert "^" in text


def test_every_diagnostic_appears_in_the_report():
    records = _diagnostics("missing_value.txt") + _diagnostics("stray_operator.txt")
    text = format_parse_diagnostics(records)
    for record in records:
        assert record["message"] in text


def test_no_diagnostics_formats_to_empty_text():
    assert format_parse_diagnostics([]) == ""


def test_dialog_accepting_means_import_anyway(qtbot, monkeypatch):
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    assert _show_parse_diagnostics(
        None, _diagnostics("unclosed_block.txt"), "unclosed_block.txt",
        allow_continue=True,
    ) is True


def test_dialog_rejecting_means_abort(qtbot, monkeypatch):
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    assert _show_parse_diagnostics(
        None, _diagnostics("unclosed_block.txt"), "unclosed_block.txt",
        allow_continue=True,
    ) is False


def test_unrecoverable_parse_never_offers_to_continue(qtbot, monkeypatch):
    """With nothing parsed there is nothing to import, whatever the user clicks."""
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    assert _show_parse_diagnostics(
        None, _diagnostics("unclosed_block.txt"), "unclosed_block.txt",
        allow_continue=False,
    ) is False
