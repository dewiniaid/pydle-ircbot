"""Miscellaneous utilities."""
import collections
import datetime

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
    Implements a tunable throttling mechanism, e.g. for ensuring we don't flood IRC too much.

    This mechanism has two components: `burst`, which is the number of events we can trigger at once, and `rate`, which
    is the amount of time required for to recover 1 event.

    :ivar burst: Configured maximum burst.
    :ivar rate: Recovery rate as a timedelta
    :ivar free: How many events are currently available for bursting
    :ivar next: The next time a tick should occur.  This may be greater than `rate` seconds away
    :ivar queue: Event queue.  Events are tuples of (cost, callable)
    :ivar on_clear: Function called if the queue is emptied.
    :ivar handle: Handle for scheduling.  Exact use is determined by caller.

    If rate is 0, has no actual throttling mechanics other than the fact that events are not executed until the queue
    is ticked.

    Scheduling with Throttle:
    -------------------------

    All of the methods that add item(s) to the queue (:meth:`add`, :meth:`addexec`, :meth:`extend`, :meth:`extendexec`)
    as well as :meth:`tick` return either None or a timedelta.

    If they return None, it is an indication that there is nothing to do and thus nothing should be scheduled.

    If they return a timedelta (which may be a 0-second timedelta, particularly in the case of :meth:`add` and
    :meth:`extend`), it means that :meth:`tick` should be called in that amount of time.

    There is no harm in calling :meth:`tick` early, and doing so will have no effect.  (Its return value, however, will
    tell you the next time you *should* call :meth:`tick`

    There is also no harm in calling :meth:`tick` late (other than events being delayed in their triggering).  It will
    correctly recover based on how much time has *actually* passed, and then process events until either the queue is
    empty there are no remaining events.

    Rate, Burst, and Event Cost:
    ----------------------------

    Throttle essentially works by having a bucket that holds `burst` units.  Every `rate` interval, one additional unit
    is added to the bucket.  The bucket can never be overfilled; any excess is discarded just as it would be if pouring
    water into a real bucket.  `free` indicates how full the bucket currently is (how many units are available for
    execution); it will equal `burst` when the bucket is full.

    Executing an event requires at least `cost` units in the bucket, which will be consumed when the event fires.

    If an event's cost happens to be greater than the total size of the bucket (`burst`), it will execute the next time
    the bucket is full.  In this case, the bucket can reach a less-than-empty state -- i.e. `free` will be negative.

    The methods that return the amount of time until the next :meth:`tick` will take into account the cost of the next
    event in the queue.  If the next event is cost 3 but only 1 unit is free, the time remaining will be 2*rate.  (This
    isn't exactly true, since we might be halfway to the next recovery, but is close enough for explanation.)
    """
    ZEROTIME = datetime.timedelta()

    def __init__(self, burst, rate, on_clear=None):
        """
        Creates a new Throttle.

        :param burst: Maximum number of burstable events.  Should be at last 1
        :param rate: Amount of time required before the number of available events recharges by 1, in seconds or as a
            :class:`datetime.timedelta`.
        :param on_clear: Function called when the queue is empty, or None
        """
        self.burst = burst
        if not isinstance(rate, datetime.timedelta):
            rate = datetime.timedelta(seconds=rate)
        self.rate = rate
        self.free = burst
        self.next = datetime.datetime.now() + rate
        self.queue = collections.deque()
        self.on_clear = on_clear
        self.handle = None

    def add(self, item, tick=False):
        """
        Adds the callable to the queue, and immediately ticks the queue if `tick` is set.

        :param item: Callable to add, or a tuple of (cost, callable)
        :param tick: If True, ticks the queue afterwards.
        :returns: (possibly updated) self.next
        """
        if callable(item):
            item = (1, item)
        self.queue.append(item)
        if tick:
            self.tick()
        return self.time_remaining()

    def addexec(self, item):
        """
        Adds the callable to the queue, and immediately ticks the queue if possible.

        :param item: Callable to add, or a tuple of (cost, callable)
        """
        return self.add(item, tick=True)

    def extend(self, items, tick=False):
        """
        Adds the callables in items to the queue, and immediately ticks the queue if `tick` is set.

        :param items: Sequence of callables to add.  Each item may also be a a tuple of (cost, callable)
        :param tick: If True, ticks the queue afterwards.
        """
        self.queue.extend((1, x) if callable(x) else x for x in items)
        if tick:
            self.tick()
        return self.time_remaining()

    def extendexec(self, items):
        """
        Adds the callables in items to the queue, and immediately ticks the queue.

        :param items: Sequence of callables to add.  Each item may also be a a tuple of (cost, callable)
        """
        return self.extend(items, tick=True)

    def pop(self):
        """
        Pops the oldest item off the queue and calls it.

        Returns False if the queue was empty.
        """
        if not self.queue:
            return False
        cost, fn = self.queue.popleft()
        if not self.rate:
            cost = 0
        self.free -= cost
        fn()
        return True

    def tick(self):
        """
        Ticks the queue.
        """
        if not self.rate:
            while self.queue:
                self.pop()
            return None

        if self.free >= self.burst:
            # If we're at max, run the next command regardless of its cost.
            if self.queue:
                self.pop()
                self.next = datetime.datetime.now() + self.rate
        else:
            self.recover()

        while self.queue and self.next_cost() <= self.free:
            self.pop()
        remaining = self.time_remaining()
        if remaining is None and self.on_clear:
            self.on_clear()
        return remaining

    def recover(self):
        """
        Refills the bucket based on elapsed time.  (Updates `free` and `next`)
        """
        recovered = min((datetime.datetime.now() - self.next) // self.rate, self.burst - self.free)
        if recovered <= 0:
            return
        self.next += self.rate*recovered  # We'll get 1 tick by then.
        self.free += recovered

    def next_cost(self):
        if not self.queue:
            return None
        if not self.rate:
            return 0
        return self.queue[0][0]

    def time_remaining(self):
        """
        Returns a timedelta representing how long until it makes sense to call tick() again.

        If the queue is empty AND we're maxed out, this returns None.
        """
        if not self.queue and self.free >= self.burst:
            return None
        if not self.rate:
            return self.ZEROTIME
        return max(
            self.ZEROTIME,
            self.next - datetime.datetime.now() + (self.rate * (min(self.next_cost() or 0, self.burst) - 1))
        )
