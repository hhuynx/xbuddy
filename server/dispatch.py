"""Redis delivers wake-ups; transactional SQL outbox and leases remain authoritative."""
import time
from server.storage.database import now


class Dispatch:
    stream = 'xbuddy:dispatch'
    group = 'research-workers'

    def __init__(self, db, url, consumer):
        self.db, self.consumer = db, consumer
        self.redis = None
        self.retry_at = 0
        self.status = 'disabled'
        if url:
            from redis.asyncio import Redis
            self.redis = Redis.from_url(url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1)

    async def tick(self):
        if not self.redis or time.monotonic() < self.retry_at:
            return
        try:
            from redis.exceptions import ResponseError
            try:
                await self.redis.xgroup_create(self.stream, self.group, id='0', mkstream=True)
            except ResponseError as error:
                if 'BUSYGROUP' not in str(error):
                    raise
            for row in self.db.all('SELECT * FROM dispatch_outbox WHERE sent_at IS NULL ORDER BY created_at LIMIT 50'):
                # A crash after XADD may duplicate a wake-up. SQL run leases prevent duplicate execution.
                await self.redis.xadd(self.stream, {'outbox_id':row['id'], 'run_id':row['run_id']}, maxlen=10000)
                with self.db.connect() as conn:
                    conn.execute('UPDATE dispatch_outbox SET sent_at=?,attempts=attempts+1 WHERE id=?', (now(), row['id']))
            reclaimed = await self.redis.xautoclaim(self.stream, self.group, self.consumer, 15000, '0-0', count=50)
            entries = list(reclaimed[1])
            fresh = await self.redis.xreadgroup(self.group, self.consumer, {self.stream:'>'}, count=50)
            entries += [entry for _, messages in fresh for entry in messages]
            # ACK means the durable SQL scheduler has been woken, not that research is complete.
            if entries:
                await self.redis.xack(self.stream, self.group, *[entry[0] for entry in entries])
            self.status = 'available'
        except Exception:
            # Preserve the unsent outbox. SQL polling keeps research/recovery functioning.
            self.status = 'degraded_sql_polling'
            self.retry_at = time.monotonic() + 5

    async def close(self):
        if self.redis:
            await self.redis.aclose()
