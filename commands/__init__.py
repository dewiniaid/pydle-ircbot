"""
IRC command parsing tools.

This module defines several classes and utility functions related to defining IRC bot commands and parsing arguments
for them.

Argument Parsing
================
Argument parsing works on the notion of splitting a string of space-separated words, but with an easy way to take any
word and retrieve the remainder of the string that was originally parsed.

The :class:`ArgumentList` class parses a line of text and converts it into a list of :class:`Arguments <Argument>`.
Arguments are subclasses of :class:`str` that add some extra fluff -- namely, the :attr:`~Argument.eol` property which
returns from the beginning of the selected argument up through the end of the line that was parsed.  This is notably
useful for commands that take a few 'word' arguments followed by a partial line of text.

Command Binding
===============
Command binding is the process of taking a particular set of arguments (in a :class:`ArgumentList`), interpreting them,
and calling a function with said arguments.

The definition of a particular :class:`Binding` is written in a simple syntax that resembles the same sort of
text you might write as a one-line "Usage: " instruction, e.g. something like::

   Binding("set <key> <value>")

Which will call a function using function(key=word, value=word)

The :class:`Binding` interface is designed in such a way where a command ultimately can have multiple unique
bindings for various subcommands.

Commands
========
The most basic :class:`Command` decorates a function in such a way that it is called with the following signature::

    function(bot, event)

If a :class:`Command` has one or more bindings associated, the functions will instead be called as::

    function(bot, event, parameters_based_on_bindings

Bindings will be tried against the function and input parameters in such a way where the first binding that 'fits' is
the one that will be called.
"""
import re
import inspect
import collections
import functools
import itertools

import ircbot.util

__all__ = [
    'Argument', 'ArgumentList',
    'Binding', 'register_type', 'ParamType', 'ConstParamType', 'StrParamType', 'NumberParamType',
    'UsageError', 'FinalUsageError', 'ParseError',
    'Command', 'PendingCommand', 'from_chain',
    'wrap_decorator', 'chain_decorator', 'command', 'alias', 'bind', 'Pattern', 'doc'
]


_default = object()


class Argument(str):
    """
    Like a str, but with some added attributes useful in command parsing.

    Normally, Arguments should not be constructed directly but instead created in bulk from an ArgumentList.
    """
    __slots__ = ('_text', '_start', '_eol')
    # str subclassing is a pain
    def __new__(cls, s, text, span):
        rv = str.__new__(cls, s)
        return rv

    def __init__(self, s, text, start):
        """
        Constructs a new Argument

        :param s: String that we will be set to.
        :param text: Full line of text involved in the original parse.
        :param start: Tuple of (start, end) representing where we were located in the original parse.
        """
        super().__init__()
        self._text = text
        self._start = start
        self._eol = None

    @property
    def eol(self):
        """
        Returns the original string from the beginning of this Argument to the end of the line.

        Calculated on first access.
        """
        if self._eol is None:
            self._eol = self._text[self._start:]
            del self._text
            del self._start
        return self._eol


# noinspection PyShadowingNames
class ArgumentList(list):
    """
    When parsing IRC commands, it's often useful to have the input text split into words -- usually by
    re.split(r'\s+', ...) or similar.

    This allows for that, while also allowing for a way to get the remainder of the line as one solid chunk,
    unaltered by any spaces.

    Slicing an ArgumentList produces a new ArgumentList originating at the string
    """
    pattern = re.compile(r'\S+')  # Matches not-whitespace.

    def __init__(self, text):
        """
        Parse a string of text (likely said by someone on IRC) into a series of words.
        :param text: Original text.
        """
        self.text = text
        super().__init__(
            Argument(match.group(), self.text, match.start())
            for match in self.pattern.finditer(self.text)
        )


