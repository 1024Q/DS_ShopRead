"""Chain background start/stop hooks onto the application lifespan (replaces deprecated on_event)."""

from contextlib import asynccontextmanager


def chain(app, start, stop):
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            await start()
            try:
                yield
            finally:
                await stop()

    app.router.lifespan_context = lifespan
