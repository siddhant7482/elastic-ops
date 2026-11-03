"""
Bounded worker pool for the portal's HTTP server.

ThreadingHTTPServer starts one OS thread per connection and never stops. That
is fine for three people and a dashboard; it is the wrong shape for a burst,
for two reasons specific to this process:

  * Every database call in the program funnels through one sqlite3 connection
    behind one mutex, and engine.loop() takes that same mutex. Extra request
    threads therefore buy no throughput at all — they only lengthen the queue
    the check loop is standing in. Monitoring that gets slower the more people
    watch it is the wrong way to fail.

  * A login costs one scrypt at n=2**15, roughly 32 MB of working memory, and
    auth.login pays it even for a username that does not exist — deliberately,
    so that response time cannot be used to enumerate accounts. Unbounded
    concurrent logins are therefore an unbounded memory claim on a box that is
    also a hypervisor.

So connections go to a fixed set of workers through a bounded queue, and past
that queue the answer is an immediate 503 with Retry-After, written on the
accepting thread. Refusing in a few hundred microseconds is kinder than
accepting work that will time out anyway, and it keeps the failure legible:
"busy" is a different thing from "down", and the portal should be able to say
which one it is while it is happening.

Keep-alive stays on, because the dashboard polls. But a fixed pool plus
keep-alive is a trap — six idle browser connections would pin six workers with
no work to do — so under pressure the handler answers `Connection: close` and
the worker goes straight back to the queue. See `Pool.under_pressure`.
"""

import http.server
import queue
import threading
import time

# Built rather than written out, so the length can never disagree with the body.
BUSY_BODY = b'{"error":"the portal is busy, try again","retry_after":2}'
BUSY = (b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json\r\n"
        b"Retry-After: 2\r\n"
        b"Connection: close\r\n"
        b"Content-Length: " + str(len(BUSY_BODY)).encode() + b"\r\n"
        b"\r\n" + BUSY_BODY)


class Gate:
    """Admission control for a path that costs real memory.

    A semaphore with a deadline and a tally of who was turned away. Used on the
    login route, where the cost is one scrypt per attempt whether the username
    exists or not — so the only thing that bounds the memory is a limit on how
    many can be in flight at once.
    """

    def __init__(self, limit=2, wait=3.0):
        self.limit = max(1, int(limit))
        self.wait = max(0.0, float(wait))
        self._sem = threading.BoundedSemaphore(self.limit)
        self._lock = threading.Lock()
        self.held = 0
        self.peak_held = 0
        self.admitted = 0
        self.turned_away = 0

    def acquire(self):
        """True if the caller may proceed. False means shed this one."""
        if not self._sem.acquire(timeout=self.wait):
            with self._lock:
                self.turned_away += 1
            return False
        with self._lock:
            self.held += 1
            self.admitted += 1
            self.peak_held = max(self.peak_held, self.held)
        return True

    def release(self):
        with self._lock:
            self.held = max(0, self.held - 1)
        self._sem.release()

    def snapshot(self):
        with self._lock:
            return {"limit": self.limit, "held": self.held,
                    "peak_held": self.peak_held, "admitted": self.admitted,
                    "turned_away": self.turned_away}


class Pool(http.server.HTTPServer):
    """HTTPServer with a fixed worker pool instead of a thread per connection.

    Everything socketserver already does well — finish_request, shutdown_request,
    error handling — is left alone. The only thing overridden is who runs it.
    """

    allow_reuse_address = True
    # Kernel listen backlog. Deliberately much larger than the internal queue:
    # a burst should wait in the kernel, where it costs nothing, rather than be
    # refused at the SYN with no explanation the client can read.
    request_queue_size = 256

    def __init__(self, addr, handler, workers=8, backlog=256, idle=5.0,
                 shed_after=5.0):
        self.workers = max(2, int(workers))
        self.idle = max(1.0, float(idle))
        self.shed_after = max(0.0, float(shed_after))
        self._q = queue.Queue(maxsize=max(8, int(backlog)))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.served = 0
        self.rejected = 0
        self.peak_active = 0
        self.peak_queued = 0
        self.wait_ms_peak = 0.0
        self._wait_ms_total = 0.0
        self._threads = []
        # Binds and listens, so request_queue_size has to be set before this.
        super().__init__(addr, handler)
        for i in range(self.workers):
            t = threading.Thread(target=self._work, name=f"http-{i + 1}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    # -- the bit that makes it a pool ------------------------------------
    def process_request(self, request, client_address):
        """Hand the connection to a worker, or refuse it if the wait is hopeless.

        While the queue is full this blocks the accept loop, on purpose. An
        unaccepted connection waits in the kernel backlog for nothing, so a
        client that would have been served 50 ms later should not be handed an
        error instead — measured, refusing eagerly turned 57% of a 200-client
        burst into 503s that the kernel would have absorbed for free.

        Only a queue that stays full for `shed_after` is genuinely beyond what
        this process can serve, and then saying so beats a timeout. The wait is
        taken in short slices so that shutdown() is never held up by more than
        one of them.
        """
        item = (request, client_address, time.monotonic())
        deadline = time.monotonic() + self.shed_after
        while not self._stop.is_set():
            try:
                self._q.put(item, timeout=0.2)
            except queue.Full:
                if time.monotonic() >= deadline:
                    break
                continue
            with self._lock:
                self.peak_queued = max(self.peak_queued, self._q.qsize())
            return

        with self._lock:
            self.rejected += 1
        try:
            request.sendall(BUSY)
        except OSError:
            pass                          # client already gone; nothing to say
        self.shutdown_request(request)

    def _work(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:              # shutdown pill
                return
            request, addr, queued_at = item
            waited = (time.monotonic() - queued_at) * 1000.0
            with self._lock:
                self.active += 1
                self.served += 1
                self.peak_active = max(self.peak_active, self.active)
                self.wait_ms_peak = max(self.wait_ms_peak, waited)
                self._wait_ms_total += waited
            try:
                # Per-operation, not per-connection: a client that stops talking
                # mid-request releases the worker instead of holding it forever.
                request.settimeout(self.idle)
                self.finish_request(request, addr)
            except OSError:
                pass                      # reset, timeout, client hung up
            except Exception:
                self.handle_error(request, addr)
            finally:
                self.shutdown_request(request)
                with self._lock:
                    self.active -= 1

    def under_pressure(self):
        """True when a worker should answer and hang up rather than hold the
        connection open for a keep-alive request that may never arrive.

        A browser opens several connections per host and keeps them. With a
        fixed pool that is how every worker ends up parked on an idle socket
        while real requests queue behind them.
        """
        if not self._q.empty():
            return True
        return self.active >= max(1, int(self.workers * 0.75))

    # -- lifecycle -------------------------------------------------------
    def server_close(self):
        self._stop.set()
        for _ in self._threads:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass
        super().server_close()
        for t in self._threads:
            t.join(timeout=1.0)

    # -- what the portal reports about itself ----------------------------
    def snapshot(self):
        with self._lock:
            mean = (self._wait_ms_total / self.served) if self.served else 0.0
            return {
                "workers": self.workers,
                "active": self.active,
                "queued": self._q.qsize(),
                "capacity": self._q.maxsize,
                "served": self.served,
                "rejected": self.rejected,
                "peak_active": self.peak_active,
                "peak_queued": self.peak_queued,
                "wait_ms_peak": round(self.wait_ms_peak, 1),
                "wait_ms_mean": round(mean, 2),
            }
