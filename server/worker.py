"""Independent worker: python -m server.worker. Shares SQL state, never exposes HTTP."""
import asyncio
import signal
from dataclasses import replace
from fastapi import FastAPI
from server.api.routes import install_api
from server.config import Settings


async def serve():
    settings = replace(Settings.load(), embedded_worker=True)
    service = install_api(FastAPI(), settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ('SIGINT', 'SIGTERM'):
        signal.signal(getattr(signal, name), lambda *_:loop.call_soon_threadsafe(stop.set))
    await service.harness.start()
    try:
        while not stop.is_set():
            if service.harness.scheduler.done():
                service.harness.scheduler.result()
                raise RuntimeError('worker_scheduler_stopped')
            (settings.data_dir / 'worker.heartbeat').touch()
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except asyncio.TimeoutError:
                pass
    finally:
        await service.harness.close()


if __name__ == '__main__':
    asyncio.run(serve())
