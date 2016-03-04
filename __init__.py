"""
IRC Bot extensions for Pydle.

This package adds a Pydle subclass optimized towards operating IRC bots, including a simplified means of defining
command syntax.
"""
import collections
import configparser
import contextlib
import functools
import re
import sys
import textwrap
import threading
import traceback

import pydle

import ircbot.commands
import ircbot.usertrack
from ircbot.util import Throttle


class ConfigSection(dict):
    """
    Represents a ConfigSection

    Subclass this and override read() to perform your own config file validation.  Override write() to allow saving
    of settings

    Allows attribute-based dict access.
    """
    def __init__(self, section=None):
        """
        Initializes ourself based on a :class:`configparser.SectionProxy`

        :param section: :class:`configparser.SectionProxy` to initialize ourselves with.
        """
        super().__init__()
        self.read(section)

    def read(self, section):
        """
        Converts, initializes and validates our parameters.

        :param section: :class:`configparser.SectionProxy` to initialize ourselves with.
        """
        return True

    # noinspection PyMethodMayBeStatic
    def write(self, section):
        """
        (Possibly) updates the specified configsection to match us.

        :param section: A :class:`configparser.SectionProxy` to modify
        """
        pass

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)

    def __delattr__(self, item):
        try:
            return super().__delattr__(item)
        except AttributeError:
            pass
        try:
            del self[item]
        except KeyError:
            raise AttributeError(item)

    __setattr__ = dict.__setitem__


class MainConfigSection(ConfigSection):
    """
    Handles main bot configuration
    """

    # noinspection PyAttributeOutsideInit
    def read(self, section):
        self.nicknames = re.split(r'[\s+,]', section.get('nick', '').strip())
        self.verify_ssl = section.getboolean('verify_ssl', True)
        self.realname = section.get('realname', self.nicknames[0])
        self.username = section.get('username', self.nicknames[0])
        self.prefix = re.compile(section.get('prefix', '!'))

        self.burst = section.getint('burst', 5)
        self.rate = section.getfloat('rate', 0.5)
        self.channel_burst = section.getint('channel_burst', 0)
        self.channel_rate = section.getfloat('channel_rate', 0)
        self.user_burst = section.getint('user_burst', 3)
        self.user_rate = section.getfloat('user_rate', 1.0)
        self.wrap_length = section.getint('wrap_length', 400)
        self.wrap_indent = section.get('wrap_indent', '...')

        servers = []
        for server in re.split(r',+', section.get('server', '')):
            server = server.strip()
            if not server:
                continue
            d = {'port': '6667'}
            d.update(zip(('hostname', 'port'), re.split(r'[/:]', server, 1)))
            d['tls'] = (d['port'][0] == '+')
            d['port'] = int(d['port'])
            servers.append(d)
        self.servers = servers

        channels = []
        for channel in re.split(r',+', section.get('channels', '')):
            channel = channel.strip()
            if not channel:
                continue
            d = {'password': None}
            d.update(zip(('channel', 'password'), channel.split('=', 1)))
            channels.append(d)
        self.channels = channels

        for attr in (
            'auth_method', 'auth_username', 'auth_password',
            'tls_client_cert', 'tls_client_cert_key', 'tls_client_cert_password'
        ):
            self[attr] = section.get(attr)


class Config:
    """
    Handles configuration, and is a wrapper around a :class:`configparser.ConfigParser`.
    """
    sections = {}

    def __init__(self, filename=None, data=None):
        """
        Creates a new Configuration.

        :param filename: Filename to load from using read_file()
        :param data: Dict or str to load from using read_data()
        :return:
        """
        self._parser = configparser.ConfigParser()
        if data:
            self.read_data(data)
        if filename:
            self.read_file(filename)

        self.section('main', MainConfigSection)

    def section(self, name, class_=None):
        """
        Registers the specified class as a handler for the specified config section.  Ignored if the section is already
        handled.

        :param name: Config section name.
        :param class_: Class.  If None, returns a decorator.
        """
        if class_ is None:
            return functools.partial(self.section, name)
        if name not in self.sections:
            self.sections[name] = class_(self._parser[name])
        return class_

    def read_file(self, filename):
        """
        Reads configuration from the specified ini file

        :param filename: Filename to read
        """
        self._parser.read(filename)

    def read_data(self, data):
        """
        Reads configuration from the specified dict or str

        :param data: String (with INI file syntax) or dict consisting of data to read
        """
        if isinstance(data, str):
            self._parser.read_string(data)
        elif isinstance(data, dict):
            self._parser.read_string(data)

    def __getattr__(self, item):
        try:
            return self.sections[item]
        except KeyError:
            raise AttributeError(item)

    def __getitem__(self, item):
        return self.sections[item]


