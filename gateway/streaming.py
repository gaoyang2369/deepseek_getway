"""Downstream SSE heartbeats without cancelling or buffering the upstream read."""

import asyncio

import anyio


async def with_heartbeats(stream, interval):
    iterator = stream.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(pending, return_exceptions=True)
