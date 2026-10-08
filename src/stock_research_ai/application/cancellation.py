"""Cancellation that reaches blocking work running off the event loop.

`asyncio.to_thread` cannot stop its worker: cancelling the awaiting task leaves the
thread - and any parser child process it is waiting on - running to completion.
`to_thread_cancellable` runs the work with a cancellation `threading.Event` in its
context (`cancel_scope()`); cancelling the awaiting task sets it, so a blocking wait
that checks it (e.g. `adapters.documents.isolation.run_isolated`) kills and reaps what
it owns and returns promptly. The cancellation then propagates as usual.
"""

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable

#: How long a cancelled caller waits for its worker to wind down before propagating.
CANCEL_GRACE_SECONDS = 5.0
_SCOPE: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "blocking_cancel_scope", default=None
)


def cancel_scope() -> threading.Event | None:
    """The current worker's cancellation event, if it runs under `to_thread_cancellable`."""
    return _SCOPE.get()


async def to_thread_cancellable[**P, R](
    func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> R:
    """`asyncio.to_thread`, plus: when the awaiting task is cancelled, the worker's
    `cancel_scope()` event is set and the worker is given `CANCEL_GRACE_SECONDS` to
    clean up before the cancellation propagates."""
    event = threading.Event()
    context = contextvars.copy_context()
    context.run(_SCOPE.set, event)
    call = functools.partial(context.run, func, *args, **kwargs)
    future = asyncio.get_running_loop().run_in_executor(None, call)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        event.set()
        await asyncio.wait({future}, timeout=CANCEL_GRACE_SECONDS)
        if future.done() and not future.cancelled():
            future.exception()  # retrieved: the cancellation is what the caller sees
        raise