class EventEmitter(pydle.Client):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.events = collections.defaultdict(list)

    def emit(self, _event, *args, **kwargs):
        """
        Triggers the specified event.

        :param _event: Event to trigger
        :param args: Event arguments
        :param kwargs: Event kwargs
        """
        if _event not in self.events:
            pass
        for fn in self.events[_event]:
            self.eventloop.schedule(fn, *args, **kwargs)

    def emit_in(self, _when, _event, *args, **kwargs):
        """
        Triggers the specified event.

        :param _when: When to trigger the event
        :param _event: Event to trigger
        :param args: Event arguments
        :param kwargs: Event kwargs
        :returns: An event handle
        """
        return self.eventloop.schedule_in(_when, self.emit, _event, *args, **kwargs)

    def emit_periodically(self, _interval, _event, *args, **kwargs):
        """
        Triggers the specified event.

        :param _interval: Event interval
        :param _event: Event to trigger
        :param args: Event arguments
        :param kwargs: Event kwargs
        :returns: An event handle
        """
        return self.eventloop.schedule_periodically(_interval, self.emit, _event, *args, **kwargs)

    def on_raw(self, message):
        """
        Trigger events for raw messages.

        :param message: Raw IRC message.
        """
        self.emit('raw', message)
        # noinspection PyProtectedMember
        if message._valid:
            if isinstance(message.command, int):
                cmd = str(message.command).zfill(3)
            else:
                cmd = message.command
            self.emit('raw_' + cmd.lower())
        return super().on_raw(message)

    def on(self, event, fn=None, prepend=False):
        """
        Calls fn upon the specified event.  If fn is None, returns a decorator

        :param event: Event name.
        :param fn: Function to call.  If None, returns a decorator
        :param prepend: If True, adds to the beginning of the list rather than the end.
        :returns: Decorator or `fn`
        """
        if fn is None:
            return functools.partial(self.on, event, prepend=prepend)
        if prepend:
            self.events[event].insert(0, fn)
        else:
            self.events[event].append(fn)


def _add_emitter(attr):
    fn = getattr(EventEmitter, attr)
    if not callable(fn):
        return
    event = attr[3:]

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        rv = fn(self, *args, **kwargs)
        self.emit(event, *args, **kwargs)
        return rv
    setattr(EventEmitter, attr, wrapper)

for _attr in filter(lambda x: x.startswith('on_') and not x.startswith('on_raw_') and x != 'on_raw', dir(EventEmitter)):
    _add_emitter(_attr)
del _add_emitter


