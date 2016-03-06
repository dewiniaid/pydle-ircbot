"""Miscellaneous utilities."""
import collections
import collections.abc
import datetime
import functools

import tornado.locks
import tornado.gen
import pydle.async
import tornado.concurrent
import concurrent.futures
import asyncio
import operator

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


class DependencyItem(collections.namedtuple('_DependencyItem', ['before', 'after', 'requires', 'required_by', 'data'])):
    def __new__(cls, before=None, after=None, required_by=None, requires=None, data=None):
        """
        Creates a new :class:`DependencyItem`

        :param before: This item will be before these items (if they exist)
        :param after: This item will be after these items (if they exist)
        :param required_by: Same as `before`, but the items must exist.
        :param requires: Same as `after`, but the items must exist.
        :param data: Optional data to associate.
        :return: A new :class:`DependencyItem`

        All dependencies are stored in sets and thus must be hashable.

        Items in `before` and `required_by` should not be in `after` or `requires`.  Dependency sorting will fail
        if they are.
        """
        before = set(before) if before else set()
        after = set(after) if after else set()
        required_by = set(required_by) if required_by else set()
        requires = set(required_by) if required_by else set()
        return super().__new__(cls, before, after, required_by, requires, data)


class DependencyDict(collections.abc.MutableMapping):
    """
    Handles a set of items with internal dependencies and sorts them in dependency order.

    A DependencyDict is a mapping of ```{ key: :class:`DependencyItem` } items.  The :attr:`~DependencyItem.before`,
    :attr:`~DependencyItem.after`, :attr:`~DependencyItem.requires` and :attr:`~DependencyItem.required_by` attributes
    on each item refer to other keys in the DependencyDict and how the item relates to them.

    Dependency solving will fail if there is a circular relationship between items, or if an item requires or is
    required by another item that is not present in a list.

    Items can be added to a DependencyDict using its :meth:`add` method, which will automatically construct the
    appropriate DependencyItem.  They can also be added using normal dictionary methods, provided that they are
    one of:

    - a :class:`DependencyItem`
    - `None` or another false-y value, which turns into an empty `DependencyItem`
    - Something dictionary-like, which is passed as keyword arguments to `add` and thus to `DependencyItem`
    - Something sequence-like, which is passed as positional arguments to `add` and thus to `DependencyItem`
    """
    #: Class or factory that produces dependency items.
    ITEMCLASS = DependencyItem

    #: Used in validating items and producing error messages.  (attr, verb, required, before)
    _ITEM_ATTRINFO = (
        ('before', 'is before', False, True), ('required_by', 'is required by', True, True),
        ('after', 'is after', False, False), ('requires', 'requires', True, False)
    )

    __marker = object()
    def __init__(self, *items):
        """
        Creates a new :class:`DependencyDict`.
        """
        self._data = {}
        self._solution = {}
        self._passes = None  # How many passes solving took.

    def add(self, key, *args, **kwargs):
        """
        Adds an item to the dictionary, creating a DependencyItem based on parameters.

        Equivalent to self[key] = DependencyItem(*args, **kwargs)

        :param key: Dictionary key.
        :param args: Passed to DependencyItem constructor.
        :param kwargs: Passed to DependencyItem constructor.

        If the item already exists in the set, before and after are merged with the existing contents.
        If the set was already solved, renders it unsolved.

        Passing nothing but `key` is perfectly valid and creates an item with no explicit dependencies.
        """
        self._solution = None
        self._data[key] = self.ITEMCLASS(*args, **kwargs)

    def clear(self):
        self._solution = {}
        self._data = {}

    def pop(self, key, default=__marker):
        if default is self.__marker:
            result = super().pop(key)
        else:
            result = super().pop(key, default)
        self._solution = None
        return result

    def popitem(self):
        result = super().popitem()
        self._solution = None
        return result

    def __setitem__(self, key, value):
        if isinstance(value, self.ITEMCLASS):
            self._data[key] = value
            self._solution = None
            return
        if not value:
            value = tuple()
        elif hasattr(value, 'keys') and hasattr(value, '__getitem__'):
            return self.add(key, **value)
        return self.add(key, *value)

    def __delitem__(self, key):
        del self._data[key]
        self._solution = None

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def unsorted_keys(self):
        """Returns dictionary keys in an undetermined order.  Will not solve dependencies."""
        return self._data.keys()

    def unsorted_values(self):
        """Returns dictionary values in an undetermined order.  Will not solve dependencies."""
        return self._data.values()

    def unsorted_items(self):
        """Returns dictionary items in an undetermined order.  Will not solve dependencies."""
        return self._data.items()

    def solve(self):
        """Solves dependencies and performs a topological sort."""
        pending = {k: set() for k in self._data}
        keys = pending.keys()

        # Validate everything and set up pending.
        for k, v in self._data.items():
            for attr, verb, required, before in self._ITEM_ATTRINFO:
                items = getattr(v, attr)
                if k in items:
                    raise RuntimeError("Item {!r} {} itself.".format(k, verb), k, attr)
                if required:
                    missing = items - keys
                    if missing:
                        missing = missing.pop()
                        raise RuntimeError("Item {!r} {} missing item {!r}.".format(k, verb, missing), k, attr, missing)
                    overlap = items
                else:
                    overlap = items & keys

                if before:
                    for other in overlap:
                        pending[other].add(k)
                    continue
                pending[k].update(overlap)

        # Actually solve.
        solution = []
        n = 0
        while pending:
            n += 1
            # Find items with no (remaining) dependencies.
            solved = list(k for k, v in pending.items() if not v)
            if not solved:
                raise RuntimeError(
                    "Could not solve dependencies on pass {} ({} items remaining)".format(n, len(pending)),
                    n, len(pending)
                )
            solution.extend(solved)
            for item in solved:
                del pending[item]
            for v in pending.values():
                v.difference_update(solved)
        self._solution = solution
        self._passes = n

    def __iter__(self):
        if self._solution is None:
            self.solve()
        return iter(self._solution)

    def __len__(self):
        return len(self._data)


if __name__ == '__main__':
    dset = DependencyDict()

    ct = 1000
    import random
    for ix in range(ct):
        obj = ix
        before = set()
        after = set()
        pending = set()

        # Decide what our odds of having 'before' items are.
        p = ix/(ct-1)
        if random.random() > (0.85 * p):
            after = set(random.sample(range(0, ix), random.randrange(1 + int(0.10*ix))))
        if random.random() > (0.65 * (1-p)):
            before = set(random.sample(range(ix+1, ct), random.randrange(1 + int(0.10*(ct - ix - 1)))))
        dset.add(obj, before, after)
    print(", ".join(str(x) for x in dset))
    print(", ".join(str(x) for x in dset._solution))
