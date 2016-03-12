"""
Core bot functionality.

To add commands in this module with their default settings, simply register them.

To override settings, use command.export(...) or set the appropriate class attributes before registering them.

`help_command`
    Adds ``!help.``  Common options are:

    ``allow_full=True``
        Allow !help full (which can be very spammy with a lot of commands loaded)
    ``reply=default_reply``
        Function that takes an event and a message and produces the appropriate reply.  The default implementation
        sends a NOTICE to the target nick.
"""
from ircbot.commands import Command, command, alias, bind, doc
from ircbot.commands.commands import Pattern, Command, command, bind, alias, doc
import functools
import itertools
import textwrap


def default_reply(event, message):
    """Default function called to reply to bot commands."""
    return event.unotice(message)


class HelpCommand(Command):
    def __init__(self, *args, reply=default_reply, allow_full=True, command_filter=None, **kwargs):
        """
        Specialized HelpCommand data.

        :param args: Passed to superclass
        :param reply: Function that will be called for all help reply messages.
        :param allow_full: If TRUE, allows !help full which shows usage for all commands.  Spammy.
        :param command_filter: If not-None, a callable that receives a command and returns True if it should be
            included in health.
        :param kwargs: Passed to superclass.
        :return:
        """
        self.reply = reply
        self.allow_full = allow_full
        self.command_filter = command_filter
        super().__init__(*args, **kwargs)

        # Remove the binding that doesn't match our allow_fullness.
        toremove = 'nofull' if allow_full else 'full'
        self.bindings = list(filter(lambda x: x.label != toremove, self.bindings))


@command('help', factory=HelpCommand, return_command=True, allow_full=True)
@bind('', 'Shows a list of commands.', label="nofull")
@bind('[?full=FULL]', 'Shows a list of commands.  FULL shows usage for all commands.', label="full")
@bind('<name?command>', 'Shows detailed help on one command.')
def help_command(event, name=None, full=False):
    """
    Produces help.
    :param event: Event
    :param name: Optional command name to search for.
    """
    registry = event.bot.command_registry
    reply = functools.partial(event.command.reply, event)
    if event.command.command_filter:
        command_filter = lambda x: x and x.name and event.bot.command_filter(x)
    else:
        command_filter = lambda x: x and x.name is not None

    def usage_lines(c, name):
        for ix, binding in enumerate(c.bindings):
            fmt = "{name} {binding.usage}"
            if binding.summary:
                fmt += " -- {binding.summary}"
            yield fmt.format(name=name, binding=binding)

    if name:
        search = registry.parse(name)
        if search.command and search.command.name:
            command = search.command
            name = search.full_name
        else:
            command = registry.lookup(name)
            name = event.prefix + name
        if not command_filter(command):
            reply(
                "Unknown command {name}.  See {help_command} for a complete list of commands."
                .format(name=name, help_command=event.full_name)
            )
            return
        usage_string = "Usage: "
        for index, line in enumerate(usage_lines(command, name)):
            reply(((' ' * len(usage_string)) if index else usage_string) + line)

        aliases = []
        for alias in command.aliases:
            if isinstance(alias, Pattern):
                alias = alias.doc
            if alias and alias != command.name:
                aliases.append(event.prefix + alias)
        if aliases:
            reply("Aliases: " + ", ".join(aliases))

        # We don't show patterns for now, since that gets messy real quick.

        if command.doc:
            reply(command.doc)
        return

    # Build a wordwrapper for formatting the command list.
    ww = textwrap.TextWrapper(
        width=80, subsequent_indent="... "
    ).wrap

    # Build unique list of commands
    commands = set(filter(command_filter, registry.commands))

    reply("For detailed help on a specific command, use {} <command>".format(event.full_name))
    # Sort it and group by category
    for category, commandlist in itertools.groupby(
        sorted(commands, key=lambda item: ((item.category or "").lower(), item.name)),
        key=lambda item: (item.category or "").lower()
    ):
        if full:
            for command in commandlist:
                for line in usage_lines(command, event.prefix + command.name):
                    reply(line)
            continue

        fmt = ("[{category}]: " if category else "") + "{commands}"
        for line in ww(fmt.format(
            category=category.upper(),
            commands=", ".join(command.name for command in commandlist))
        ):
            reply(line)
