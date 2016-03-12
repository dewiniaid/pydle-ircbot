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

    function(bot, event, parameters_based_on_bindings...)

Bindings will be tried against the function and input parameters in such a way where the first binding that 'fits' is
the one that will be called.
"""
from .exc import *
from .core import *
from .bindings import *
from .commands import *
