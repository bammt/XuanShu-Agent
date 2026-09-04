import asyncio
import logging
import os

from xuanshu_platform.db import DesignSession, SessionLocal
from sqlalchemy import select
from xuanshu_platform.services import STUDIO_PROCESSING_QUEUE, STUDIO_QUEUE, redis
from worker import process_studio_session, recover_interrupted_studio_jobs

STUDIO_CONCURRENCY = max(1, int(os.getenv('STUDIO_WORKER_CONCURRENCY', '4')))


async def requeue_studio_jobs() -> None:
    async with SessionLocal() as db:
        rows = (await db.scalars(select(DesignSession))).all()
        queued = [
            row.id for row in rows
            if (row.active_job or {}).get('status') == 'queued'
            and (row.active_job or {}).get('request')
        ]
    pending = set(await redis.lrange(STUDIO_QUEUE, 0, -1))
    processing = set(await redis.lrange(STUDIO_PROCESSING_QUEUE, 0, -1))
    for session_id in queued:
        if session_id not in pending and session_id not in processing:
            await redis.lpush(STUDIO_QUEUE, session_id)


async def recovery_loop() -> None:
    last_recovery = asyncio.get_running_loop().time()
    while True:
        try:
            now = asyncio.get_running_loop().time()
            if now - last_recovery >= 15:
                await requeue_studio_jobs()
                last_recovery = now
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('studio recovery loop failed; retrying')
            await asyncio.sleep(2)


async def studio_consumer(index: int) -> None:
    while True:
        try:
            session_id = await redis.rpoplpush(
                STUDIO_QUEUE, STUDIO_PROCESSING_QUEUE,
            )
            if session_id:
                await process_studio_session(session_id)
            else:
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('studio consumer %s failed; retrying', index)
            await asyncio.sleep(2)


async def main() -> None:
    await recover_interrupted_studio_jobs()
    logging.info('starting %s concurrent Studio consumers', STUDIO_CONCURRENCY)
    await asyncio.gather(
        recovery_loop(),
        *(studio_consumer(index) for index in range(STUDIO_CONCURRENCY)),
    )


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