class ParseError(ValueError):
    """
    Represents an error in parsing :class:`CommandBinding` syntax.
    """
    def __init__(self, message=None, text=None, pos=None):
        """
        Creates a new :class:`ParseError`

        :param message: Error message
        :param text: Line of text where error occured.
        :param pos: Character position on line.
        :return:
        """
        self.message = message
        self.text = text
        self.pos = pos
        super().__init__(message)

    def __str__(self):
        msg = [self.message or 'Parse error']
        if self.pos:
            msg += [' at position ' + str(self.pos)]
        return "".join(msg)

    def __repr__(self):
        values = [self.message or 'Parse error', self.text, self.pos]
        while values and values[-1] is None:
            values.pop()
        return self.__class__.__name__ + repr(tuple(values))

    def show(self, maxwidth=None, after=10):
        """
        Pretty-prints out a version of the actual parse error: showing a (portion) of the text and an arrow pointing to
        the portion that failed.

        :param maxwidth: Maximum line width.  0 or none=no limit.  Must be >= 0
        :param after: If a line is trimmed to maxwidth, ensure at least this many characters after 'pos' are visible
            (assuming there's at least that many characters left in the string).  Must be >= 0

        If either of self.text or self.pos are None, this simply returns str(self).

        If the values of 'after' and 'maxwidth' are too close, 'after' is reduced to make room for at least 4 leading
        characters ("..." + the character that triggered the exception).
        :return: Formatted multiline string.
        """
        if maxwidth and maxwidth < 0:
            raise ValueError('maxwidth cannot be negative')
        if after and after < 0:
            raise ValueError('after cannot be negative')

        if self.text is None or self.pos is None:
            return str(self)
        # Replace newlines with spaces.  We shouldn't really have newlines anyways.
        text = str.replace(self.text, "\n", " ")

        # Format string
        fmt = "{text}\n{arrow}^\n{desc}"

        if maxwidth:
            diff = (maxwidth + 4) - after
            if diff < 0:
                after = min(0, after + diff)
            end = min(len(text), self.pos + after + 1)
            start = max(0, end - maxwidth)
            if start:
                text = "..." + text[start+3:end]
            else:
                text = text[:end]
            pos = self.pos - start
        else:
            pos = self.pos
        return "{text}\n{arrow}^\n{desc}".format(
            text=text, arrow='-'*pos, desc=str(self)
        )


class UsageError(Exception):
    """
    Thrown when a command is called with invalid syntax.
    """
    def __init__(self, message=None, event=None, param=None, final=False):
        """
        Creates a new UsageError.

        UsageErrors represent instances where a user enters invalid syntax for an IRC command.  For instance, if they
        fail to specify the correct number of parameters to a command, or enter an integer where a string is expected.

        There are two types of UsageErrors: normal and final.

        A command can have multiple bindings to represent different subcommands.  This works by trying the bindings,
        in order, until one does not raise a UsageError.  This means that the binding in question accepted the
        specified commands as intended for it.

        FinalUsageError is thrown when a binding thinks it was most likely intended for a command, but the parameters
        were still wrong.

        :param message: Error message.
        :param event: The Event that was being parsed.
        :param param: The Parameter that triggered the error.  May be None
        :param final: If True, we're treated as 'Final' -- no other command bindings will be attempted.
        """
        self.message = message
        self.event = event
        self.param = param
        self.final = final

    def __str__(self):
        if self.message:
            return self.message
        return super().__str__()


class RequiredParamError(UsageError):
    """Thrown when a required parameter was not present."""
    pass


class PrecheckError(UsageError):
    """Thrown when a binding's precheck function returns False."""
    def __init__(self, message=None, event=None, param=None, final=False):
        super().__init__(message or "This command is not available here.", event, param, final)


class FinalUsageError(UsageError):
    """See UsageError"""
    def __init__(self, message=None, event=None, param=None, final=True):
        super().__init__(message, event, param, final)
    pass


