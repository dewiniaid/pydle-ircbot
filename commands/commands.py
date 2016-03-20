import functools
import inspect
import itertools
import re

import ircbot.util
from .core import Event
from .exc import *
from .bindings import Binding


class Registry:
    """
    Registers commands and serves as the intermediary between command and interface.

    :ivar aliases: Dictionary of string aliases -> command.
    :ivar patterns: :class:`DependencyDict` of pattern info
    :ivar regex: Pattern that matches commands.  Should have named groups 'prefix', 'name', and 'text'
    """

    #: Default pattern for regex matching.  %% will be replaced by the prefix regex.
    DEFAULT_PATTERN = r'(?P<prefix>!)(?P<name>\S+)(?:\s+(?P<text>.*\S)?\s*)?'

    def __init__(self, prefix=None, pattern=None, cachesize=1024):
        """
        :param prefix: Partial regular expression that matches the beginning of a command.  Will be compiled into a
            more complete regular expression.  Optional if 'match' is not specified.
        :param pattern: Regular expression that matches a line of text and breaks it into named groups "prefix", "name",
        and "text".  (At least "name" and "text" must be present.)
        :param cachesize: Size of the lookup cache for pattern-based commands.
        """
        if pattern is None:
            if prefix is None:
                prefix = '!'
            if hasattr(prefix, 'pattern'):  # Already a compiled regex
                prefix = prefix.pattern
            pattern = self.DEFAULT_PATTERN.replace("%%", prefix)
        if not hasattr(pattern, 'pattern'):
            pattern = re.compile(pattern)

        # Make sure pattern is sane.
        t = pattern.groupindex
        if 'name' not in t:
            raise TypeError("pattern must have a group named 'name'")
        if 'text' not in t:
            raise TypeError("pattern must have a group named 'text'")
        self.has_prefix = 'prefix' in t
        self.regex = pattern
        self.aliases = {}
        self.patterns = ircbot.util.DependencyDict()
        self.commands = set()

        self.pattern_lookup = self._pattern_lookup  # Replaced by the cache the first time it is invalidated.
        self._cache = 0
        self.cache = cachesize  # Which is... here.

    def register(self, *commands):
        """
        Adds one or more commands to the registry.

        :param commands: Command(s) to add.
        """
        invalidate = False
        try:
            for command in commands:
                aliases = set(alias.lower() for alias in command.aliases if not isinstance(alias, Pattern))
                if command.name:
                    aliases.add(command.name.lower())
                dupes = aliases.intersection(aliases, self.aliases.keys())
                if dupes:
                    raise ValueError("Duplicate command alias {!r}".format(dupes.pop()))
                self.aliases.update(zip(aliases, itertools.repeat(command)))
                for pattern in filter(lambda x: isinstance(x, Pattern), command.aliases):
                    invalidate = True
                    key = pattern if pattern.key is None else pattern.key
                    self.patterns.add(key, data=(pattern.pattern, command), **pattern.item_kwargs)
                self.commands.add(command)
        finally:
            if invalidate:
                self.invalidate_cache()

    @property
    def cache(self):
        return self._cache

    @cache.setter
    def cache(self, value):
        self._cache = value
        if value:
            self.pattern_lookup = functools.lru_cache(value, typed=True)(self._pattern_lookup)
        else:
            self.pattern_lookup = self._pattern_lookup

    def invalidate_cache(self):
        if hasattr(self.pattern_lookup, 'cache_clear'):
            # noinspection PyUnresolvedReferences
            self.pattern_lookup.cache_clear()

    def _pattern_lookup(self, search):
        """
        Searches for 'search' against all patterns and returns the matching command.

        :param search: Command to search for.
        :returns: A :class:`Command`, or None.
        """
        for value in self.patterns.values():
            match, command = value.data
            if match(search):
                return command
        return None

    def lookup_all(self, search):
        """
        Searches for 'search' against all registered commands.  Yields all results

        :param search: Command to search for.
        """
        search = search.lower().strip()
        command = self.aliases.get(search)
        if command:
            yield command
        yield from [command for pattern, command in self.patterns.items() if pattern.fullmatch(search)]

    def lookup(self, search):
        """
        Searches for 'search' against all registered commands.  Returns the first matching result.

        If multiple patterns match and no aliases do, the definition of 'first' matching result is undefined (unless
        the patterns are explicitly ordered)

        :param search: Command to search for.
        :returns: A :class:`Command`, or None.
        """
        search = search.lower().strip()
        command = self.aliases.get(search)
        if command:
            return command
        return self.pattern_lookup(search)

    def match(self, text):
        result = self.regex.fullmatch(text)
        if not result:
            return False
        return result.group('prefix') if self.has_prefix else None, result.group('name'), result.group('text')

    def update_event(self, match, event):
        """
        Updates an event based on our match result.

        :param match: Match result
        :param event: Event to update.
        """
        prefix, name, text = match
        command = self.lookup(name)
        event.prefix = prefix
        event.name = name
        event.command = command
        event.text = text or ''

    def parse(self, text, factory=Event, event=None):
        """
        Parses a line of text and returns a :class:`Event` representing the outcome of the parse.

        If no command is found, `Event.command` will be None.

        :param text: Text to parse
        :param factory: Factory class to use instead of Event.  Return object must have prefix, name, command and text
            attributes.
        :param event: If specified, attributes of this event are updated rather than creating a new one with factory.
        :returns: `event` or a new instance of the factory class.
        """
        if event is None:
            event = factory()
        match = self.match(text)
        if match:
            self.update_event(match, event)
        return event


