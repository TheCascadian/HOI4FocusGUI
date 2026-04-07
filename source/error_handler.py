#!/usr/bin/env python3
"""
Unified Error Handler Module for HOI4 Focus GUI

This module provides the SINGLE SOURCE OF TRUTH for all error handling
across the entire application. No module may contain local try/except blocks
except where explicitly delegated through this system.

DESIGN PRINCIPLES:
    1. Zero Local Error Logic - All errors route through this system
    2. Typed Error Taxonomy - Structured hierarchy distinguishing error types
    3. Policy-Driven Behavior - Configurable via policy objects, not conditionals
    4. Context Propagation - Every error carries full context
    5. Lossless Refactoring - Original behavior preserved via policies
    6. No Catch-and-Forget - Every exception is wrapped, classified, forwarded

ERROR TAXONOMY:
    AppError (base)
    ├── FatalError - Unrecoverable, requires app termination
    │   ├── ConfigurationError - Invalid/corrupt configuration
    │   └── StateCorruptionError - Unrecoverable state corruption
    ├── RecoverableError - Operation failed but app can continue
    │   ├── FileOperationError - File I/O failures
    │   ├── ValidationError - Data validation failures
    │   ├── DependencyError - Missing dependency/import failures
    │   ├── NetworkError - Network/update failures
    │   └── BatchProcessingError - Batch operation failures (with partial results)
    └── UserError - User-initiated or user-facing errors
        ├── UserCancelledError - User cancelled operation
        └── UserInputError - Invalid user input

POLICIES:
    ErrorPolicy defines HOW an error is handled:
    - LOG_ONLY: Log error, continue execution
    - RAISE: Log and re-raise
    - GUI_NOTIFY: Show GUI dialog (if available)
    - RETRY: Attempt retry with backoff
    - SUBSTITUTE_DEFAULT: Use default value, log warning
    - ABORT_OPERATION: Stop current operation, don't crash app
    - SILENT: Suppress completely (use sparingly!)

USAGE PATTERNS:
    # 1. Direct error raising (preferred for new code):
    from error_handler import raise_error, FileOperationError
    if not path.exists():
        raise_error(FileOperationError("File not found", path=str(path)))

    # 2. Delegated try/except (for boundary code):
    from error_handler import handle_exception, ErrorPolicy
    try:
        external_library_call()
    except ExternalError as e:
        handle_exception(e, policy=ErrorPolicy.LOG_ONLY, 
                        context={"operation": "external_call"})

    # 3. Safe operation decorator (for optional features):
    from error_handler import safe_operation
    @safe_operation(default_return=[], policy=ErrorPolicy.LOG_ONLY)
    def load_optional_plugins():
        return discover_plugins()

    # 4. Context manager for operation scopes:
    from error_handler import operation_context
    with operation_context("Exporting project", file_path=path):
        export_to_file(data, path)  # Any error gets full context

    # 5. Batch processing with error collection:
    from error_handler import batch_operation, BatchProcessingError
    with batch_operation("Processing focuses") as batch:
        for focus in focuses:
            batch.process(process_focus, focus)
    if batch.has_errors:
        # Handle partial failures

    # 6. GUI-safe error display:
    from error_handler import show_error_dialog
    show_error_dialog(parent, "Save Failed", "Could not save project", exc=e)
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
import time
from abc import ABC
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import wraps
from pathlib import Path
from threading import local
from typing import (
    Any, Callable, Dict, Generic, Iterator, List, Optional, 
    Tuple, Type, TypeVar, Union, TYPE_CHECKING
)

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

logger = logging.getLogger(__name__)

# Type variables for generics
T = TypeVar('T')
E = TypeVar('E', bound='AppError')

# =============================================================================
# Error Policies
# =============================================================================

class ErrorPolicy(Enum):
    """Defines how an error should be handled.
    
    Policy selection determines the error handler's behavior without
    requiring conditional logic at each error site.
    """
    LOG_ONLY = auto()           # Log error, continue execution
    RAISE = auto()              # Log and re-raise exception
    GUI_NOTIFY = auto()         # Show GUI dialog (with fallback to log)
    RETRY = auto()              # Attempt retry with exponential backoff
    SUBSTITUTE_DEFAULT = auto() # Use default value, log as warning
    ABORT_OPERATION = auto()    # Stop current operation cleanly
    SILENT = auto()             # Suppress completely (use sparingly!)
    GUI_NOTIFY_AND_RAISE = auto()  # Show dialog then raise


class ErrorSeverity(Enum):
    """Severity level for error classification."""
    DEBUG = auto()      # Development-time errors
    INFO = auto()       # Informational (expected failures)
    WARNING = auto()    # Recoverable issues
    ERROR = auto()      # Significant failures
    CRITICAL = auto()   # Fatal errors requiring intervention


class ErrorCategory(Enum):
    """High-level categorization for error routing."""
    VALIDATION = auto()     # Data validation failures
    IO = auto()             # File/network I/O errors
    DEPENDENCY = auto()     # Missing imports/modules
    STATE = auto()          # Application state errors
    LOGIC = auto()          # Programming/logic errors
    USER = auto()           # User-initiated errors
    EXTERNAL = auto()       # Third-party/external errors
    INTERNAL = auto()       # Internal application errors


# =============================================================================
# Policy Configuration
# =============================================================================

@dataclass
class PolicyConfig:
    """Configuration for error handling behavior.
    
    Attributes:
        policy: Primary handling policy
        severity: Error severity level
        log_level: Logging level string
        show_traceback: Include traceback in logs
        user_message: User-friendly message (for GUI)
        max_retries: Maximum retry attempts (for RETRY policy)
        retry_delay: Initial delay between retries in seconds
        default_value: Default value (for SUBSTITUTE_DEFAULT policy)
        propagate: Whether to propagate error up the chain
        notify_user: Whether error should be shown to user
        batch_safe: Whether error is safe in batch processing context
    """
    policy: ErrorPolicy = ErrorPolicy.LOG_ONLY
    severity: ErrorSeverity = ErrorSeverity.ERROR
    log_level: str = "error"
    show_traceback: bool = True
    user_message: Optional[str] = None
    max_retries: int = 3
    retry_delay: float = 1.0
    default_value: Any = None
    propagate: bool = False
    notify_user: bool = False
    batch_safe: bool = True


# Pre-defined policy configurations for common scenarios
POLICY_SILENT = PolicyConfig(
    policy=ErrorPolicy.SILENT,
    severity=ErrorSeverity.DEBUG,
    log_level="debug",
    show_traceback=False,
    batch_safe=True
)

POLICY_LOG_DEBUG = PolicyConfig(
    policy=ErrorPolicy.LOG_ONLY,
    severity=ErrorSeverity.DEBUG,
    log_level="debug",
    show_traceback=False,
    batch_safe=True
)

POLICY_LOG_WARNING = PolicyConfig(
    policy=ErrorPolicy.LOG_ONLY,
    severity=ErrorSeverity.WARNING,
    log_level="warning",
    show_traceback=True,
    batch_safe=True
)

POLICY_LOG_ERROR = PolicyConfig(
    policy=ErrorPolicy.LOG_ONLY,
    severity=ErrorSeverity.ERROR,
    log_level="error",
    show_traceback=True,
    batch_safe=True
)

POLICY_RAISE = PolicyConfig(
    policy=ErrorPolicy.RAISE,
    severity=ErrorSeverity.ERROR,
    log_level="error",
    show_traceback=True,
    propagate=True,
    batch_safe=False
)

POLICY_GUI_NOTIFY = PolicyConfig(
    policy=ErrorPolicy.GUI_NOTIFY,
    severity=ErrorSeverity.ERROR,
    log_level="error",
    show_traceback=True,
    notify_user=True,
    batch_safe=False
)

POLICY_GUI_AND_RAISE = PolicyConfig(
    policy=ErrorPolicy.GUI_NOTIFY_AND_RAISE,
    severity=ErrorSeverity.ERROR,
    log_level="error",
    show_traceback=True,
    notify_user=True,
    propagate=True,
    batch_safe=False
)

POLICY_ABORT = PolicyConfig(
    policy=ErrorPolicy.ABORT_OPERATION,
    severity=ErrorSeverity.ERROR,
    log_level="error",
    show_traceback=True,
    batch_safe=False
)

POLICY_RETRY = PolicyConfig(
    policy=ErrorPolicy.RETRY,
    severity=ErrorSeverity.WARNING,
    log_level="warning",
    show_traceback=False,
    max_retries=3,
    retry_delay=1.0,
    batch_safe=True
)

POLICY_DEFAULT_VALUE = PolicyConfig(
    policy=ErrorPolicy.SUBSTITUTE_DEFAULT,
    severity=ErrorSeverity.WARNING,
    log_level="warning",
    show_traceback=False,
    batch_safe=True
)


# =============================================================================
# Error Context
# =============================================================================

@dataclass
class ErrorContext:
    """Structured context information for error tracking.
    
    Every error carries context about where and why it occurred,
    enabling better debugging and user-facing messages.
    """
    module: str = ""
    operation: str = ""
    file_path: Optional[str] = None
    state_info: Dict[str, Any] = field(default_factory=dict)
    user_message: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert context to dictionary for logging."""
        result = {
            "module": self.module,
            "operation": self.operation,
            "timestamp": self.timestamp,
        }
        if self.file_path:
            result["file_path"] = self.file_path
        if self.state_info:
            result["state"] = self.state_info
        if self.user_message:
            result["user_message"] = self.user_message
        return result
    
    def __str__(self) -> str:
        parts = []
        if self.module:
            parts.append(f"module={self.module}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.file_path:
            parts.append(f"path={self.file_path}")
        if self.state_info:
            state_str = ", ".join(f"{k}={v}" for k, v in self.state_info.items())
            parts.append(f"state=({state_str})")
        return " | ".join(parts) if parts else "<no context>"


# Thread-local storage for context stack
_context_stack = local()

def _get_context_stack() -> List[ErrorContext]:
    """Get the current thread's context stack."""
    if not hasattr(_context_stack, 'stack'):
        _context_stack.stack = []
    return _context_stack.stack

def get_current_context() -> Optional[ErrorContext]:
    """Get the current error context from the stack."""
    stack = _get_context_stack()
    return stack[-1] if stack else None

def get_full_context() -> List[ErrorContext]:
    """Get the full context stack for nested operations."""
    return list(_get_context_stack())


# =============================================================================
# Exception Hierarchy
# =============================================================================

class AppError(Exception):
    """Base exception class for all application errors.
    
    All application-specific exceptions inherit from this class,
    enabling consistent error handling and context propagation.
    
    Attributes:
        message: Human-readable error message
        context: Structured error context
        original_exception: The wrapped original exception (if any)
        category: Error category for routing
        default_policy: Default handling policy for this error type
    """
    category: ErrorCategory = ErrorCategory.INTERNAL
    default_policy: PolicyConfig = POLICY_LOG_ERROR
    
    def __init__(
        self, 
        message: str,
        context: Optional[ErrorContext] = None,
        original_exception: Optional[BaseException] = None,
        **context_kwargs
    ):
        super().__init__(message)
        self.message = message
        self.original_exception = original_exception
        
        # Build context from current stack and kwargs
        if context:
            self.context = context
        else:
            current = get_current_context()
            self.context = ErrorContext(
                module=context_kwargs.pop('module', current.module if current else ''),
                operation=context_kwargs.pop('operation', current.operation if current else ''),
                file_path=context_kwargs.pop('file_path', context_kwargs.pop('path', None)),
                user_message=context_kwargs.pop('user_message', None),
                state_info=context_kwargs
            )
    
    def with_context(self, **kwargs) -> 'AppError':
        """Add additional context to this error."""
        for key, value in kwargs.items():
            if key == 'module':
                self.context.module = value
            elif key == 'operation':
                self.context.operation = value
            elif key in ('file_path', 'path'):
                self.context.file_path = value
            elif key == 'user_message':
                self.context.user_message = value
            else:
                self.context.state_info[key] = value
        return self
    
    def get_user_message(self) -> str:
        """Get a user-friendly error message."""
        if self.context.user_message:
            return self.context.user_message
        return self.message
    
    def get_log_message(self) -> str:
        """Get detailed message for logging."""
        parts = [self.message]
        if self.context:
            parts.append(f"[{self.context}]")
        if self.original_exception:
            parts.append(f"(caused by: {type(self.original_exception).__name__}: {self.original_exception})")
        return " ".join(parts)
    
    def __str__(self) -> str:
        return self.get_log_message()


# -----------------------------------------------------------------------------
# Fatal Errors (Unrecoverable)
# -----------------------------------------------------------------------------

class FatalError(AppError):
    """Base class for unrecoverable errors requiring app termination."""
    category = ErrorCategory.INTERNAL
    default_policy = POLICY_GUI_AND_RAISE


class ConfigurationError(FatalError):
    """Raised when configuration is invalid or corrupt."""
    category = ErrorCategory.STATE


class StateCorruptionError(FatalError):
    """Raised when application state becomes unrecoverably corrupt."""
    category = ErrorCategory.STATE


# -----------------------------------------------------------------------------
# Recoverable Errors
# -----------------------------------------------------------------------------

class RecoverableError(AppError):
    """Base class for recoverable errors where app can continue."""
    category = ErrorCategory.INTERNAL
    default_policy = POLICY_LOG_ERROR


class FileOperationError(RecoverableError):
    """Raised when file operations fail."""
    category = ErrorCategory.IO
    default_policy = POLICY_LOG_ERROR


class ValidationError(RecoverableError):
    """Raised when data validation fails."""
    category = ErrorCategory.VALIDATION
    default_policy = POLICY_LOG_WARNING


class DependencyError(RecoverableError):
    """Raised when a required dependency is missing."""
    category = ErrorCategory.DEPENDENCY
    default_policy = POLICY_LOG_WARNING


class ImportFailureError(DependencyError):
    """Raised when an import fails."""
    pass


class NetworkError(RecoverableError):
    """Raised when network operations fail."""
    category = ErrorCategory.IO
    default_policy = POLICY_RETRY


class ParseError(RecoverableError):
    """Raised when parsing data fails."""
    category = ErrorCategory.VALIDATION
    default_policy = POLICY_LOG_ERROR


class RenderError(RecoverableError):
    """Raised when rendering/drawing operations fail."""
    category = ErrorCategory.INTERNAL
    default_policy = POLICY_LOG_WARNING


class SerializationError(RecoverableError):
    """Raised when serialization/deserialization fails."""
    category = ErrorCategory.IO
    default_policy = POLICY_LOG_ERROR


# -----------------------------------------------------------------------------
# Batch Processing Errors
# -----------------------------------------------------------------------------

@dataclass
class BatchError:
    """Single error within a batch operation."""
    index: int
    item: Any
    error: AppError
    
    def __str__(self) -> str:
        return f"Item {self.index}: {self.error.message}"


class BatchProcessingError(RecoverableError):
    """Raised when batch operations have partial failures.
    
    Contains both successful results and collected errors.
    """
    category = ErrorCategory.INTERNAL
    default_policy = POLICY_LOG_WARNING
    
    def __init__(
        self,
        message: str,
        errors: Optional[List[BatchError]] = None,
        results: Optional[List[Any]] = None,
        total_items: int = 0,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.errors = errors or []
        self.results = results or []
        self.total_items = total_items
    
    @property
    def error_count(self) -> int:
        return len(self.errors)
    
    @property
    def success_count(self) -> int:
        return self.total_items - self.error_count
    
    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0
    
    @property
    def all_failed(self) -> bool:
        return self.error_count == self.total_items > 0
    
    def get_error_summary(self) -> str:
        """Get a summary of batch errors."""
        if not self.errors:
            return "No errors"
        lines = [f"{self.error_count}/{self.total_items} items failed:"]
        for err in self.errors[:5]:  # Show first 5
            lines.append(f"  - {err}")
        if len(self.errors) > 5:
            lines.append(f"  ... and {len(self.errors) - 5} more")
        return "\n".join(lines)


# -----------------------------------------------------------------------------
# User Errors
# -----------------------------------------------------------------------------

class UserError(AppError):
    """Base class for user-initiated or user-facing errors."""
    category = ErrorCategory.USER
    default_policy = POLICY_GUI_NOTIFY


class UserCancelledError(UserError):
    """Raised when user cancels an operation."""
    default_policy = POLICY_SILENT


class UserInputError(UserError):
    """Raised when user provides invalid input."""
    default_policy = POLICY_GUI_NOTIFY


# -----------------------------------------------------------------------------
# External/Third-Party Errors
# -----------------------------------------------------------------------------

class ExternalError(AppError):
    """Wraps errors from external libraries/systems."""
    category = ErrorCategory.EXTERNAL
    default_policy = POLICY_LOG_ERROR


class QtError(ExternalError):
    """Wraps Qt-specific errors."""
    pass


# =============================================================================
# Error Handler Core
# =============================================================================

class ErrorHandler:
    """Central error handling dispatcher.
    
    This class is the single point of control for all error handling.
    It applies policies, logs errors, shows dialogs, and manages retries.
    """
    
    _instance: Optional['ErrorHandler'] = None
    
    def __init__(self):
        self._gui_parent: Optional['QWidget'] = None
        self._batch_mode: bool = False
        self._collected_errors: List[AppError] = []
        self._error_hooks: List[Callable[[AppError], None]] = []
    
    @classmethod
    def get_instance(cls) -> 'ErrorHandler':
        """Get the singleton error handler instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def set_gui_parent(self, parent: Optional['QWidget']) -> None:
        """Set the default GUI parent for error dialogs."""
        self._gui_parent = parent
    
    def add_error_hook(self, hook: Callable[[AppError], None]) -> None:
        """Add a hook to be called on every error."""
        self._error_hooks.append(hook)
    
    def remove_error_hook(self, hook: Callable[[AppError], None]) -> None:
        """Remove an error hook."""
        if hook in self._error_hooks:
            self._error_hooks.remove(hook)
    
    def handle(
        self,
        error: Union[AppError, BaseException],
        policy: Optional[PolicyConfig] = None,
        parent: Optional['QWidget'] = None,
        **context_kwargs
    ) -> Optional[Any]:
        """Handle an error according to the specified policy.
        
        Args:
            error: The error to handle (AppError or standard exception)
            policy: Policy configuration (defaults to error's default_policy)
            parent: GUI parent widget for dialogs
            **context_kwargs: Additional context to add to error
            
        Returns:
            default_value if SUBSTITUTE_DEFAULT policy, None otherwise
        """
        # Wrap non-AppError exceptions
        if not isinstance(error, AppError):
            error = self._wrap_exception(error, **context_kwargs)
        elif context_kwargs:
            error.with_context(**context_kwargs)
        
        # Get policy
        if policy is None:
            policy = error.default_policy
        
        # Call hooks
        for hook in self._error_hooks:
            try:
                hook(error)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        
        # Collect in batch mode
        if self._batch_mode and policy.batch_safe:
            self._collected_errors.append(error)
        
        # Apply policy
        return self._apply_policy(error, policy, parent)
    
    def _wrap_exception(
        self, 
        exc: BaseException,
        error_class: Type[AppError] = ExternalError,
        **context_kwargs
    ) -> AppError:
        """Wrap a standard exception in an AppError."""
        message = str(exc) or type(exc).__name__
        return error_class(
            message=message,
            original_exception=exc,
            **context_kwargs
        )
    
    def _apply_policy(
        self,
        error: AppError,
        policy: PolicyConfig,
        parent: Optional['QWidget']
    ) -> Optional[Any]:
        """Apply the handling policy to an error."""
        
        # Silent - do nothing
        if policy.policy == ErrorPolicy.SILENT:
            return policy.default_value
        
        # Log the error
        self._log_error(error, policy)
        
        # GUI notification
        if policy.policy in (ErrorPolicy.GUI_NOTIFY, ErrorPolicy.GUI_NOTIFY_AND_RAISE):
            self._show_gui_dialog(error, parent or self._gui_parent)
        
        # Raise if required
        if policy.policy in (ErrorPolicy.RAISE, ErrorPolicy.GUI_NOTIFY_AND_RAISE):
            raise error
        
        # Abort operation
        if policy.policy == ErrorPolicy.ABORT_OPERATION:
            raise error
        
        # Return default value
        if policy.policy == ErrorPolicy.SUBSTITUTE_DEFAULT:
            return policy.default_value
        
        return None
    
    def _log_error(self, error: AppError, policy: PolicyConfig) -> None:
        """Log an error with appropriate level and detail."""
        log_func = getattr(logger, policy.log_level, logger.error)
        
        message = error.get_log_message()
        
        if policy.show_traceback and error.original_exception:
            log_func(message, exc_info=error.original_exception)
        elif policy.show_traceback:
            log_func(message, exc_info=True)
        else:
            log_func(message)
    
    def _show_gui_dialog(
        self, 
        error: AppError,
        parent: Optional['QWidget']
    ) -> None:
        """Show a GUI error dialog."""
        try:
            _show_error_dialog_impl(
                parent=parent,
                title=type(error).__name__.replace("Error", " Error"),
                message=error.get_user_message(),
                details=self._format_error_details(error)
            )
        except Exception as e:
            logger.error(f"Failed to show error dialog: {e}")
    
    def _format_error_details(self, error: AppError) -> str:
        """Format error details for display."""
        lines = [f"Error: {error.message}"]
        
        if error.context:
            lines.append(f"\nContext: {error.context}")
        
        if error.original_exception:
            lines.append(f"\nOriginal exception: {type(error.original_exception).__name__}")
            lines.append(traceback.format_exc())
        
        return "\n".join(lines)
    
    @contextmanager
    def batch_mode(self) -> Iterator[List[AppError]]:
        """Context manager for batch processing mode.
        
        In batch mode, batch-safe errors are collected rather than
        immediately raised, allowing partial processing to complete.
        """
        self._batch_mode = True
        self._collected_errors = []
        try:
            yield self._collected_errors
        finally:
            self._batch_mode = False


# Global handler instance
_handler = ErrorHandler.get_instance()


# =============================================================================
# Public API Functions
# =============================================================================

def handle_exception(
    exc: BaseException,
    policy: Optional[PolicyConfig] = None,
    parent: Optional['QWidget'] = None,
    error_class: Type[AppError] = ExternalError,
    **context_kwargs
) -> Optional[Any]:
    """Handle an exception through the central error system.
    
    This is the primary entry point for handling caught exceptions.
    
    Args:
        exc: The exception to handle
        policy: Handling policy (default: error class default)
        parent: GUI parent for dialogs
        error_class: AppError subclass to wrap exception in
        **context_kwargs: Additional context
        
    Returns:
        Default value if SUBSTITUTE_DEFAULT policy
        
    Example:
        try:
            external_library.do_something()
        except ExternalException as e:
            handle_exception(e, policy=POLICY_LOG_WARNING,
                           operation="external_call")
    """
    if isinstance(exc, AppError):
        error = exc
        if context_kwargs:
            error.with_context(**context_kwargs)
    else:
        error = error_class(
            message=str(exc) or type(exc).__name__,
            original_exception=exc,
            **context_kwargs
        )
    
    return _handler.handle(error, policy, parent)


def raise_error(
    error: AppError,
    policy: Optional[PolicyConfig] = None,
    parent: Optional['QWidget'] = None
) -> None:
    """Raise an application error through the central system.
    
    Args:
        error: The AppError to raise
        policy: Optional policy override (default: POLICY_RAISE)
        parent: GUI parent for dialogs
        
    Example:
        if not data.is_valid():
            raise_error(ValidationError("Invalid data format", 
                                       field="name", value=data.name))
    """
    if policy is None:
        policy = PolicyConfig(
            policy=ErrorPolicy.RAISE,
            severity=error.default_policy.severity,
            log_level=error.default_policy.log_level,
            show_traceback=True,
            propagate=True
        )
    _handler.handle(error, policy, parent)


def log_error(
    error: Union[AppError, BaseException],
    level: str = "error",
    **context_kwargs
) -> None:
    """Log an error without raising or showing dialog.
    
    Args:
        error: Error to log
        level: Log level ("debug", "info", "warning", "error")
        **context_kwargs: Additional context
    """
    policy = PolicyConfig(
        policy=ErrorPolicy.LOG_ONLY,
        log_level=level,
        show_traceback=(level in ("error", "exception"))
    )
    _handler.handle(error, policy, **context_kwargs)


def show_error_dialog(
    parent: Optional['QWidget'],
    title: str,
    message: str,
    exc: Optional[BaseException] = None,
    details: Optional[str] = None
) -> None:
    """Show an error dialog to the user.
    
    Args:
        parent: Parent widget for dialog
        title: Dialog title
        message: User-friendly message
        exc: Optional exception for details
        details: Optional detail string
    """
    detail_str = details
    if exc and not details:
        detail_str = traceback.format_exc()
    _show_error_dialog_impl(parent, title, message, detail_str)


def _show_error_dialog_impl(
    parent: Optional['QWidget'],
    title: str,
    message: str,
    details: Optional[str] = None
) -> None:
    """Internal implementation of error dialog display."""
    try:
        # Try to use the custom ErrorDialog from _focusGUI
        try:
            from _focusGUI import ErrorDialog
            # Obfuscate sensitive paths
            try:
                from _utils import obfuscate_text
                message = obfuscate_text(message)
                if details:
                    details = obfuscate_text(details)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            dlg = ErrorDialog(title=title, message=message, details=details, parent=parent)
            dlg.exec()
            return
        except ImportError as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        
        # Fallback to QMessageBox
        from PyQt6.QtWidgets import QMessageBox
        try:
            from _utils import obfuscate_text
            message = obfuscate_text(message)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
        
        msg_box = QMessageBox(parent)
        msg_box.setIcon(QMessageBox.Icon.Critical)
        msg_box.setWindowTitle(title)
        msg_box.setText(message)
        if details:
            try:
                from _utils import obfuscate_text
                details = obfuscate_text(details)
            except Exception as e:
                handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))
            msg_box.setDetailedText(details)
        msg_box.exec()
        
    except Exception as e:
        # Last resort: just log it
        logger.error(f"Failed to show error dialog: {e}")
        logger.error(f"Original error - {title}: {message}")


# =============================================================================
# Decorators
# =============================================================================

def safe_operation(
    default_return: Any = None,
    policy: PolicyConfig = POLICY_LOG_WARNING,
    error_class: Type[AppError] = RecoverableError,
    operation_name: Optional[str] = None
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for operations that should fail gracefully.
    
    Wraps function in error handling that catches exceptions and
    returns a default value instead of propagating.
    
    Args:
        default_return: Value to return on error
        policy: Error handling policy
        error_class: AppError subclass for wrapping
        operation_name: Name for context (default: function name)
        
    Example:
        @safe_operation(default_return=[], policy=POLICY_LOG_DEBUG)
        def load_optional_data():
            return json.load(open('optional.json'))
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except AppError as e:
                e.with_context(operation=operation_name or func.__name__)
                _handler.handle(e, policy)
                return default_return  # type: ignore
            except Exception as e:
                error = error_class(
                    message=str(e) or type(e).__name__,
                    original_exception=e,
                    operation=operation_name or func.__name__
                )
                _handler.handle(error, policy)
                return default_return  # type: ignore
        return wrapper
    return decorator


def gui_error_boundary(
    title: str = "Error",
    message: str = "An error occurred",
    show_details: bool = True
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for GUI operations that should show error dialogs.
    
    Args:
        title: Dialog title
        message: User-friendly message
        show_details: Include exception details in dialog
        
    Example:
        @gui_error_boundary(title="Save Error", message="Failed to save file")
        def save_file(self):
            self.project.save()
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Optional[T]:
            try:
                return func(*args, **kwargs)
            except UserCancelledError:
                return None
            except AppError as e:
                parent = args[0] if args and hasattr(args[0], 'parent') else None
                _handler.handle(e, POLICY_GUI_NOTIFY, parent)
                return None
            except Exception as e:
                parent = args[0] if args and hasattr(args[0], 'parent') else None
                show_error_dialog(parent, title, message, exc=e)
                return None
        return wrapper
    return decorator


def retry_operation(
    max_attempts: int = 3,
    delay: float = 1.0,
    exponential_backoff: bool = True,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[int, Exception], None]] = None
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for operations that should retry on failure.
    
    Args:
        max_attempts: Maximum retry attempts
        delay: Initial delay between attempts (seconds)
        exponential_backoff: Double delay after each failure
        exceptions: Exception types to retry on
        on_retry: Callback(attempt, exception) on each retry
        
    Example:
        @retry_operation(max_attempts=5, delay=2.0)
        def fetch_remote_data():
            return requests.get(url).json()
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception: Optional[Exception] = None
            current_delay = delay
            
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        if on_retry:
                            on_retry(attempt + 1, e)
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed: {e}. "
                            f"Retrying in {current_delay:.1f}s..."
                        )
                        time.sleep(current_delay)
                        if exponential_backoff:
                            current_delay *= 2
            
            # All retries exhausted
            raise NetworkError(
                message=f"Operation failed after {max_attempts} attempts",
                original_exception=last_exception,
                operation=func.__name__
            )
        return wrapper
    return decorator


# =============================================================================
# Context Managers
# =============================================================================

@contextmanager
def operation_context(
    operation: str,
    module: Optional[str] = None,
    **context_kwargs
) -> Iterator[ErrorContext]:
    """Context manager that adds context to any errors raised within.
    
    Args:
        operation: Description of the operation
        module: Module name (auto-detected if not provided)
        **context_kwargs: Additional context (file_path, state info, etc.)
        
    Example:
        with operation_context("Exporting project", file_path=path):
            export_to_file(data, path)
    """
    # Auto-detect module from caller
    if module is None:
        import inspect
        frame = inspect.currentframe()
        if frame and frame.f_back:
            module = frame.f_back.f_globals.get('__name__', '')
    
    ctx = ErrorContext(
        module=module or '',
        operation=operation,
        file_path=context_kwargs.pop('file_path', context_kwargs.pop('path', None)),
        user_message=context_kwargs.pop('user_message', None),
        state_info=context_kwargs
    )
    
    stack = _get_context_stack()
    stack.append(ctx)
    try:
        yield ctx
    except AppError:
        # AppErrors already have context, just re-raise
        raise
    except Exception as e:
        # Wrap other exceptions with context
        raise ExternalError(
            message=str(e) or type(e).__name__,
            context=ctx,
            original_exception=e
        ) from e
    finally:
        stack.pop()


@contextmanager
def silent_operation(
    operation_name: str = "operation",
    log_errors: bool = True,
    log_level: str = "debug",
    **context_kwargs
) -> Iterator[None]:
    """Context manager for operations that should fail silently.
    
    Use sparingly - only for truly optional operations where
    failure is expected and acceptable.
    
    Args:
        operation_name: Description for logging
        log_errors: Whether to log failures
        log_level: Logging level for failures
        **context_kwargs: Additional context
        
    Example:
        with silent_operation("Setting optional Qt attribute"):
            widget.setAttribute(Qt.WA_DeleteOnClose, True)
    """
    try:
        yield
    except Exception as e:
        if log_errors:
            policy = PolicyConfig(
                policy=ErrorPolicy.LOG_ONLY,
                log_level=log_level,
                show_traceback=False
            )
            error = RecoverableError(
                message=f"Silent operation '{operation_name}' failed: {e}",
                original_exception=e,
                operation=operation_name,
                **context_kwargs
            )
            _handler.handle(error, policy)


@contextmanager 
def suppress_errors(*error_types: Type[Exception]) -> Iterator[None]:
    """Context manager to suppress specific error types.
    
    Like contextlib.suppress but logs the suppressed errors.
    
    Args:
        *error_types: Exception types to suppress
        
    Example:
        with suppress_errors(FileNotFoundError, PermissionError):
            os.remove(temp_file)
    """
    try:
        yield
    except error_types as e:
        logger.debug(f"Suppressed {type(e).__name__}: {e}")


# =============================================================================
# Batch Processing
# =============================================================================

class BatchProcessor(Generic[T]):
    """Helper for batch operations with error collection.
    
    Processes items in batch, collecting errors for partial failures
    instead of failing on first error.
    
    Example:
        with batch_operation("Processing focuses") as batch:
            for focus in focuses:
                batch.process(process_focus, focus)
        
        if batch.has_errors:
            show_error_dialog(parent, "Batch Errors", batch.error_summary)
        
        results = batch.results
    """
    
    def __init__(self, operation_name: str):
        self.operation_name = operation_name
        self.results: List[T] = []
        self.errors: List[BatchError] = []
        self._index = 0
    
    def process(
        self, 
        func: Callable[..., T], 
        *args, 
        **kwargs
    ) -> Optional[T]:
        """Process a single item, collecting any error.
        
        Args:
            func: Processing function
            *args, **kwargs: Arguments to func
            
        Returns:
            Result if successful, None if error occurred
        """
        try:
            result = func(*args, **kwargs)
            self.results.append(result)
            return result
        except AppError as e:
            self.errors.append(BatchError(
                index=self._index,
                item=args[0] if args else None,
                error=e
            ))
            return None
        except Exception as e:
            error = RecoverableError(
                message=str(e),
                original_exception=e,
                operation=self.operation_name,
                batch_index=self._index
            )
            self.errors.append(BatchError(
                index=self._index,
                item=args[0] if args else None,
                error=error
            ))
            return None
        finally:
            self._index += 1
    
    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0
    
    @property
    def error_count(self) -> int:
        return len(self.errors)
    
    @property
    def success_count(self) -> int:
        return len(self.results)
    
    @property
    def total_count(self) -> int:
        return self._index
    
    @property
    def error_summary(self) -> str:
        if not self.errors:
            return "No errors"
        lines = [f"{self.error_count}/{self.total_count} items failed:"]
        for err in self.errors[:10]:
            lines.append(f"  [{err.index}] {err.error.message}")
        if len(self.errors) > 10:
            lines.append(f"  ... and {len(self.errors) - 10} more")
        return "\n".join(lines)
    
    def raise_if_errors(self) -> None:
        """Raise BatchProcessingError if any errors occurred."""
        if self.has_errors:
            raise BatchProcessingError(
                message=f"{self.operation_name} completed with {self.error_count} errors",
                errors=self.errors,
                results=self.results,
                total_items=self.total_count,
                operation=self.operation_name
            )


@contextmanager
def batch_operation(operation_name: str) -> Iterator[BatchProcessor]:
    """Context manager for batch processing with error collection.
    
    Example:
        with batch_operation("Importing focuses") as batch:
            for data in focus_data:
                batch.process(create_focus, data)
        
        print(f"Imported {batch.success_count} focuses")
        if batch.has_errors:
            print(batch.error_summary)
    """
    processor: BatchProcessor = BatchProcessor(operation_name)
    with operation_context(operation_name):
        yield processor


# =============================================================================
# Safe Import Utilities
# =============================================================================

def safe_import(
    module_path: str,
    fallback: Any = None,
    log_failure: bool = False,
    log_level: str = "debug"
) -> Any:
    """Safely import a module or attribute, returning fallback on failure.
    
    Args:
        module_path: Dotted import path (e.g., "PIL.Image")
        fallback: Value to return on failure
        log_failure: Whether to log import failures
        log_level: Logging level for failures
        
    Returns:
        Imported object or fallback value
        
    Example:
        PIL_Image = safe_import('PIL.Image', fallback=None)
        numpy = safe_import('numpy', fallback=None, log_failure=True)
    """
    try:
        parts = module_path.split('.')
        if len(parts) == 1:
            return __import__(parts[0])
        else:
            module_name = '.'.join(parts[:-1])
            attr_name = parts[-1]
            module = __import__(module_name, fromlist=[attr_name])
            return getattr(module, attr_name)
    except Exception as e:
        if log_failure:
            log_func = getattr(logger, log_level, logger.debug)
            log_func(f"Failed to import '{module_path}': {e}")
        return fallback


# =============================================================================
# Validation Helpers  
# =============================================================================

def validate_not_none(value: T, name: str = "value") -> T:
    """Validate that a value is not None.
    
    Args:
        value: Value to validate
        name: Name for error message
        
    Returns:
        The validated value
        
    Raises:
        ValidationError: If value is None
    """
    if value is None:
        raise ValidationError(f"{name} cannot be None", field=name)
    return value


def validate_type(value: Any, expected_type: type, name: str = "value") -> Any:
    """Validate that a value is of expected type.
    
    Args:
        value: Value to validate
        expected_type: Expected type or tuple of types
        name: Name for error message
        
    Returns:
        The validated value
        
    Raises:
        ValidationError: If type doesn't match
    """
    if not isinstance(value, expected_type):
        raise ValidationError(
            f"{name} must be {expected_type.__name__}, got {type(value).__name__}",
            field=name,
            expected=expected_type.__name__,
            actual=type(value).__name__
        )
    return value


def validate_file_path(
    path: Any, 
    must_exist: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False
) -> Path:
    """Validate and return a file path.
    
    Args:
        path: Path to validate
        must_exist: Path must exist
        must_be_file: Path must be a file
        must_be_dir: Path must be a directory
        
    Returns:
        Validated Path object
        
    Raises:
        ValidationError: If path is invalid
        FileOperationError: If path doesn't exist when required
    """
    if path is None:
        raise ValidationError("Path cannot be None", field="path")
    
    try:
        path_obj = Path(path)
    except Exception as e:
        raise ValidationError(f"Invalid path: {path}", original_exception=e)
    
    if must_exist and not path_obj.exists():
        raise FileOperationError(f"Path does not exist: {path}", path=str(path))
    
    if must_be_file and path_obj.exists() and not path_obj.is_file():
        raise ValidationError(f"Path is not a file: {path}", path=str(path))
    
    if must_be_dir and path_obj.exists() and not path_obj.is_dir():
        raise ValidationError(f"Path is not a directory: {path}", path=str(path))
    
    return path_obj


# =============================================================================
# Delegated Exception Handlers
# =============================================================================
# These functions provide the ONLY allowed try/except pattern outside error_handler.py.
# They MUST be used for boundary code that catches external exceptions.

def catch_and_handle(
    func: Callable[..., T],
    *args,
    policy: PolicyConfig = POLICY_LOG_ERROR,
    error_class: Type[AppError] = ExternalError,
    default: T = None,  # type: ignore
    **kwargs
) -> T:
    """Execute a function and handle any exception.
    
    This is the ONLY approved pattern for catching exceptions outside
    the central error handler.
    
    Args:
        func: Function to execute
        *args: Arguments to function
        policy: Error handling policy
        error_class: AppError class for wrapping
        default: Default return value on error
        **kwargs: Keyword arguments to function
        
    Returns:
        Function result or default value
        
    Example:
        result = catch_and_handle(
            external_library.parse,
            data,
            policy=POLICY_LOG_WARNING,
            default={}
        )
    """
    try:
        return func(*args, **kwargs)
    except AppError as e:
        _handler.handle(e, policy)
        return default
    except Exception as e:
        error = error_class(
            message=str(e) or type(e).__name__,
            original_exception=e,
            operation=func.__name__
        )
        _handler.handle(error, policy)
        return default


def catch_and_convert(
    func: Callable[..., T],
    *args,
    error_class: Type[AppError] = ExternalError,
    **kwargs
) -> T:
    """Execute a function and convert any exception to AppError.
    
    Unlike catch_and_handle, this always raises an AppError
    if the original function raises an exception.
    
    Args:
        func: Function to execute
        *args: Arguments to function
        error_class: AppError class for wrapping
        **kwargs: Keyword arguments to function
        
    Returns:
        Function result
        
    Raises:
        AppError: Wrapped exception if func raises
    """
    try:
        return func(*args, **kwargs)
    except AppError:
        raise
    except Exception as e:
        raise error_class(
            message=str(e) or type(e).__name__,
            original_exception=e,
            operation=func.__name__
        ) from e


# =============================================================================
# Legacy Compatibility
# =============================================================================
# These functions provide backward compatibility with the old error_handler API

def handle_error(
    exc: BaseException,
    parent: Optional['QWidget'] = None,
    message: str = "An error occurred",
    title: str = "Error",
    show_dialog: bool = True,
    log_level: str = "error",
    **context
) -> None:
    """Legacy compatibility function - routes to new system.
    
    DEPRECATED: Use handle_exception() or raise_error() instead.
    """
    policy = PolicyConfig(
        policy=ErrorPolicy.GUI_NOTIFY if show_dialog and parent else ErrorPolicy.LOG_ONLY,
        log_level=log_level,
        show_traceback=True,
        user_message=message
    )
    
    error = ExternalError(
        message=message,
        original_exception=exc,
        **context
    )
    error.context.user_message = message
    
    _handler.handle(error, policy, parent)


def error_context(operation: str, **context_data):
    """Legacy compatibility - alias for operation_context."""
    return operation_context(operation, **context_data)


def log_exception_chain(exc: BaseException, level: str = "error") -> None:
    """Log an exception and its entire chain of causes."""
    log_func = getattr(logger, level, logger.error)
    log_func(f"Exception: {exc}")
    
    current = exc
    depth = 0
    while current.__cause__ or current.__context__:
        depth += 1
        current = current.__cause__ or current.__context__  # type: ignore
        log_func(f"{'  ' * depth}Caused by: {current}")


def format_exception_details(exc: BaseException, include_locals: bool = False) -> str:
    """Format exception details for display."""
    details = [f"Exception: {type(exc).__name__}: {exc}"]
    details.append("\nTraceback:")
    details.append(traceback.format_exc())
    return '\n'.join(details)


# Legacy retry function
def retry_on_error(
    func: Callable[..., T],
    max_attempts: int = 3,
    delay: float = 1.0,
    exponential_backoff: bool = True,
    exceptions: tuple = (Exception,)
) -> T:
    """Legacy retry function - wraps retry_operation decorator."""
    @retry_operation(
        max_attempts=max_attempts,
        delay=delay,
        exponential_backoff=exponential_backoff,
        exceptions=exceptions
    )
    def wrapper():
        return func()
    return wrapper()


# =============================================================================
# Module Initialization
# =============================================================================

def configure_error_handler(
    gui_parent: Optional['QWidget'] = None,
    log_level: str = "INFO"
) -> ErrorHandler:
    """Configure the global error handler.
    
    Should be called once at application startup.
    
    Args:
        gui_parent: Default parent widget for error dialogs
        log_level: Default logging level
        
    Returns:
        Configured ErrorHandler instance
    """
    handler = ErrorHandler.get_instance()
    handler.set_gui_parent(gui_parent)
    
    # Configure logging: prefer file logging to reduce console noise.
    # Reuse startup diagnostic folder if available so all traces land in one file.
    try:
        env_logs = os.environ.get('FOCUS_LOG_DIR', '').strip()
        if env_logs:
            logs_root = Path(env_logs)
        else:
            logs_root = Path.cwd() / 'Logs'
        try:
            logs_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Fallback to user home if current directory is not writable.
            logs_root = Path.home() / '.focus_tool' / 'Logs'
            logs_root.mkdir(parents=True, exist_ok=True)
        trace_log = logs_root / 'log.txt'
        fh = logging.FileHandler(trace_log, encoding='utf-8')
        logging.basicConfig(
            level=getattr(logging, log_level.upper(), logging.ERROR),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[fh],
            force=True,
        )
    except Exception:
        # If file handler fails, fall back to default basicConfig
        logging.basicConfig(
            level=getattr(logging, log_level.upper(), logging.ERROR),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            force=True,
        )
    
    return handler


def install_global_excepthook(
    gui_parent: Optional['QWidget'] = None,
    policy: Optional[PolicyConfig] = None
) -> None:
    """Route uncaught exceptions through the central error handler.

    Installs a sys.excepthook that wraps unhandled exceptions in an
    ``ExternalError`` and forwards them to the global handler. Falls back to
    printing the traceback if error handling itself fails.
    """
    handler = ErrorHandler.get_instance()
    if gui_parent is not None:
        try:
            handler.set_gui_parent(gui_parent)
        except Exception as e:
            handle_exception(e, policy=PolicyConfig(policy=ErrorPolicy.LOG_ONLY, log_level="error", show_traceback=True))

    default_policy = policy or PolicyConfig(
        policy=ErrorPolicy.GUI_NOTIFY,
        log_level="error",
        show_traceback=True,
        user_message="An unexpected error occurred."
    )

    def _global_hook(exc_type, exc_value, exc_tb):
        try:
            err = ExternalError(
                message=str(exc_value) or getattr(exc_type, "__name__", "Exception"),
                original_exception=exc_value,
                context=ErrorContext(
                    module="sys",
                    operation="global_excepthook",
                    user_message=str(exc_value) or "Unexpected error"
                )
            )
            handler.handle(err, default_policy, gui_parent)
        except Exception:
            traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = _global_hook


# =============================================================================
# Public Export Surface
# =============================================================================

__all__ = [
    # Policies
    'ErrorPolicy',
    'ErrorSeverity', 
    'ErrorCategory',
    'PolicyConfig',
    'POLICY_SILENT',
    'POLICY_LOG_DEBUG',
    'POLICY_LOG_WARNING',
    'POLICY_LOG_ERROR',
    'POLICY_RAISE',
    'POLICY_GUI_NOTIFY',
    'POLICY_GUI_AND_RAISE',
    'POLICY_ABORT',
    'POLICY_RETRY',
    'POLICY_DEFAULT_VALUE',
    
    # Error classes
    'AppError',
    'FatalError',
    'ConfigurationError',
    'StateCorruptionError',
    'RecoverableError',
    'FileOperationError',
    'ValidationError',
    'DependencyError',
    'ImportFailureError',
    'NetworkError',
    'ParseError',
    'RenderError',
    'SerializationError',
    'BatchProcessingError',
    'BatchError',
    'UserError',
    'UserCancelledError',
    'UserInputError',
    'ExternalError',
    'QtError',
    
    # Context
    'ErrorContext',
    'get_current_context',
    'get_full_context',
    
    # Core functions
    'handle_exception',
    'raise_error',
    'log_error',
    'show_error_dialog',
    
    # Decorators
    'safe_operation',
    'gui_error_boundary',
    'retry_operation',
    
    # Context managers
    'operation_context',
    'silent_operation',
    'suppress_errors',
    
    # Batch processing
    'BatchProcessor',
    'batch_operation',
    
    # Utilities
    'safe_import',
    'validate_not_none',
    'validate_type',
    'validate_file_path',
    'catch_and_handle',
    'catch_and_convert',
    
    # Configuration
    'ErrorHandler',
    'configure_error_handler',
    'install_global_excepthook',
    
    # Legacy compatibility
    'handle_error',
    'error_context',
    'log_exception_chain',
    'format_exception_details',
    'retry_on_error',
]