# noinspection PyShadowingNames
class Binding:
    """
    A given IRC command can have one or more bindings.
    A binding consists of:
    - A function to call
    - A priority (default 0)
    - A parameter string, consisting of a a space-separated list of variables, constants, and options

    If the first word of a line of text matches one of the command names, the remainder is checked against the bindings
    available for that command in order of priority.  (Bindings with the same priority are called in the order they
    were defined).

    The first binding that is matched calls its function with arguments set corresponding to contents of the
    parameter string.

    Approximate ABNF syntax for parameter strings:

    ::

        SPACE = 1*(WSP / CR / LF)

        param-string = param *(SPACE param)
        param = (*"[" (variable | constant) *"]") / option

        word = 1*(ALPHA / DIGIT)
        name = 1*WORD
        type = 1*WORD
        help = 1*WORD
        type-specifier = ":" type
        help-specifier = "?" [help]
        const-phrase-separator = "/" / "|"
        const-phrase = WORD 1*(const-separator WORD)
        constant = bare-constant / parenthesized-constant
        bare-constant = [name "="] const-phrase [help-specifier]
        parenthesized-constant = "(" bare-constant ")"

        variable = "<" name [type-specifier] [help-specifier] ">"

        option = "/" 1*ALPHA

    **Constants:**

    Constants are optionally wrapped in parenthesis.  They consist of the following, in order:
    - An optional argument name, followed by an equals sign, which will receive a lowercased version of the constant.
    - One or more words, separated by the | or / characters.  Text has to be a (case-insensitive) match to one of the
    listed words.
    - An optional helpname, consisting of a question mark (?) followed by text that should be used in place of the
    variable name in help.  This may contain spaces as long as the constant is wrapped in parenthesis.

    Examples:

    ``foo``
        Word must exactly match 'foo'
    ``action=add|delete``
        Word must be 'add' or 'delete', and the function's 'action' argument will match the word.
    ``(action=add|delete?ACTION)``
        As above, but will show as ACTION in helptext.

    When a constant is present, parsing expects an exact (case-insensitive) match to one of the words listed:

    ``foo``
        Word must be 'foo'
    ``foo|bar or foo/bar``
        Word must be 'foo' or 'bar'

    A constant may be optionally preceded with "name=".  If so, the parameter named *name* will be set to the constant
    value when calling the function.

    **Variables:**

    Variables are wrapped in angle brackets and normally correspond 1-to-1 with words in the text being processed.
    They consist of the following, in order:

    - A name, which must match the name of a function argument (unless the function has `kwargs`)
    - An optional type specifier, consisting of a colon (:) plus the name of a type.  Currently supported types are:
      - ``:line`` - Matches the rest of the line instead of the usual one word.  Should be the last parameter.
      - ``:str`` - Input coerced to string.  This is the default.
      - ``:int`` - Input coerced to integer.
    - An optional helpname, consisting of a question mark (?) followed by text that should be used in place of the
      variable name in help.  This may contain spaces.

    The variable name may also be preceded by one of the following:

    ``*``
        Produces a list of 0..*N* items.  Must be the last parameter.
    ``+``
        Produces a list of 1..*N* items.  Must be the last parameter.
    ``?``
        Optional variable.  The function's default value will be used if the variable is not present.

    **Options:**

    Options begin with a "/", and the remaining text sets various options.

    Current options are:

    b
        When invoking the bound function, pass this binding to it.  This is passed the first argument (before any
        `args`)
    B
        As 'b', but as the argument after `args`
    b=keyword
        Pass the binding as this keyword to the bound function.
    u
        f present and all bindings raise a UsageError, use this one's usage error rather than the first UsageError
        raised.

    Optional parameters:
    Each chunk may optionally begin with any number of ['s and end with any number of ]'s to indicate that they are
    optional.  This only affects the help text.  To make a particular parameter actually optional, the function being
    called need only specify a default value for it.

    :ivar label: Our label
    :ivar function: The function that we're bound to.
    :ivar signature: A `class:inspect.Signature` from the bound function
    :ivar usage: Usage helptext generated by parameters.
    :ivar params: Parameters.
    :ivar summary: Short summary of usage.
    :ivar binding_arg: If /b, /B or /b= is set, this will be set to FIRST_ARG, LAST_ARG or the kwarg-name accordingly
    :ivar default_error: True if /u is set.
    :ivar command: Command that we're linked to.
    """
    _paramstring_symbols = '[^<>()]'

    # Each group is named with 'prefix_name[_unused]
    # Prefix is used for classification.  Name is used for variable naming.  Unused exists solely as a way to avoid
    # duplicate parameter names.
    _paramstring_re = re.compile(
        r'''
        (?:
            # Options
            (?:/(?P<options>))
            # Or constant/variable
            | (?:
                (?P<prefix_0>\[*)                   # Allow any number of brackets to indicate optional sections
                (?:
                    # Parameter
                    (?:(?P<prefix_1>\<)                 # begin
                        (?P<var_count>[+*?])?           # argument count.
                        (?P<var_arg>%N+?)               # argument name
                        (?:\:(?P<var_type>%N+?))?       # optional type specifier
                        (?:\:(?P<var_options>%N+?))?    # optional type specifier
                        (?:\?(?P<var_name>%N+?))?       # optional helptext
                    (?P<suffix_1>\>))                   # end
                    | # Or constant
                    (?:(?P<prefix_2>\()                 # begin
                        (?P<const_count>[+*?])?         # argument count.
                        (?:(?P<const_arg>%N+?)=)?       # optional argument name
                        (?P<const_options>%N+?)         # constant phrase
                        (?:\?(?P<const_name>%N+?))?     # optional helptext
                    (?P<suffix_2>\)))                   # end
                    | # Or a constant w/o parenthesis
                    (?:
                        (?P<const_count_1>[+*?])?       # argument count
                        (?:(?P<const_arg_1>%N+?)=)?     # optional argument name
                        (?P<const_options_1>%N+?)       # constant phrase
                        (?:\?(?P<const_name_1>%N+?))?   # helptext
                    )
                    | # Or anything else, which is invalid
                    (?P<error_unexpected>.+?)
                )
                (?P<suffix_3>\]*)                   # Allow any number of brackets to indicate optional sections
            )
        )
        (?:\s+|\Z)                          # Some whitespace or the end of the string
        '''.replace('%N', _paramstring_symbols), re.VERBOSE
    )

    default_type = 'str'
    type_registry = {}
    FIRST_ARG = object()
    LAST_ARG = object()

    def __init__(self, function, paramstring='', summary=None, label=None, precheck=None):
        """
        Creates a new :class:`Binding`.

        :param function: The function that we bind.
        :param paramstring: The parameter string to interpret.
        :param label: Name of this binding.  Optional.
        :param summary: Optional short summary of usage.
        :param precheck: Optional function that takes a :class:`Event` and returns True if we should even be
            considered for evaluation.
        """
        self.label = label
        self.function = function
        self.signature = inspect.signature(function)
        self.summary = summary
        self.binding_arg = None
        self.default_error = False
        self.precheck = precheck or (lambda x: True)

        signature = self.signature

        kwargs_var = None
        varargs_var = None
        for data in signature.parameters.values():
            if data.kind == inspect.Parameter.VAR_KEYWORD:
                kwargs_var = data.name
            elif data.kind == inspect.Parameter.VAR_POSITIONAL:
                varargs_var = data.name

        def parse_error_here(message):
            return ParseError(message, paramstring, match.start())

        def adapt_parse_error(ex):
            if ex.message is not None or ex.pos is not None:
                raise ex
            raise parse_error_here(ex.message)

        params = []          # List of parameter structures
        self.params = params
        arg_names = set()    # Found parameter names (to avoid duplication)
        usage = []      # Usage line.  (Starts as a list, combined to a string later.)
        eol = False          # True after we've consumed a parameter that eats the remainder of the line.

        # index = -1
        was_required = True  # Was the last parameter required?  (For error handling)
        for index, match in enumerate(self._paramstring_re.finditer(paramstring)):
            options = match.group('options')
            if options is not None:
                for ix, ch in enumerate(options):
                    if ch == 'u':
                        self.default_error = True
                        continue
                    if ch in 'bB':
                        if ix+1 < len(options) and options[ix+1] == '=':
                            self.binding_arg = options[ix+2:]
                            if not self.binding_arg:
                                raise ParseError(ch + '= must specify a kwarg')
                        else:
                            self.binding_arg = self.FIRST_ARG if ch == 'b' else self.LAST_ARG
                        continue
                    raise ParseError("Unrecognized option {!r}".format(ch))
                continue
            # index += 1
            if eol:
                raise parse_error_here(
                    "Previous parameter consumes remainder of line, cannot have additional parameters."
                )
            # Parse our funky regex settings
            data = collections.defaultdict(dict)
            for key, value in match.groupdict().items():
                if value is None:
                    continue
                keytype, key, *unused = key.split("_")
                data[keytype][key] = value
            prefix = "".join(v for k, v in sorted(data.pop('prefix', {}).items(), key=lambda x: int(x[0])))
            suffix = "".join(v for k, v in sorted(data.pop('suffix', {}).items(), key=lambda x: int(x[0])))
            assert len(data) == 1, "Unexpectedly matched multiple sections of paramstring."

            paramtype, data = data.popitem()  # Should only have one key left.
            if paramtype == 'error':
                error_type = next(iter(data.values()))
                raise parse_error_here({'unexpected': "Unexpected characters"}.get(error_type, error_type))

            # paramtype will be one of 'var' or 'const'
            # data will consist of:
            # arg (default to None)
            # type ('const' if paramtype == 'str', else default to 'str')
            # options (default to None)
            # name (default to arg)
            arg = data.get('arg') or None
            options = data.get('options')
            name = data.get('name')
            count = data.get('count', '')
            required = True
            if required and not was_required:
                raise parse_error_here("Cannot have mandatory arguments after optional ones.")

            if paramtype == 'const':
                type_ = 'const'
                name = name or options
            else:
                type_ = data.get('type') or self.default_type
                name = name or arg

            if count and count in '+*':
                listmode = Parameter.LIST_NORMAL  # we might override this in a moment, but that's fine.
            else:
                listmode = Parameter.LIST_NONE
            required = not count or count not in '?*'

            if arg:
                # if arg[-1] in '+*':
                #     listmode = Parameter.LIST_NORMAL  # we might override this in a moment that's fine.
                #     required = (arg[-1] == '+')
                #     arg = arg[:-1]
                if arg in arg_names:
                    raise parse_error_here("Duplicate parameter name '{!r}'".format(arg))
                if arg == varargs_var:
                    listmode = Parameter.LIST_VARARGS
                if arg == kwargs_var:
                    raise parse_error_here("Cannot directly reference a function's kwargs argument.")
                if arg not in signature.parameters:
                    if not kwargs_var:
                        raise parse_error_here("Bound function has no argument named '{!r}'".format(arg))
                    # required = False
                elif not listmode:
                    # required = (signature.parameters[arg].default is inspect.Parameter.empty)
                    pass
                arg_names.add(arg)
            try:
                param = Parameter(self, index, arg, type_, options, name, listmode, required)
                eol = listmode or param.eol
                params.append(param)
            except ParseError as ex:
                raise adapt_parse_error(ex)
            usage.append(prefix + name + ("..." if listmode else "") + suffix)

        self.usage = " ".join(usage)

    @classmethod
    def register_type(cls, class_, typename=_default, *args, **kwargs):
        """
        Registers a subclass of :class:`ParamType` as a type handler to match cases of <param:typename>

        :param class_: :class:`ParamType` subclass that will handle this type.  If None, returns a decorator.  If this
            is not a callable, it is treated as the value of 'typename' instead and a decorator is returned.
        :param typename: Name of the type
        :param args: Passed to _class's constructor after 'options'.
        :param kwargs: Passed to _class's constructor after 'options'.
        """
        if class_ is not None and not callable(class_):
            if typename is not _default:
                args = [typename] + list(args)
            typename = class_
            class_ = None

        def decorator(class_):
            if typename in cls.type_registry:
                raise ValueError("Type handler {!r} is already registered".format(typename))
            cls.type_registry[typename] = (class_, args, kwargs)
            return class_

        return decorator if class_ is None else decorator(class_)

    def bind(self, event, *args, **kwargs):
        """
        Binds the information in input_string
        :param event: An :class:`Event`
        :param args: Initial arguments to include in binding.
        :param kwargs: Initial keyword arguments to include in binding.
        :return: Outcome of signature.Bind()
        """
        if not self.precheck(event):
            raise PrecheckError()

        if self.binding_arg:
            if self.binding_arg == self.FIRST_ARG:
                args = [self] + list(args)
            elif self.binding_arg == self.LAST_ARG:
                args = list(args) + [self]
            else:
                kwargs[self.binding_arg] = self
        if (
            len(event.arglist) > len(self.params) and
            (not self.params or not (self.params[-1].eol or self.params[-1].listmode))
        ):
            raise UsageError("Incorrect number of arguments.")

        for param in self.params:
            param.validate(event)

        for param in self.params:
            param.bind(event, args, kwargs)
            # Did we get too many arguments?

        return self.signature.bind(event, *args, **kwargs)

    def __call__(self, event, *args, **kwargs):
        """
        Calls the bound function.

        Equivalent to ``bound = binding.bind(...); binding.function(*bound.args, **bound.kwargs))``

        :param arglist: An :class:`ArgumentList` cosisting of the arguments we wish to bind.
        :param args: Initial arguments to include in binding.
        :param kwargs: Initial keyword arguments to include in binding.
        :return: Result of function call.
        """
        bound = self.bind(event, *args, **kwargs)
        event.binding = self
        return self.function(*bound.args, **bound.kwargs)


