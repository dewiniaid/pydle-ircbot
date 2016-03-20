"""
Defines exceptions regarding command handling.
"""
__all__ = [
    'ParseError', 'UsageError', 'PrecheckError',
    'ArgumentCountError', 'NotEnoughArgumentsError', 'TooManyArgumentsError',
]


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
        return type(self).__name__ + repr(tuple(values))

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
    def __init__(self, message=None, event=None, binding=None, param=None, final=False):
        """
        Creates a new UsageError.

        UsageErrors represent instances where a user enters invalid syntax for an IRC command.  For instance, if they
        fail to specify the correct number of parameters to a command, or enter an integer where a string is expected.

        There are two major types of UsageErrors: normal and final.

        A command can have multiple bindings to represent different subcommands.  This works by trying the bindings,
        in order, until one does not raise a UsageError.  This means that the binding in question accepted the
        specified commands as intended for it.

        If a particular UsageError is flagged as `final`, no other bindings will be tried (if there were any left).
        Use this in cases where it's very clear that the selection of this particular binding was intended, but
        something was still wrong with the parameters.

        :param message: Error message.
        :param event: The `Event` that was being handled.
        :param param: The `Binding` that triggered the error.  May be None
        :param param: The `Parameter` that triggered the error.  May be None
        :param final: If True, we're treated as 'Final' -- no other command bindings will be attempted.
        """
        super().__init__(message)
        self.event = event
        self.binding = binding
        self.param = param
        self.final = final
        self.message = message or self.default_message()

    def default_message(self):
        """Supplies a default message when our message is None on construction."""
        return None

    def __str__(self):
        if self.message:
            return self.message
        return super().__str__()


class ArgumentCountError(UsageError):
    """Thrown when we had more/less arguments than we expected."""

    def default_message(self):
        message = "Incorrect number of arguments."
        if self.binding is None:
            return message

        min = self.binding.minargs
        max = self.binding.maxargs
        if max is None:
            if min:
                expected = "at least {}".format(min)
            else:
                expected = "any number"
        elif min == max:
            expected = str(min)
        elif min:
            expected = "between {} and {}".format(min, max)
        else:
            expected = "up to {}".format(max)

        if self.event and self.event.arglist is not None:
            n = len(self.event.arglist)
            if n < min:
                message = "Not enough arguments."
            elif max is not None and n > max:
                message = "Too many arguments."
            return "{}  (Expected {}, got {})".format(message, expected, n)
        return "{}  (Expected {})".format(message, expected)


class NotEnoughArgumentsError(ArgumentCountError):
    """Thrown when we didn't have enough arguments.  `param` points to the first parameter that was unfilled."""
    pass


class TooManyArgumentsError(ArgumentCountError):
    """Thrown when we we had too many arguments.  `param` will usually be `None`."""
    pass


class PrecheckError(UsageError):
    """Thrown when the precheck condition fails."""

    def default_message(self):
        return "This command is not available here."