class Pattern:
    """
    Represents a pattern attached to a command.
    """

    # noinspection PyShadowingNames
    def __init__(
            self, pattern, key=None, doc=None, flags=re.IGNORECASE, attr='fullmatch',
            **kwargs
    ):
        """
        Creates a new `Pattern`

        Patterns are added to commands as a way to perform regex matching or some other special logic for looking up
        commands.  For simple text matches, you should use aliases instead.

        :param pattern: Pattern definition.  Passed to :meth:`ircbot.util.patternize()`
        :param key: Optional identifier for this definition, used for ordering dependencies.
        :param doc: A pseudo-alias for this pattern for use in documentation.  Omitted from documentation if None.
        :param flags: Regex flags if a regex is compiled.  Passed to :meth:`ircbot.util.patternize()`
        :param attr: Regex object attribute.  Passed to :meth:`ircbot.util.patternize()`
        :param kwargs: Passed to :class:`DependencyItem`
        :return:
        """
        self.pattern = ircbot.util.patternize(pattern, flags, attr)
        self.key = key
        self.doc = doc
        self.item_kwargs = kwargs

    def __repr__(self):
        return "<{}({!r})>".format(type(self).__name__, self.key or self.pattern)


class Command:
    """
    Represents commands.

    In addition to constructing commands using this class, they can also be constructed using the decorator syntax with
    :func:`command`, :func:`bind`, :func:`alias`, :func:`match`, :func:`doc`.

    These are designed in such a way to account for the fact that they run 'backwards', e.g::

        @command('memo')
        @bind('action=add <message:text>')
        @bind('action=del/delete <message:text>')
        def myfunction(..., action, message):
            pass

    Despite the fact that the second ``@bind`` is called first, the binds will be in the 'logical' order of top to
    bottom.
    """
    name = None      # Command name for !help
    aliases = []     # Aliases.  (Case-insensitive string matching)
    bindings = []    # Associated command bindings, in order of priority.

    def __init__(self, name=None, aliases=None, patterns=None, bindings=None, doc=None, usage=None, category=None):
        """
        Defines a new command.

        :param name: Command name, used in helptext.  If None, uses the first alias, or the first pattern.
        :param aliases: Command aliases.
        :param patterns: Regular expression patterns.
        :param bindings: Command bindings.
        :param doc: Detailed help text.
        :param usage: Usage error text displayed when all bindings fail.
        """
        self.name = name
        self.aliases = aliases or []
        self.patterns = patterns or []
        self.bindings = bindings or []
        self.doc = doc
        self._done = False
        self.pending_functions = []  # From things being added by decorators.
        self.usage = usage
        self.category = category

    def export(self, registry=None, **kwargs):
        """
        Makes a copy of this command with updated attributes and optionally registers it.

        The copy will be partially shallow: All lists will be duplicated, but their objects will not.  Thus, updating
        a binding will update all copies, but removing a binding from a list will only affect that command's list.

        :param registry: If not-None, points to a :class:`Registry` that the new command will be registered with
        :param kwargs: Arguments to pass to the new command's constructor.  Omitted arguments will be set from the
            current command.
        :return: The new :class:`Command` object.
        """
        # Attributes we directly assign
        for attr in ('name', 'doc', 'usage', 'category'):
            kwargs.setdefault(attr, getattr(self, attr))

        # Attributes we make shallow copies of
        for attr in ('aliases', 'bindings'):
            if attr in kwargs:
                continue
            kwargs[attr] = getattr(self, attr).copy()

        done = kwargs.pop('_done', self._done)

        created = type(self)(**kwargs)
        created._done = done
        if registry:
            registry.register(created)
        return created

    def finish(self):
        """
        Called by decorators when the command is fully assembled.
        """
        if self._done:
            return
        if not self.name:
            for alias in self.aliases:
                if not isinstance(alias, Pattern):
                    self.name = alias
                    break

    def __call__(self, event, *args, **kwargs):
        """
        Calls bound functions until one returns or raises FinalUsageError.

        :param event: A :class:`Event` instance representing information we were called with.

        All arguments are passed to `Binding.__call__`
        """
        if not self.bindings:
            raise ValueError("Command has no bindings")

        error_binding, error = None, None
        for binding in self.bindings:
            try:
                return binding(event, *args, **kwargs)
            except UsageError as ex:
                if ex.final:
                    raise
                if isinstance(ex, PrecheckError):
                    continue
                if error is None or binding.is_default_error:
                    error = ex
                    error_binding = binding
        if self.usage:
            raise UsageError(self.usage)
        elif not error.message:
            raise UsageError("Usage: {command} {usage}".format(command=event.full_name, usage=error_binding.usage))
        raise error

    def __repr__(self):
        return "<{}({!r})>".format(type(self).__name__, self.name or (self.aliases[0] if self.aliases else None))

    @classmethod
    def from_pending(cls, pending, registry=None, **kwargs):
        """
        Create a new instance from a :class:`PendingCommand`

        :param pending: A PendingCommand instance.
        :param registry: If non-None, a :class:`Registry` to register the new command with.
        :param kwargs: Additional arguments to pass to constructor.  May be merged with PendingCommand arguments.
        :return: The new command
        """
        # Merge the various lists in reverse.  This allows decorators to be interpreted top-down even though they are
        # executed bottom-up.
        for attr in ('aliases', 'bindings'):
            kwargs[attr] = (kwargs.get(attr) or [])
            kwargs[attr].extend(reversed(getattr(pending, attr) or []))
        # Downright default some other attributes
        for attr in ('category', 'usage'):
            kwargs.setdefault(attr, getattr(pending, attr))
        # doc is tricky.
        doc = ircbot.util.listify(kwargs.get('doc'))
        doc.extend(reversed(pending.doc or []))
        kwargs['doc'] = "\n".join(doc)

        rv = cls(**kwargs)
        rv.finish()
        if registry:
            registry.register(rv)
        return rv

    @classmethod
    def from_chain(cls, fn, *chain):
        """
        Occasionally we want to use the decorators in a context where decorators aren't useable -- e.g. when we're
        creating a command using a `functools.partial` or something similar.

        This takes a list of decorators that haven't yet decorated a function and calls them on the selected function.
        It's essentially equivalent to::

            for item in reversed(chain):
                fn = item(fn)

        (The reason for the `reversed` bit is because the decorators already have logic to ensure they apply in top-
        down order, this counters it.)

        :param fn: Initial function that decorators receive.
        :param chain: Sequence of decorators.
        :return: Result of final command.
        """
        for decorator in reversed(chain):
            fn = decorator(fn)
        return fn

        return functools.reduce((lambda decorator, fn: decorator(fn)), itertools.chain([fn], reversed(chain)))