register_type = Binding.register_type


class Parameter:
    LIST_NONE = 0
    LIST_NORMAL = 1
    LIST_VARARGS = 2

    def __init__(self, parent, index, arg, type_=None, options=None, name=None, listmode=LIST_NONE, required=True):
        """
        Defines a new parameter

        :param parent: Parent :class:`Binding`.
        :param index: Index in list
        :param arg: Function argument name.  May be None in some circumstances.
        :param type_: Parameter type
        :param options: Parameter type options.
        :param name: Helpname.  Defaults to 'arg' if not set.
        :param listmode: LIST_NONE(default) if this is a single argument.  LIST_NORMAL if this is a list of arguments.
            LIST_VARARGS if this is a function's args
        :param required: True if this parameter is required.  For lists, this means it must match 1+ items instead of
            0+ items.
        """
        if name is None:
            name = arg

        self.index = index
        self.parent = parent
        self.arg = arg
        self.type_ = type_
        self.options = options
        self.name = name
        self.listmode = listmode
        self.required = required

        parser_class, args, kwargs = self.parent.type_registry[self.type_]
        self.parser = parser_class(self, *args, **kwargs)
        self.eol = self.parser.eol

    def args(self, arglist):
        """
        Yields arguments from arglist that we are directly responsible for.  Helper function.

        :param arglist: A :class:`ArgumentList`
        """
        for index in range(self.index, len(arglist)):
            yield arglist[index]
            if not self.listmode:
                return

    def validate(self, event):
        """
        Performs validation of arguments in the event's `ArgumentList` that belong to us.

        Gives ParamType handlers a chance to raise a UsageError() before actually invoking the function involved.

        :param event: A :class:`Event`
        """
        if len(event.arglist) <= self.index:
            if self.required:
                if self.listmode:
                    raise UsageError("At least one {name} must be specified".format(name=self.name or '<const>'))
                raise UsageError("{name} must be specified".format(name=self.name or '<const>'))
            return
        if not self.parser.check:
            return
        for index, arg in enumerate(self.args(event.arglist), self.index):
            try:
                self.parser.validate(event, arg.eol if self.parser.eol else arg)
            except UsageError as ex:
                raise UsageError(ex.message, event.arglist, index)
            except Exception as ex:
                if self.parser.wrap_exceptions:
                    raise UsageError("Invalid format for {name}") from ex
                raise

    def bind(self, event, args, kwargs):
        """
        Updates the args and kwargs that we'll use to call the bound function based on what the ParamType handler says

        :param event: A :class:`Event`
        :param args: Initial arguments to pass to function
        :param kwargs: Additional keyword arguments to pass to function
        """
        values = []
        for index, arg in enumerate(self.args(event.arglist), self.index):
            try:
                values.append(self.parser.parse(event, arg.eol if self.parser.eol else arg))
            except UsageError as ex:
                raise UsageError(ex.message, event.arglist, index)
            except Exception as ex:
                if self.parser.wrap_exceptions:
                    raise UsageError("Invalid format for {}".format(self.name)) from ex
                raise
        if not self.arg:
            # If we don't have an argument name, we don't want to update anything.  However, it's still necessary to
            # do all of the above work in case it would throw a UsageError.
            return
        if not values:
            return
        if not self.listmode:
            kwargs[self.arg] = values[0]
            return
        if self.listmode == self.LIST_VARARGS:
            args.extend(values)
            return
        kwargs[self.arg] = values


