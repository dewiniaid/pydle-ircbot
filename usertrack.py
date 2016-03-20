"""Improved usertracking capabilities."""
import logging
import pydle
from pydle.features.rfc1459 import protocol, parsing
from pydle.async import Future, parallel

import pydle.features

logger = logging.getLogger(__name__)
# Portions of UserTrackingClient are directly derived from code in Pydle and thus are subject to its license.
# The Pydle license and related information can be found in LICENSE.md


class UserTrackingClient(pydle.Client):
    def _reset_attributes(self):
        # Work around framework bugs.
        super()._reset_attributes()
        self._pending['whois'] = parsing.NormalizingDict(self._pending['whois'], case_mapping=self._case_mapping)
        self._pending['whowas'] = parsing.NormalizingDict(self._pending['whowas'], case_mapping=self._case_mapping)
        self._whois_info = parsing.NormalizingDict(self._whois_info, case_mapping=self._case_mapping)
        self._whowas_info = parsing.NormalizingDict(self._whowas_info, case_mapping=self._case_mapping)

    def on_user_create(self, user, nickname):
        """Called when a user is created."""
        pass

    def on_user_delete(self, user, nickname):
        """Called when a user is destroyed."""
        pass

    def on_user_update(self, user, nickname):
        """Called when a user is updated (except for nickname changes.)"""
        pass

    def on_user_rename(self, user, nickname, oldname):
        """Called when a user's nick is changed."""
        pass

    def _cleanup_user(self, nick):
        """
        Destroys a user if it's not on our monitor list or in any channels.

        Scheduled task to work around https://github.com/Shizmob/pydle/issues/32
        """
        if nick not in self.users:
            return
        try:
            del self.users[nick]['_cleanup_handle']
        except KeyError:
            pass
        if self.can_see_nick(nick):
            return
        logger.debug("_cleanup_user(): Cleaning up {!r}".format(nick))
        self._destroy_user(nick)

    def _schedule_user_cleanup(self, nick):
        udata = self.users.get(nick)
        if udata is None:
            return
        if udata.get('_cleanup_handle') is not None:
            return
        udata['_cleanup_handle'] = self.eventloop.schedule(self._cleanup_user, nick) or True

    def _create_user(self, nickname):
        super()._create_user(nickname)
        if nickname == self.nickname:
            return
        if nickname not in self.users:
            return
        self.users[nickname]['complete'] = False  # True if we've performed a whois or WHOX against this user.
        self.users[nickname]['data'] = {}  # Misc user data for addons.
        self.on_user_create(self.users[nickname], nickname)
        self._schedule_user_cleanup(nickname)

    def _rename_user(self, user, new):
        super()._rename_user(user, new)
        if new == self.nickname:  # By the time _rename_user is called, we've already updated our own nick
            return
        self._sync_user(new, {'complete': False})
        self.on_user_rename(self.users.get(new), new, user)

    def _sync_user(self, nick, metadata):
        if nick == self.nickname:
            return super()._sync_user(nick, metadata)
        if 'identified' in metadata and 'account' in metadata:
            metadata.setdefault('complete', True)
        udata = self.users.get(nick)
        changed = udata is None or any(k not in udata or udata[k] == v for k, v in metadata.items())
        super()._sync_user(nick, metadata)
        udata = self.users.get(nick)
        if changed and udata:
            self.on_user_update(udata, nick)
        self.eventloop.schedule(self._cleanup_user, nick)

    def _destroy_user(self, user, channel=None, **kwargs):
        udata = self.users.get(user)
        super()._destroy_user(user, channel, **kwargs)
        if user == self.nickname:
            return
        if udata and user not in self.users:
            self.on_user_delete(udata, user)

    def can_see_nick(self, nick):
        return self.is_monitoring(nick) or any(nick in ch['users'] for ch in self.channels.values())

    def whois(self, nickname):
        """
        If nickname is a list, performs multiple nicknames per WHOIS.  Otherwise, does default WHOIS behavior.

        :param nicknames: Nickname(s) to lookup.
        :return: Future
        """
        if isinstance(nickname, str):
            return super().whois(nickname)

        nicknames = set(filter(None, [nick.strip() for nick in nickname]))
        if not nicknames or any(protocol.ARGUMENT_SEPARATOR.search(nickname) is not None for nickname in nicknames):
            # IRCds don't like spaces in nicknames.  Adapted from pydle.features.rfc1459
            return None

        all_futures = set()  # Store all relevant futures.
        drop_nicknames = []  # List of nicknames we'll delete after the pass.
        for nickname in nicknames:
            if nickname in self._pending['whois']:
                all_futures.add(self._pending['whois'][nickname])
                drop_nicknames.append(nickname)
                continue
            future = Future()
            all_futures.add(future)
            self._pending['whois'][nickname] = future
            self._whois_info[nickname] = {
                'oper': False,
                'idle': 0,
                'away': False,
                'away_message': None,
                'account': None,
                'identified': False,
            }

        if nicknames:
            # It's possible we eliminated all nicknames due to already in-progress /WHOIS requests.  But we're here,
            # so we didn't.

            # Send WHOIS
            self.rawmsg('WHOIS', ",".join(nicknames))

        return parallel(*all_futures)

    def on_raw_318(self, message):
        """ End of /WHOIS list. """
        # Our superclass doesn't expect the message to contain multiple nicks, so we do some fakery.
        target, nicknames = message.params[:2]
        for nickname in nicknames.split(","):
            message.params[1] = nickname
            super().on_raw_318(message)

    def on_raw_307(self, message):
        """ WHOIS: User has identified for this nickname. (Anope) """
        # Superclass doesn't set account.  For convenience, assume it's the same as the nick.
        target, nickname = message.params[:2]
        info = {
            'account': nickname,
            'identified': True
        }
        if nickname in self.users:
            self._sync_user(nickname, info)
        if nickname in self._pending['whois']:
            self._whois_info[nickname].update(info)
        super().on_raw_307(message)

    def on_raw_301(self, message):
        """User is away.  Patches around a Pydle bug."""
        nickname, message = message.params[1:]
        info = {
            'away': True,
            'away_message': message
        }

        if nickname in self.users:
            self._sync_user(nickname, info)
        if nickname in self._pending['whois']:
            self._whois_info[nickname].update(info)

    @pydle.coroutine
    def get_user_value(self, nickname, key, default=None, must_exist=False):
        """
        Retrieves a user value, performing a /whois if needed.
        :param nickname: Nickname
        :param property: Property
        :param default: Default value
        :param must_exist: If True, the user must already be known to us (otherwise we return default)

        value = yield bot.get_user_property(...)
        """
        user = self.users.get(nickname)
        if not user and must_exist:
            # yield dummy_future()
            return default

        if user and key in user and user.get('complete'):
            # yield dummy_future()
            return user.get(key, default)

        result = yield self.whois(nickname)
        return result.get(key, default) if result else default