from_chain = Command.from_chain


class PendingCommand:
    """
    Trickery to allow decorators to return something looking like the original function.

    Should not be directly instantiated by external code.
    """
    def __init__(self, function):
        self.function = function
        # These all resemble the Command counterparts, but will be reversed upon being finalized.
        self.bindings = []
        self.aliases = []
        self.doc = []
        self.usage = None
        self.category = None

    def __call__(self, *args, **kwargs):
        return self.function(*args, **kwargs)


def wrap_decorator(fn, index=None):
    """
    Returns a version of the function wrapped in such a way as to allow both decorator and non-decorator syntax.

    If the first argument of the wrapped function is a callable, the wrapped function is called as-is.

    Otherwise, returns a decorator

    :param fn: Function to decorate.
    :param index: Argument index of the function.  Defaults to 0.
    """
    # Determine the name of the first argument, in case it is specified in kwargs instead.
    signature = inspect.signature(fn)
    arg = None
    param = next(iter(signature.parameters.values()), None)
    assert param
    if param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
        arg = param.name
    assert arg

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if (args and callable(args[0])) or (arg in kwargs and callable(kwargs[arg])):
            return fn(*args, **kwargs)

        def decorator(_fn):
            return fn(_fn, *args, **kwargs)
        return decorator
    return wrapper