class ParamType:
    """
    Defines logic and rules behind argument parsing.

    There's three passes to argument parsing:

    1. At bind time, a `ParamType` instance is created.  It receives any options specified as its first argument.

    2. Before calling a function, any arguments that have :attr:`check` = *True* have their validate method called.  If
       any of them raise the function will not be called and the system will continue to the next binding (if one
       exists)

    3. Before calling a function, all arguments have their :meth:`parse` method called, and the result is assigned to
       one of the function arguments.  If any of these raise, the function will not be called. and the system will
       continue to the next binding (if one exists)

    :ivar eol: If True, this argument type consumes the entire line instead of just one word.
    :ivar wrap_exceptions: If True, all non-UsageError exceptions from cls.parse() are re-raised as UsageErrors.
    :ivar check: If True, call validate() before parse().  Use this when the parsing may have side effects or
        would be otherwise expensive, but simple validation is easy.
    """
    eol = False
    wrap_exceptions = True
    check = False

    def __init__(self, param):
        """
        Created as part of the CommandBinding process.  Should raise a ParseError if invalid.

        :param param: Parameter we are attached to.
        """
        self.param = param

    def parse(self, event, value):
        """
        Parses the incoming string and returns the parsed result.

        :param event: `Event` being parsed.
        :param value: Value to parse.
        :return: Parsed result.
        """
        return value

    def validate(self, event, value):
        """
        Preparses the incoming string and raises an exception if it fails early validation.

        :param event: Event being parsed.
        :param value: Value to parse.
        :return: Nothing
        """
        return