class Bot(pydle.featurize(EventEmitter, ircbot.usertrack.UserTrackingClient)):
    def __init__(self, config=None, filename=None, data=None, **kwargs):
        """
        Creates a new Bot.

        :param config: Configuration object.
        :param filename: Filename to load config from.  Ignored if `config` is not None.
        :param data: Data to load config from.  Ignored if `config` is not None.
        :param kwargs: Keyword arguments passed to superclass.  Overrides config if there is a conflict.
        """
        self.server_index = -1

        if config is None:
            config = Config(filename=filename, data=data)
        self.config = config
        main = self.config['main']

        self.event_factory = kwargs.pop('event_factory', Event)

        kwargs.setdefault('nickname', main.nicknames[0])
        kwargs.setdefault('fallback_nicknames', main.nicknames[1:])
        for attr in (
            'tls_client_cert', 'tls_client_cert_key', 'tls_client_cert_password',
            'username', 'realname'
        ):
            kwargs.setdefault(attr, getattr(main, attr))

        self.command_registry = commands.Registry(prefix=main.prefix)
        super().__init__(**kwargs)
        self.events = collections.defaultdict(list)
        self.global_throttle = Throttle(main.burst, main.rate)
        self.target_throttles = {}
        self.throttle_lock = threading.RLock()
        self.rules = []

        self.textwrapper = textwrap.TextWrapper(
            width=main.wrap_length, subsequent_indent=main.wrap_indent,
            replace_whitespace=False, tabsize=4, drop_whitespace=True
        )

        self.data = {}

    @contextlib.contextmanager
    def log_exceptions(self, target=None):
        """
        Log exceptions rather than allowing them to raise.  Contextmanager.

        :param target: If specified, the bot will also notice(target, str(exception)) if an exception is raised.

        Usage::

            with bot.log_exceptions("Adminuser"):
                raise ValueError("oh no!")
        """
        # TODO: Log to a configurable file rather than just stderr.
        try:
            yield None
        except Exception as ex:
            traceback.print_exc(file=sys.stderr)
            if target:
                self.notice(target, str(ex))

    def wraptext(self, text):
        return self.textwrapper.wrap(text)

    def connect(self, hostname=None, **kwargs):
        """
        Overrides the superclass's connect() to allow rotating between multiple servers if hostname is None.

        :param hostname: Passed to superclass.
        :param kwargs: Passed to superclass
        """
        kwargs['hostname'] = hostname
        if hostname is None and self.config.main.servers:
            self.server_index += 1
            if self.server_index >= len(self.config.main.servers):
                self.server_index = 0
            kwargs.update(self.config.main.servers[self.server_index])
        return super().connect(**kwargs)

    def on_connect(self):
        """
        Attempt to join channels on connect.
        """
        super().on_connect()
        for channel in self.config.main.channels:
            try:
                self.join(**channel)
            except pydle.AlreadyInChannel:
                pass

    def rule(self, pattern, fn=None):
        """
        Calls the decorated function if a message matches 'pattern'
        :param pattern: String or regex
        :param fn: Function to call.  Returns decorator if None.
        :return: fn or decorator
        """
        if fn is None:
            return functools.partial(self.rule, pattern)
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        self.rules.append((pattern, fn))
        return fn

    def command(self, *args, **kwargs):
        """
        Same as :decorator:`ircbot.commands.command`, but using our command registry by default.

        :param args: Passed to decorator
        :param kwargs: Passed to decorator
        :return: fn
        """
        kwargs.setdefault('registry', self.command_registry)
        return ircbot.commands.command(*args, **kwargs)

    def throttled(self, target, fn, cost=1):
        """
        Adds a throttled event.  Or calls it now if it makes sense to.

        :param target: Event target nickname or channel.  May be None for a global event
        :param fn: Function to queue or call
        :param cost: Event cost.
        """
        # noinspection PyShadowingNames
        def _tick_event(throttle, lock=None):
            """Tick event helper"""
            with lock if lock else contextlib.ExitStack():
                throttle.handle = None
                t = throttle.tick()
                while t is not None and t < throttle.ZEROTIME:
                    t = throttle.tick()
                if t is not None:
                    throttle.handle = self.eventloop.schedule_in(t, _tick_event, throttle, lock)

        def _onclear(k):
            """Callback for on_clear to free up dict space"""
            if k in self.target_throttles:
                del self.target_throttles[k]

        if target is None:
            if not self.config.main.rate:
                fn()
                return
            with self.throttle_lock:
                self.global_throttle.add((cost, fn))
                if self.global_throttle.handle is None:
                    _tick_event(self.global_throttle, self.throttle_lock)
            return

        # If we're still here, we have a nick or a channel
        fn = functools.partial(self.throttled, None, fn, cost)
        with self.throttle_lock:
            throttle = self.target_throttles.get(target)
            if throttle is None:
                is_channel = self.is_channel(target)
                if is_channel:
                    burst, rate = self.config.main.channel_burst, self.config.main.channel_rate
                else:
                    burst, rate = self.config.main.user_burst, self.config.main.user_rate
                if not rate:
                    fn()
                    return
                throttle = Throttle(burst, rate, on_clear=functools.partial(_onclear, target))
            throttle.add(fn)
            if throttle.handle is None:
                _tick_event(throttle, self.throttle_lock)

    def _msgwrapper(self, parent, target, message, wrap=True, throttle=True, cost=1):
        if wrap:
            message = "\n".join(self.wraptext(message))
        for line in message.replace('\r', '').split('\n'):
            if throttle:
                self.throttled(target, functools.partial(parent, target, line), cost)
            else:
                target(parent, target, line)

    # Override the builtin message() and notice() methods to allow for throttling and our own wordwrap methods.
    def message(self, target, message, wrap=True, throttle=True, cost=1):
        """
        Sends a PRIVMSG

        :param target: Recipient
        :param message: Message text.  May contain newlines, which will be split into multiple messages.
        :param wrap: If True, text will be wordwrapped.
        :param throttle: If True, messaging will be throttled.
        :param cost: If throttled, the cost per message.
        """
        self._msgwrapper(super().message, target, message, wrap, throttle, cost)

    def notice(self, target, message, wrap=True, throttle=True, cost=1):
        """
        Sends a NOTICE

        :param target: Recipient
        :param message: Message text.  May contain newlines, which will be split into multiple messages.
        :param wrap: If True, text will be wordwrapped.
        :param throttle: If True, messaging will be throttled.
        :param cost: If throttled, the cost per message.
        """
        self._msgwrapper(super().notice, target, message, wrap, throttle, cost)

    # Convenience functions for bot events
    say = message

    def reply(self, target, message, reply_to=None, *args, **kwargs):
        """
        Same as :meth:`say`, but potentially prepending user name(s).

        :param target: Recipient
        :param message: Message text.  May contain newlines, which will be split into multiple messages.
        :param reply_to: A nickname (or sequence of nicknames) to address when sending the message.  Leaving this at
            `None` makes this identical to say().  Names are not prepended in private messages.
        :param args: Passed to :meth:`say`
        :param kwargs: Passed to :meth:`say`
        """
        if reply_to is not None and self.is_channel(target):
            if not isinstance(reply_to, str):
                reply_to = ", ".join(reply_to)
            message = reply_to + ": " + message
        return self.message(target, message, *args, **kwargs)

    @pydle.coroutine
    def handle_message(self, irc_command, target, nick, message):
        """
        Handles incoming messages
        :param irc_command: PRIVMSG or NOTICE
        :param target: Message target
        :param nick: Nickname of sender
        :param message: Messge
        :return:
        """
        super().on_message(target, nick, message)
        channel = target if self.is_channel(target) else None
        factory = functools.partial(
            self.event_factory, bot=self, irc_command=irc_command, nick=nick, channel=channel, message=message
        )

        for pattern, fn in self.rules:
            match = pattern.match(message)
            if match:
                event = factory(command=fn, text=message, match=match)
                with self.log_exceptions(target):
                    result = event()
                    if isinstance(result, pydle.Future):
                        yield result

        event = self.command_registry.parse(message, factory=factory)
        if event.command:
            with self.log_exceptions(target):
                try:
                    result = event()
                    if isinstance(result, pydle.Future):
                        yield result
                except ircbot.commands.UsageError as ex:
                    self.notice(nick, str(ex))

    def on_message(self, target, nick, message):
        return self.handle_message('PRIVMSG', target, nick, message)


