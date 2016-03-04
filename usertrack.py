"""Improved usertracking capabilities."""
import pydle
from pydle.features.rfc1459 import protocol
from pydle.async import Future, parallel
import pydle.features
# Portions of UserTrackingClient are directly derived from code in Pydle and thus are subject to its license.
# The Pydle license and related information can be found in LICENSE.md


def dummy_future(result=None):
    """
    Creates a future with a predetermined result.  Used for cases where we might return a 'real' future but sometimes
    we can resume immediately.
    :return: Created future.
    """
    future = Future()
    future.set_result(result)
    return future


class UserTrackingClient(pydle.featurize(pydle.features.AccountSupport, pydle.features.RFC1459Support)):
    def _create_user(self, nickname):
        super()._create_user(nickname)
        if nickname in self.users:
            self.users[nickname]['complete'] = False  # True if we've performed a whois or WHOX against this user.

    def _rename_user(self, user, new):
        super()._rename_user(user, new)
        self._sync_user(new, {'complete': False})

    def _sync_user(self, nick, metadata):
        if 'identified' in metadata and 'account' in metadata:
            metadata.setdefault('complete', True)
        return super()._sync_user(nick, metadata)

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
            return dummy_future()

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
            yield dummy_future()
            return default

        if key in user and user.get('complete'):
            yield dummy_future()
            return user.get(key, default)

        result = yield self.whois(user)
        return result.get(key, default) if result else default