@Binding.register_type('str')
@Binding.register_type('line', eol=True)
class StrParamType(ParamType):
    def __init__(self, param, eol=False):
        """
        String arguments are the simplest arguments, and usually return their input.

        :param param: Parameter we are bound to.

        If param.options is equal to 'lower' or 'upper', the corresponding method is called on the string before it is
        returned.
        """
        super().__init__(param)
        self.eol = eol
        if param.options and param.options in ('lower', 'upper'):
            self.parsefn = getattr(str, param.options)
        else:
            self.parsefn = str

    def parse(self, event, value):
        return self.parsefn(value)


@Binding.register_type('const')
class ConstParamType(ParamType):
    check = True

    def __init__(self, param, split=re.compile('[/|]').split):
        """
        Const arguments require that an argument be a case-insensitive exact match for one of the provided inputs.

        :param param: Parameter we are bound to.
        :param split: Function that splits constant value string into an iterable of allowed values.

        param.options dictates allowed constant values, as a string
        """
        super().__init__(param)
        self.values = set(split(param.options.lower()))
        if not self.values:
            raise ParseError("Must have at least one constant value.")

    def validate(self, event, value):
        if value not in self.values:
            if len(self.values) == 1:
                fmt = "{name} must equal {values}"
            else:
                fmt = "{name} must be one of ({values})"
            raise UsageError(fmt.format(name=self.param.name, values=", ".join(self.values)))


