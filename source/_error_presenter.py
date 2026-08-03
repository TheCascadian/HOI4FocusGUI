"""Application-level error presenter registration.

This module is intentionally dependency-neutral: it imports nothing from the
project and nothing from PyQt6. It exists so that low-level helpers (``_utils``)
can *present* an error without importing the GUI layer (``_focusGUI``), which
would create a reverse dependency and an import cycle.

The GUI layer installs a presenter during application start-up::

    from _error_presenter import set_error_presenter

    def _present(parent, title, message, details=None) -> bool:
        ErrorDialog(title=title, message=message, details=details,
                    parent=parent).exec()
        return True

    set_error_presenter(_present)

Callers that need to show an error use ``_utils.show_error``, which delegates to
the installed presenter and falls back to a plain Qt message box when no
presenter has been installed (headless runs, tests, early start-up).
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

__all__ = [
    "ErrorPresenter",
    "set_error_presenter",
    "get_error_presenter",
    "clear_error_presenter",
]


@runtime_checkable
class ErrorPresenter(Protocol):
    """Callable that renders an error to the user.

    Implementations return ``True`` when the error was presented, and ``False``
    when they declined to present it (so the caller can use its own fallback).
    """

    def __call__(
        self,
        parent: Any,
        title: str,
        message: str,
        details: Optional[str] = None,
    ) -> bool:
        ...


_presenter: Optional[ErrorPresenter] = None


def set_error_presenter(presenter: ErrorPresenter) -> None:
    """Install the application-wide error presenter.

    Args:
        presenter: Callable matching :class:`ErrorPresenter`.

    Raises:
        TypeError: If ``presenter`` is not callable.
    """
    if not callable(presenter):
        raise TypeError(f"error presenter must be callable, got {type(presenter)!r}")
    global _presenter
    _presenter = presenter


def get_error_presenter() -> Optional[ErrorPresenter]:
    """Return the installed presenter, or ``None`` when none is installed."""
    return _presenter


def clear_error_presenter() -> None:
    """Remove the installed presenter (used by tests and on shutdown)."""
    global _presenter
    _presenter = None
