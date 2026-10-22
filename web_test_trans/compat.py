"""Compatibility helpers used by the recording server on Python 3.8+."""
import asyncio
import contextvars
from functools import partial


async def run_in_thread(func, *args, **kwargs):
    """Run blocking work without blocking the loop; preserve context and errors.

    Equivalent to asyncio.to_thread, which is unavailable on Python 3.8.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = partial(context.run, func, *args, **kwargs)
    return await loop.run_in_executor(None, call)