@Binding.register_type('int', coerce=int, coerce_error="{name} must be an integer")
@Binding.register_type('float', coerce=float, coerce_error="{name} must be a number")
class NumberParamType(ParamType):
    def __init__(self, param, coerce=int, coerce_error=None):
        """
        Number arguments require that their data be a number, potentially within a set range.

        :param param: Parameter we are bound to.
        :param coerce: Function that coerces 'min', 'max' and the input to integers.
        :param coerce_error: Error message for coercion failures.

        param.options may contain a string in the form of 'min..max' or 'min' that determines the lower and upper
        bounds for this argument.  if either 'min' or 'max' are blank (as opposed to 0), the range is treated as
        unbound at that end.
        """
        super().__init__(param)
        self.coerce = coerce
        self.coerce_error = coerce_error

        minvalue, _, maxvalue = param.options.partition('..')
        try:
            self.minvalue = coerce(minvalue) if minvalue else None
        except Exception as ex:
            raise ParseError("Unable to coerce minval using {!r}".format(coerce))

        try:
            self.maxvalue = coerce(maxvalue) if maxvalue else None
        except Exception as ex:
            raise ParseError("Unable to coerce maxval using {!r}".format(coerce))

        if self.minvalue is not None and self.maxvalue is not None and self.minvalue > self.maxvalue:
            raise ParseError("minval > maxval")

    def parse(self, event, value):
        try:
            value = self.coerce(value)
        except Exception as ex:
            if self.coerce_error:
                raise UsageError(self.coerce_error.format(name=self.param.name)) from ex
            raise

        if self.minvalue is not None and value < self.minvalue:
            if self.maxvalue is not None:
                raise UsageError(
                    "{0.param.name} must be between {0.minvalue} and {0.maxvalue}"
                    .format(self)
                )
            raise UsageError(
                "{0.param.name} must be >= {0.minvalue}"
                .format(self)
            )
        if self.maxvalue is not None and value > self.maxvalue:
            raise UsageError(
                "{0.param.name} must be <= {0.minvalue}"
                .format(self)
            )