def chain_decorator(fn):
    """
    The wrapped function will always receive a PendingCommand instead of a function, and will always return that same
    PendingCommand.  This allows for chaining decorators.

    If fn is not a PendingCommand, converts it to one.

    :param fn: Function to decorate.
    """
    @functools.wraps(fn)
    @wrap_decorator
    def wrapper(pending, *args, **kwargs):
        if not isinstance(pending, PendingCommand):
            pending = PendingCommand(pending)
        fn(pending, *args, **kwargs)
        return pending
    return wrapper


@wrap_decorator
def command(
    fn=None, name=None, aliases=None, bindings=None, doc=None, category=None, usage=None,
    registry=None, factory=Command, return_command=False, **kwargs
):
    """
    Command decorator.

    This must be the 'last' decorator in the chain of command construction (and thus, the first to appear when stacking
    multiple decorators)

    :param fn: Function to decorate, or a :class:`PendingCommand` instance.
    :param name: Command name.
    :param aliases: List of command aliases and patterns.
    :param bindings: Bindings.
    :param doc: Helptext.
    :param category: Category.
    :param usage: Overrides text displayed if a UsageError occurs.
    :param registry: Which :class:`CommandRegistry` the command will be registered in.  None disables registration.
    :param factory: A :class:`Command` subclass or a function that will create the new command.
    :param return_command: If True, returns the new command object rather than the wrapped function.
    :param kwargs: Passed to factory.
    :return: the new :class:`Command` object if return_command is True, otherwise fn.function or fn
    """
    if not isinstance(fn, PendingCommand):
        fn = PendingCommand(fn)

    if hasattr(factory, 'from_pending'):
        factory = factory.from_pending

    created = factory(
        fn, registry,
        name=name, category=category, usage=usage, aliases=aliases, bindings=bindings, doc=doc,
        **kwargs
    )
    if return_command:
        return created
    return fn.function


@chain_decorator
def bind(fn, paramstring='', summary=None, label=None, precheck=None, wrapper=None, function=None):
    """
    Adds a :class:`Binding` to the pending command.  See that class for details on arguments.

    :param fn: Function to decorate, or a :class:`PendingCommand` instance.
    :param paramstring: Parameter string.
    :param summary: Optional usage summary for help.
    :param label: Optional label
    :param precheck: Optional function that receives a :class:`Event` and returns True or False if the binding
        should run.
    :param wrapper: If not None, the called function will be wrapped by this one.
    :param function: Overrides fn if present.  Convenience method for decorator chaining.
    :return:
    """
    function = function or fn.function
    if wrapper:
        function = wrapper(function)

    fn.bindings.append(Binding(function, paramstring, summary, label, precheck=precheck))


@chain_decorator
def alias(fn, *aliases):
    """
    Adds one or more aliases to the pending command.

    Aliases can be strings (in which case they'll be exact string matches) or :class:`Pattern` instances.

    :param fn: Function to decorate, or a :class:`PendingCommand` instance.
    :param aliases: One or more aliases to add.
    """
    fn.aliases.extend(reversed(aliases))


@chain_decorator
def doc(fn, helptext):
    """
    Adds helptext to the pending command.

    :param fn: Function to decorate, or a :class:`PendingCommand` instance.
    :param helptext: Helptext to add.
    """
    fn.doc.append(helptext)