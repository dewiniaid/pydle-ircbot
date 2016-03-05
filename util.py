"""Miscellaneous utilities."""
import collections
import datetime
import functools

import tornado.locks
import tornado.gen
import pydle.async
import tornado.concurrent
import concurrent.futures
import asyncio

__all__ = ["listify"]

def listify(x):
    """
    Returns [] if x is None, a single-item list consisting of x if x is a str or bytes, otherwise returns x.

    listify(None) -> []
    listify("string") -> ["string"]
    listify(b"bytes") -> [b"bytes"]
    listify(["foo", "bar"]) -> ["foo", "bar"]

    :param x: What to listify.
    :return:
    """
    if x is None:
        return []
    if isinstance(x, (str, bytes)):
        return [x]
    return x


class Throttle:
    """
    Implements an asynchronous event throttling mechanism, e.g. for ensuring we don't flood IRC too much.

    The throttling mechanism is essentially a bucket that holds `burst` units and is refilled by `amount` every `rate`
    seconds.  The bucket can never exceed its capacity, but it can be 'less than empty' in some circumstances: At least
    one event is guaranteed to execute when the bucket is full, even if the event's cost exceeds the total capacity.

    The event queue is a `collections.deque` consisting of (cost, function) tuples.  Events are removed from the head
    of the queue if there's at least `cost` units available in the bucket.

    :ivar burst: Maximum bucket capacity (must be > 0)
    :ivar rate: Replenishment rate (must be >= 0, a replenishment rate of 0 disables all actual throttling mechanics.)
    :ivar amount: How much is replenished. (must be > 0)
    :ivar queue: Event queue.
    :ivar _wake_condition: Internal condition for waking up the event loop.
    """
    _FUTURE_CLASSES = (pydle.async.Future, tornado.concurrent.Future, asyncio.Future, concurrent.futures.Future)
    ZEROTIME = datetime.timedelta()
    _now = datetime.datetime.now
    _m = 0

    def __init__(self, burst, rate, amount=1, on_clear=None):
        """
        Creates a new Throttle.

        :param burst: The size of the 'bucket', or the maximum number of burstable events.  Must be > 0
        :param rate: Amount of time required before the number of available events recharges, in seconds or as a
            :class:`datetime.timedelta`.  Must be >= 0 seconds
        :param amount: How many units are recharged every `rate`.  Must be > 0
        :param on_clear: Function called when the queue is empty and the bucket is full, or None.  Receives the throttle
            as an argument.
        """
        if not isinstance(rate, datetime.timedelta):
            rate = datetime.timedelta(seconds=rate)
        self.rate = rate
        if self.rate < self.ZEROTIME:
            raise ValueError('rate cannot be < 0 seconds')
        if self.rate:
            # Don't bother validating these if rate is zero, since they won't do anything.
            if burst <= 0:
                raise ValueError('burst must be > 0')
            if amount <= 0:
                raise ValueError('amount must be > 0')
        self.burst = burst
        self.free = burst
        self.amount = amount
        self.last = self._now()
        self.queue = collections.deque()
        self.on_clear = on_clear
        self._wake_condition = tornado.locks.Condition()
        self._stop_condition = None
        self.running = False
        self._m = self.__class__._m
        self.__class__._m += 1
        self._n = 0

    def wake(self):
        """
        Called when something is added to the queue in case we're waiting for something.
        """
        self._wake_condition.notify_all()

    def _item(self, *args, **kwargs):
        """
        Internal implementation for add() and extend()
        """
        if not callable(args[0]):
            cost, *args = args
        else:
            cost = 1
        item = (cost, functools.partial(*args, **kwargs))
        return item

    def add(self, *args, **kwargs):
        """
        Adds an item to the event queue.

        Either the first or the second argument must be a callable.  If the first argument is a callable, the event
        cost is considered to 1.  Otherwise, the first argument specifies the event cost and the second argument is
        the callable.

        Remaining args and kwargs will be bound to the callable.
        """
        self.queue.append(self._item(*args, **kwargs))
        self.wake()

    def extend(self, items):
        """
        Adds the collection of items to the queue.

        Each item in the collection can be one of the following structures:

        - A callable, in which case this is equivalent to ``self.add(1, item)``
        - A mapping (e.g. a dict), in which case this is equivalent to
          ``args = item.pop(None); self.add(*item[None], **item``
        - A sequence, in which case this is equivalent to ``self.add(*item)``
        """
        def _gen():
            for item in items:
                if callable(item):
                    yield self._item(1, item)
                elif hasattr(item, 'keys'):
                    item = dict(item)
                    args = item.pop(None)
                    yield self._item(*args, **item)
                else:
                    yield self._item(*item)
        self.queue.extend(item for item in _gen())
        self.wake()

    def is_future(self, value):
        """
        Returns True if the value is something we consider a future.
        """
        return isinstance(value, self._FUTURE_CLASSES)

    def stop(self):
        """
        Causes run() to stop the next time it gets a chance to do so.
        """
        self._stop_condition = tornado.locks.Condition()
        self._wake_condition.notify_all()

    @pydle.async.coroutine
    def wait_for_stop(self):
        self.stop()
        yield self._stop_condition.wait()

    @pydle.async.coroutine
    def run(self):
        """
        Actually handles the throttling queue.
        """
        if self.running:
            return False
        try:
            self.running = True
            self._stop_condition = None
            while not self._stop_condition:
                self._n += 1
                # Recover capacity
                if self.rate and self.free < self.burst:
                    # How much time has gone by?
                    elapsed = self._now() - self.last
                    ticks = elapsed / self.rate
                    # Actually recover it.
                    self.free = min(self.free + ticks*self.amount, self.burst)
                    self.last += self.rate*ticks

                # Flush the queue.
                while self.queue:
                    cost = self.queue[0][0]
                    if self.free >= self.burst:
                        # Reset self.last to now so the timer is accurate.
                        self.last = self._now()
                    elif cost > self.free:
                        # Can't handle this item yet.  How long would it take to fix that?
                        deficit = min(cost, self.burst) - self.free
                        ticks = deficit / self.amount
                        timeout = (self.last + self.rate*ticks - self._now()).total_seconds()
                        if timeout > 0:
                            yield tornado.gen.sleep(timeout)
                        break  # Restart the loop at capacity recovery.
                    event = self.queue.popleft()[1]
                    self.free -= cost
                    result = event()
                    if self.is_future(result):
                        yield result
                        if self._stop_condition:
                            break

                # Handle the potential lack of a queue.
                if not self.queue:
                    try:
                        if not self.on_clear or self.free >= self.burst:
                            # We don't care about when the queue is recharged, so sleep until we're awoken.
                            if self.on_clear:
                                self.on_clear(self)
                            yield self._wake_condition.wait()
                            continue
                        # Figure out how long until we'll be full.  Sleep at most that long.
                        ticks = ((self.burst - self.free) / self.amount)
                        timeout = ((self.last - self._now()) + (self.rate * ticks))
                        if timeout > self.ZEROTIME:
                            result = self._wake_condition.wait(timeout=timeout)
                            yield result
                        continue
                    except tornado.gen.TimeoutError:
                        continue
        except Exception as ex:
            import traceback
            traceback.print_exc()
            raise ex
        finally:
            if self._stop_condition:
                self._stop_condition.notify_all()
            self.running = False

    def clear(self):
        """
        Clears the current event queue.
        """
        self.queue.clear()

    def reset(self):
        """
        Signals a stop and clears the event queue.
        """
        self.stop()
        self.clear()
