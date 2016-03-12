import re


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