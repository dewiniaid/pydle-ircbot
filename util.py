"""Miscellaneous utilities."""
import collections
import collections.abc
import datetime
import functools
import concurrent.futures
import asyncio
import itertools
import re
import tornado.locks
import tornado.gen
import tornado.concurrent
import pydle.async

__all__ = ["listify", "pad", "DependencyDict", "DependencyItem", "Throttle", "patternize"]


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


def pad(iterable, size, padding=None):
    """
    Yields items from iterable, and then yields `padding` enough times to have yielded a total of `size` items.

    Designed for cases where you might want to write ``foo, bar, baz = "foo,bar".split(",")`` and don't to special case
    the tuple unpacking.

    Note that if iterable has more than size elements, they will still all be returned.

    :param iterable: Iterable to yield from.
    :param size: Number of elements to yield.
    :param padding: What to yield after the iterator is exhausted.
    """
    for item in iterable:
        yield item
        size -= 1
    if size > 0:
        yield from itertools.repeat(padding, size)


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

        :param items: Iterable of items to add.

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

        :param value: Value to test.
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


class DependencyItem(
    collections.namedtuple('_DependencyItem', ['before', 'after', 'requires', 'required_by', 'priority', 'data'])):
    def __new__(cls, before=None, after=None, required_by=None, requires=None, priority=0, data=None):
        """
        Creates a new :class:`DependencyItem`

        :param before: This item will be before these items (if they exist)
        :param after: This item will be after these items (if they exist)
        :param required_by: Same as `before`, but the items must exist.
        :param requires: Same as `after`, but the items must exist.
        :param priority: Priority.  All items in the same priority will be grouped together.
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
        # noinspection PyTypeChecker
        return super().__new__(cls, before, after, required_by, requires, priority, data)


@functools.total_ordering
class _DependencyPriority:
    """Internal class used to identify dependency groups."""
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "{0.__class__.__name__}({0.name!r})".format(self)

    def __hash__(self):
        return hash(self.name) ^ 0x20160307  # Arbitrary-ish xor

    def __eq__(self, other):
        if not isinstance(other.__class__, self.__class__):
            raise TypeError
        return self.name == other.name

    def __le__(self, other):
        if not isinstance(other.__class__, self.__class__):
            raise TypeError
        return self.name < other.name


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

    def __init__(self, key=False, get_priority=True):
        """
        Creates a new :class:`DependencyDict`.
        :param key: If `False`, no sorting is performed other than what dependency solving required.  Otherwise, passed
        as-is to functions that perform sorting.
        :param get_priority: If `False`, dependencies are not sorted by priority.  Otherwise, follows the same semantics
            as `key` but receives the priority attribute as its argument.
        """
        self.sortkey = key
        self.get_priority = get_priority
        self._data = collections.OrderedDict()
        self._solved = False  # True if we've performed sorting and whatnot.
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
        self._data[key] = self.ITEMCLASS(*args, **kwargs)
        self._solved = False

    def clear(self):
        self._data = {}
        self._solved = True  # Because there's zero elements!

    def pop(self, key, default=__marker):
        if default is self.__marker:
            result = super().pop(key)
        else:
            result = super().pop(key, default)
        self._solved = not len(self._data)
        return result

    def popitem(self):
        result = super().popitem()
        self._solved = not len(self._data)
        return result

    def __setitem__(self, key, value):
        if isinstance(value, self.ITEMCLASS):
            self._data[key] = value
            self._solved = False
            return
        if not value:
            value = tuple()
        elif hasattr(value, 'keys') and hasattr(value, '__getitem__'):
            return self.add(key, **value)
        return self.add(key, *value)

    def __delitem__(self, key):
        del self._data[key]
        self._solved = not len(self._data)

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
        if not self._data:
            self._solved = True
            return
        pending = collections.OrderedDict((k, set()) for k in self._data)
        keys = pending.keys()
        if self.get_priority:
            priorities = collections.defaultdict(list)
            get_priority = self.get_priority if callable(self.get_priority) else lambda x: x
        else:
            priorities = None
            get_priority = None

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

            if get_priority:
                priorities[get_priority(v.priority)].append(k)

        # Sort groups and handle dependencies
        # We handle group dependencies by:
        # a) Making all members of a group dependent on the previous group.
        # b) Making the group dependent on all members of the group.
        if priorities and len(priorities) == 1:  # Ignore the overhead if there's only one group.
            priorities = None
            get_priority = None

        if priorities:
            prev = None
            for priority in sorted(priorities.keys()):
                members = set(priorities[priority])
                priority = _DependencyPriority(priority)
                if prev:
                    for member in members:
                        pending[member].add(prev)
                pending[priority] = set(members)
                prev = priority

        # Actually solve.
        solution = []
        n = 0
        while pending:
            n += 1
            # Find items with no (remaining) dependencies.
            if self.sortkey is False:
                solved = list(k for k, v in pending.items() if not v)
            else:
                solved = list(item[0] for item in sorted(filter(lambda x: not x[1], pending.items()), key=self.sortkey))
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
        self._data = collections.OrderedDict(
            (k, self._data[k]) for k in solution if not isinstance(k, _DependencyPriority)
        )
        self._solved = True
        self._passes = n

    def __iter__(self):
        if not self._solved:
            self.solve()
        return iter(self._data)

    def keys(self):
        if not self._solved:
            self.solve()
        return self._data.keys()

    def items(self):
        if not self._solved:
            self.solve()
        return self._data.items()

    def values(self):
        if not self._solved:
            self.solve()
        return self._data.values()

    def __len__(self):
        return len(self._data)


def patternize(pattern, flags=re.IGNORECASE, attr='fullmatch'):
    """
    Converts `pattern` to a function that accepts a single argument and returns True if the argument matches.

    :param pattern: Pattern to convert.  Either a string (which will be converted to a regex), a regex, or a callable.
    :param flags: Flags used when compiling regular expressions.  Only used if `pattern` is a `str`
    :param attr: Name of the method on a compiled regex that actually does matching.  'match', 'fullmatch', 'search',
    'findall` and `finditer` might be good choices.
    :return: a callable.


    pattern can be:
    - a compiled regular expression, which will be tested using pattern.fullmatch(...) (or another attr if specified.)
    - a string, which will be compiled into a regular expression and then tested using the above.
    - a callable, which returns True (or something evaluating as True) if the result succeeds.

    The result of whatever pattern returns is stored in event.result
    """
    if not callable(pattern):
        if isinstance(pattern, str):
            pattern = re.compile(pattern, flags)
        pattern = getattr(pattern, attr)
    return pattern
#
#
# if __name__ == '__main__':
#     dset = DependencyDict(key=lambda x: -x[0])
#
#     ct = 1000
#     import random
#     for ix in range(ct):
#         obj = ix
#         before = set()
#         after = set()
#         pending = set()
#
#         # Decide what our odds of having 'before' items are.
#         p = ix/(ct-1)
#         if random.random() > (0.85 * p):
#             after = set(random.sample(range(0, ix), random.randrange(1 + int(0.10*ix))))
#         if random.random() > (0.65 * (1-p)):
#             before = set(random.sample(range(ix+1, ct), random.randrange(1 + int(0.10*(ct - ix - 1)))))
#         dset.add(obj, before, after)
#     print(", ".join(str(x) for x in dset))
#