def _implied_target(method):
    """
    If 'target' is specified as a keyword argument, uses it.  Otherwise, determines it from nick and channel.

    :param method: Method to wrap
    :return: Wrapped method
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        target = kwargs.pop('target', None) or self.target
        return method(self, target, *args, **kwargs)
    return wrapper


def _implied_target_user(method):
    """
    If 'target' is specified as a keyword argument, uses it.  Otherwise, determines it from nick and channel.

    :param method: Method to wrap
    :return: Wrapped method
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        target = kwargs.pop('target', None) or self.nick
        return method(self, target, *args, **kwargs)
    return wrapper


class Event(ircbot.commands.Invocation):
    """
    Passed to command and rule functions when magic happens.
    """

    def __init__(
            self,
            prefix=None, name=None, command=None, text=None,
            bot=None, irc_command=None, nick=None, channel=None, match=None, message=None
    ):
        """
        Creates a new :class:`Event`

        :param prefix: Prefix that matched.  Will be None if there was no match.
        :param name: Name of command as entered (minus prefix).  May differ from command.name
        :param command: :class:`Command` object that matched.  Will be None if there was no command match.
        :param text: Argument text that matched.
        :param bot: IRC Bot instance
        :param irc_command: IRC command name (e.g. PRIVMSG)
        :param nick: Triggering nickname
        :param channel: Triggering channel, or None for PMs.
        :param match: Triggering pattern (for rule matches)
        :param message: Full IRC message
        """
        super().__init__(prefix=prefix, name=name, command=command, text=text)
        self.bot = bot
        self.irc_command = irc_command
        self.nick = nick
        self.channel = channel
        self.match = match
        self.message = message

    @classmethod
    def from_parseresult(cls, bot, name, nick, channel, result):
        return cls(
            bot, name, nick, channel,
            prefix=result.prefix, command=result.command, arglist=result.arglist, message=result.arglist.text
        )

    @_implied_target
    def reply(self, target, message, reply_to=None, *args, **kwargs):
        """bot.reply but with a default target and reply_to, unless overridden by explicitly setting them as kwargs"""
        return self.bot.reply(target, message, reply_to or self.nick, *args, **kwargs)

    @_implied_target
    def message(self, *args, **kwargs):
        """bot.message with a default target"""
        return self.bot.message(*args, **kwargs)

    say = message

    @_implied_target_user
    def umessage(self, *args, **kwargs):
        """bot.message, but messaging the sender (not the channel) by default"""
        return self.bot.message(*args, **kwargs)

    usay = umessage

    @_implied_target
    def notice(self, *args, **kwargs):
        """bot.notice with a default target"""
        return self.bot.notice(*args, **kwargs)

    @_implied_target_user
    def unotice(self, *args, **kwargs):
        """bot.notice, but messaging the sender (not the channel) by default"""
        return self.bot.notice(*args, **kwargs)


    @_implied_target
    def action(self, *args, **kwargs):
        """bot.action with a default target"""
        return self.bot.action(*args, **kwargs)

    def whois(self, nickname=None):
        """bot.whois with an implied nickname"""
        return self.bot.whois(nickname or self.nick)

    def whowas(self, nickname=None):
        """bot.whowas with an implied nickname"""
        return self.bot.whowas(nickname or self.nick)

    # noinspection PyShadowingBuiltins
    def ban(self, channel=None, target=None, range=0):
        """bot.ban with an implied channel (if we have one) and target"""
        return self.bot.ban(channel or self.channel, target or self.target, range)

    # noinspection PyShadowingBuiltins
    def unban(self, channel=None, target=None, range=0):
        """bot.unban with an implied channel (if we have one) and target"""
        return self.bot.unban(channel or self.channel, target or self.target, range)

    def kick(self, channel=None, target=None, reason=None):
        """bot.kick with an implied channel (if we have one) and target"""
        return self.bot.kick(channel or self.channel, target or self.target, reason)

    # noinspection PyShadowingBuiltins
    def kickban(self, channel=None, target=None, reason=None, range=0):
        """bot.kickban with an implied channel (if we have one) and target"""
        return self.bot.kickban(channel or self.channel, target or self.target, reason, range)

    @property
    def target(self):
        """
        Returns the channel that invoked this (if any), otherwise the sender
        """
        return self.channel or self.nick

    def __getattr__(self, item):
        """Relay unknown attribute calls to the bot"""
        return getattr(self.bot, item)
        """
        """



