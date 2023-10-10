"""Logging utilities."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from functools import partial, wraps
import inspect
import logging
import logging.handlers
import queue
import traceback
from typing import Any, TypeVar, cast, overload

from homeassistant.core import HomeAssistant, callback, get_release_channel, is_callback
from homeassistant.exceptions import HomeAssistantError

_T = TypeVar("_T")


class SuppressHomeAssistantErrorStackTrace(logging.Filter):
    """Filter API password calls."""

    def __init__(self) -> None:
        """Initialize sensitive data filter."""
        super().__init__()
        self.stable_channel = get_release_channel() == "stable"

    def filter(self, record: logging.LogRecord) -> bool:
        """Conditionally suppress stack trace for HomeAssistantError."""
        if (
            not self.stable_channel
            or not record.exc_info
            or not record.exc_info[0]
            or not issubclass(record.exc_info[0], HomeAssistantError)
        ):
            return True

        record.exc_info = None
        return True


class HomeAssistantQueueHandler(logging.handlers.QueueHandler):
    """Process the log in another thread."""

    listener: logging.handlers.QueueListener | None = None

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Prepare a record for queuing.

        This is added as a workaround for https://bugs.python.org/issue46755
        """
        record = super().prepare(record)
        record.stack_info = None
        return record

    def handle(self, record: logging.LogRecord) -> Any:
        """Conditionally emit the specified logging record.

        Depending on which filters have been added to the handler, push the new
        records onto the backing Queue.

        The default python logger Handler acquires a lock
        in the parent class which we do not need as
        SimpleQueue is already thread safe.

        See https://bugs.python.org/issue24645
        """
        return_value = self.filter(record)
        if return_value:
            self.emit(record)
        return return_value

    def close(self) -> None:
        """Tidy up any resources used by the handler.

        This adds shutdown of the QueueListener
        """
        super().close()
        if not self.listener:
            return
        self.listener.stop()
        self.listener = None


@callback
def async_activate_log_queue_handler(hass: HomeAssistant) -> None:
    """Migrate the existing log handlers to use the queue.

    This allows us to avoid blocking I/O and formatting messages
    in the event loop as log messages are written in another thread.
    """
    simple_queue: queue.SimpleQueue[logging.Handler] = queue.SimpleQueue()
    queue_handler = HomeAssistantQueueHandler(simple_queue)
    queue_handler.addFilter(SuppressHomeAssistantErrorStackTrace())
    logging.root.addHandler(queue_handler)

    migrated_handlers: list[logging.Handler] = []
    for handler in logging.root.handlers[:]:
        if handler is queue_handler:
            continue
        logging.root.removeHandler(handler)
        migrated_handlers.append(handler)

    listener = logging.handlers.QueueListener(simple_queue, *migrated_handlers)
    queue_handler.listener = listener

    listener.start()


def log_exception(format_err: Callable[..., Any], *args: Any) -> None:
    """Log an exception with additional context."""
    module = inspect.getmodule(inspect.stack(context=0)[1].frame)
    if module is not None:
        module_name = module.__name__
    else:
        # If Python is unable to access the sources files, the call stack frame
        # will be missing information, so let's guard.
        # https://github.com/home-assistant/core/issues/24982
        module_name = __name__

    # Do not print the wrapper in the traceback
    frames = len(inspect.trace()) - 1
    exc_msg = traceback.format_exc(-frames)
    friendly_msg = format_err(*args)
    logging.getLogger(module_name).error("%s\n%s", friendly_msg, exc_msg)


@overload
def catch_log_exception(
    func: Callable[..., Coroutine[Any, Any, Any]], format_err: Callable[..., Any]
) -> Callable[..., Coroutine[Any, Any, None]]:
    ...


@overload
def catch_log_exception(
    func: Callable[..., Any], format_err: Callable[..., Any]
) -> Callable[..., None] | Callable[..., Coroutine[Any, Any, None]]:
    ...


def catch_log_exception(
    func: Callable[..., Any], format_err: Callable[..., Any]
) -> Callable[..., None] | Callable[..., Coroutine[Any, Any, None]]:
    """Decorate a function func to catch and log exceptions.

    If func is a coroutine function, a coroutine function will be returned.
    If func is a callback, a callback will be returned.
    """
    # Check for partials to properly determine if coroutine function
    check_func = func
    while isinstance(check_func, partial):
        check_func = check_func.func

    wrapper_func: Callable[..., None] | Callable[..., Coroutine[Any, Any, None]]
    if asyncio.iscoroutinefunction(check_func):
        async_func = cast(Callable[..., Coroutine[Any, Any, None]], func)

        @wraps(async_func)
        async def async_wrapper(*args: Any) -> None:
            """Catch and log exception."""
            try:
                await async_func(*args)
            except Exception:  # pylint: disable=broad-except
                log_exception(format_err, *args)

        wrapper_func = async_wrapper

    else:

        @wraps(func)
        def wrapper(*args: Any) -> None:
            """Catch and log exception."""
            try:
                func(*args)
            except Exception:  # pylint: disable=broad-except
                log_exception(format_err, *args)

        if is_callback(check_func):
            wrapper = callback(wrapper)

        wrapper_func = wrapper
    return wrapper_func


def catch_log_coro_exception(
    target: Coroutine[Any, Any, _T], format_err: Callable[..., Any], *args: Any
) -> Coroutine[Any, Any, _T | None]:
    """Decorate a coroutine to catch and log exceptions."""

    async def coro_wrapper(*args: Any) -> _T | None:
        """Catch and log exception."""
        try:
            return await target
        except Exception:  # pylint: disable=broad-except
            log_exception(format_err, *args)
            return None

    return coro_wrapper(*args)


def async_create_catching_coro(
    target: Coroutine[Any, Any, _T]
) -> Coroutine[Any, Any, _T | None]:
    """Wrap a coroutine to catch and log exceptions.

    The exception will be logged together with a stacktrace of where the
    coroutine was wrapped.

    target: target coroutine.
    """
    trace = traceback.extract_stack()
    wrapped_target = catch_log_coro_exception(
        target,
        lambda: "Exception in {} called from\n {}".format(
            target.__name__,
            "".join(traceback.format_list(trace[:-1])),
        ),
    )

    return wrapped_target