# noinspection PyShadowingNames
class Event:
    """Stores the result from :meth:`Registry.parse`, and includes data passed to commands and bindings."""
    def __init__(self, prefix=None, name=None, command=None, text=None):
        """
        Creates a new :class:`Event`

        :param prefix: Prefix that matched.  Will be None if there was no match.
        :param name: Name of command as entered (minus prefix).  May differ from command.name
        :param command: :class:`Command` object that matched.  Will be None if there was no command match.
        :param text: Argument text that matched.
        """
        self.prefix = prefix
        self.name = name
        self.command = command
        self.text = text
        self._arglist = None
        self.binding = None

    @property
    def full_name(self):
        """Returns the full command name used.  (Essentially prefix + command)"""
        return self.prefix + self.name

    def __bool__(self):
        """Returns True if `self.command` is not None"""
        return self.command is not None

    @property
    def arglist(self):
        """
        Returns the :class:`ArgumentList` in this result.  Computed on first use.

        :raises: :class:`ValueError` if self.text is None and thus no :class:`ArgumentList` can be constructed.
        """
        if self._arglist is None:
            if self.text is None:
                raise ValueError("Cannot parse arglist: no text available.")
            self._arglist = ArgumentList(self.text)
        return self._arglist


# noinspection PyShadowingNames
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
        return "<{}({!r})>".format(self.__class__.__name__, self.key or self.pattern)


# noinspection PyShadowingNames
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

        created = self.__class__(**kwargs)
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
            except FinalUsageError:
                raise
            except PrecheckError:
                pass
            except UsageError as ex:
                if error is None or binding.default_error:
                    error = ex
                    error_binding = binding
        if self.usage:
            raise UsageError(self.usage)
        elif not error.message:
            raise UsageError("Usage: {command} {usage}".format(command=event.full_name, usage=error_binding.usage))
        raise error

    def __repr__(self):
        return "<{}({!r})>".format(self.__class__.__name__, self.name or (self.aliases[0] if self.aliases else None))

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


# noinspection PyShadowingNames
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