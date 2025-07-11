# (c) 2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import contextlib
import dataclasses

try:
    import curses
except ImportError:
    HAS_CURSES = False
else:
    # this will be set to False if curses.setupterm() fails
    HAS_CURSES = True

import collections.abc as c
import codecs
import ctypes.util
import fcntl
import getpass
import io
import logging
import os
import secrets
import subprocess
import sys
import termios
import threading
import time
import tty
import typing as t

from functools import wraps
from struct import unpack, pack

from ansible import constants as C
from ansible.constants import config
from ansible.errors import AnsibleAssertionError, AnsiblePromptInterrupt, AnsiblePromptNoninteractive, AnsibleError
from ansible._internal._errors import _error_utils, _error_factory
from ansible._internal import _event_formatting
from ansible.module_utils._internal import _ambient_context, _deprecator, _messages
from ansible.module_utils.common.text.converters import to_bytes, to_text
from ansible.module_utils.datatag import deprecator_from_collection_name
from ansible._internal._datatag._tags import TrustedAsTemplate
from ansible.module_utils.six import text_type
from ansible.module_utils._internal import _traceback, _errors
from ansible.utils.color import stringc
from ansible.utils.multiprocessing import context as multiprocessing_context
from ansible.utils.singleton import Singleton

if t.TYPE_CHECKING:
    # avoid circular import at runtime
    from ansible.executor.task_queue_manager import FinalQueue

P = t.ParamSpec('P')

_LIBC = ctypes.cdll.LoadLibrary(ctypes.util.find_library('c'))
# Set argtypes, to avoid segfault if the wrong type is provided,
# restype is assumed to be c_int
_LIBC.wcwidth.argtypes = (ctypes.c_wchar,)
_LIBC.wcswidth.argtypes = (ctypes.c_wchar_p, ctypes.c_int)
# Max for c_int
_MAX_INT = 2 ** (ctypes.sizeof(ctypes.c_int) * 8 - 1) - 1

MOVE_TO_BOL = b'\r'
CLEAR_TO_EOL = b'\x1b[K'


def _is_controller_traceback_enabled(event: _traceback.TracebackEvent) -> bool:
    """Controller utility function to determine if traceback collection is enabled for the specified event."""
    flag_values: set[str] = set(value for value in C.config.get_config_value('DISPLAY_TRACEBACK'))

    if 'always' in flag_values:
        return True

    if 'never' in flag_values:
        return False

    if _traceback.TracebackEvent.DEPRECATED_VALUE.name.lower() in flag_values:
        flag_values.add(_traceback.TracebackEvent.DEPRECATED.name.lower())  # DEPRECATED_VALUE implies DEPRECATED

    return event.name.lower() in flag_values


_traceback._is_traceback_enabled = _is_controller_traceback_enabled


def get_text_width(text: str) -> int:
    """Function that utilizes ``wcswidth`` or ``wcwidth`` to determine the
    number of columns used to display a text string.

    We try first with ``wcswidth``, and fallback to iterating each
    character and using wcwidth individually, falling back to a value of 0
    for non-printable wide characters.
    """
    if not isinstance(text, text_type):
        raise TypeError('get_text_width requires text, not %s' % type(text))

    try:
        width = _LIBC.wcswidth(text, _MAX_INT)
    except ctypes.ArgumentError:
        width = -1
    if width != -1:
        return width

    width = 0
    counter = 0
    for c in text:
        counter += 1
        if c in (u'\x08', u'\x7f', u'\x94', u'\x1b'):
            # A few characters result in a subtraction of length:
            # BS, DEL, CCH, ESC
            # ESC is slightly different in that it's part of an escape sequence, and
            # while ESC is non printable, it's part of an escape sequence, which results
            # in a single non printable length
            width -= 1
            counter -= 1
            continue

        try:
            w = _LIBC.wcwidth(c)
        except ctypes.ArgumentError:
            w = -1
        if w == -1:
            # -1 signifies a non-printable character
            # use 0 here as a best effort
            w = 0
        width += w

    if width == 0 and counter:
        raise EnvironmentError(
            'get_text_width could not calculate text width of %r' % text
        )

    # It doesn't make sense to have a negative printable width
    return width if width >= 0 else 0


class FilterBlackList(logging.Filter):
    def __init__(self, blacklist):
        self.blacklist = [logging.Filter(name) for name in blacklist]

    def filter(self, record):
        return not any(f.filter(record) for f in self.blacklist)


class FilterUserInjector(logging.Filter):
    """
    This is a filter which injects the current user as the 'user' attribute on each record. We need to add this filter
    to all logger handlers so that 3rd party libraries won't print an exception due to user not being defined.
    """

    try:
        username = getpass.getuser()
    except KeyError:
        # people like to make containers w/o actual valid passwd/shadow and use host uids
        username = 'uid=%s' % os.getuid()

    def filter(self, record):
        record.user = FilterUserInjector.username
        return True


logger = None
# TODO: make this a callback event instead
if getattr(C, 'DEFAULT_LOG_PATH'):
    path = C.DEFAULT_LOG_PATH
    if path and (os.path.exists(path) and os.access(path, os.W_OK)) or os.access(os.path.dirname(path), os.W_OK):
        if not os.path.isdir(path):
            # NOTE: level is kept at INFO to avoid security disclosures caused by certain libraries when using DEBUG
            logging.basicConfig(filename=path, level=logging.INFO,  # DO NOT set to logging.DEBUG
                                format='%(asctime)s p=%(process)d u=%(user)s n=%(name)s %(levelname)s| %(message)s')

            logger = logging.getLogger('ansible')
            for handler in logging.root.handlers:
                handler.addFilter(FilterBlackList(getattr(C, 'DEFAULT_LOG_FILTER', [])))
                handler.addFilter(FilterUserInjector())
        else:
            print(f"[WARNING]: DEFAULT_LOG_PATH can not be a directory '{path}', aborting", file=sys.stderr)
    else:
        print(f"[WARNING]: log file at '{path}' is not writeable and we cannot create it, aborting\n", file=sys.stderr)

# map color to log levels, in order of priority (low to high)
color_to_log_level = {C.COLOR_DEBUG: logging.DEBUG,
                      C.COLOR_VERBOSE: logging.INFO,
                      C.COLOR_OK: logging.INFO,
                      C.COLOR_INCLUDED: logging.INFO,
                      C.COLOR_CHANGED: logging.INFO,
                      C.COLOR_SKIP: logging.WARNING,
                      C.COLOR_DEPRECATE: logging.WARNING,
                      C.COLOR_WARN: logging.WARNING,
                      C.COLOR_UNREACHABLE: logging.ERROR,
                      C.COLOR_ERROR: logging.ERROR}

b_COW_PATHS = (
    b"/usr/bin/cowsay",
    b"/usr/games/cowsay",
    b"/usr/local/bin/cowsay",  # BSD path for cowsay
    b"/opt/local/bin/cowsay",  # MacPorts path for cowsay
)


def _synchronize_textiowrapper(tio: t.TextIO, lock: threading.RLock):
    """
    This decorator ensures that the supplied RLock is held before invoking the wrapped methods.
    It is intended to prevent background threads from holding the Python stdout/stderr buffer lock on a file object during a fork.
    Since background threads are abandoned in child forks, locks they hold are orphaned in a locked state.
    Attempts to acquire an orphaned lock in this state will block forever, effectively hanging the child process on stdout/stderr writes.
    The shared lock is permanently disabled immediately after a fork.
    This prevents hangs in early post-fork code (e.g., stdio writes from pydevd, coverage, etc.) before user code has resumed and released the lock.
    """

    def _wrap_with_lock(f, lock):
        def disable_lock():
            nonlocal lock
            lock = contextlib.nullcontext()

        os.register_at_fork(after_in_child=disable_lock)

        @wraps(f)
        def locking_wrapper(*args, **kwargs):
            with lock:
                return f(*args, **kwargs)

        return locking_wrapper

    buffer = tio.buffer

    # monkeypatching the underlying file-like object isn't great, but likely safer than subclassing
    buffer.write = _wrap_with_lock(buffer.write, lock)  # type: ignore[method-assign]
    buffer.flush = _wrap_with_lock(buffer.flush, lock)  # type: ignore[method-assign]


def setraw(fd: int, when: int = termios.TCSAFLUSH) -> None:
    """Put terminal into a raw mode.

    Copied from ``tty`` from CPython 3.11.0, and modified to not remove OPOST from OFLAG

    OPOST is kept to prevent an issue with multi line prompts from being corrupted now that display
    is proxied via the queue from forks. The problem is a race condition, in that we proxy the display
    over the fork, but before it can be displayed, this plugin will have continued executing, potentially
    setting stdout and stdin to raw which remove output post processing that commonly converts NL to CRLF
    """
    mode = termios.tcgetattr(fd)
    mode[tty.IFLAG] = mode[tty.IFLAG] & ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
    mode[tty.OFLAG] = mode[tty.OFLAG] & ~(termios.OPOST)
    mode[tty.CFLAG] = mode[tty.CFLAG] & ~(termios.CSIZE | termios.PARENB)
    mode[tty.CFLAG] = mode[tty.CFLAG] | termios.CS8
    mode[tty.LFLAG] = mode[tty.LFLAG] & ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
    mode[tty.CC][termios.VMIN] = 1
    mode[tty.CC][termios.VTIME] = 0
    termios.tcsetattr(fd, when, mode)


def clear_line(stdout: t.BinaryIO) -> None:
    stdout.write(b'\x1b[%s' % MOVE_TO_BOL)
    stdout.write(b'\x1b[%s' % CLEAR_TO_EOL)


def setup_prompt(stdin_fd: int, stdout_fd: int, seconds: int, echo: bool) -> None:
    setraw(stdin_fd)

    # Only set stdout to raw mode if it is a TTY. This is needed when redirecting
    # stdout to a file since a file cannot be set to raw mode.
    if os.isatty(stdout_fd):
        setraw(stdout_fd)

    if echo:
        new_settings = termios.tcgetattr(stdin_fd)
        new_settings[3] = new_settings[3] | termios.ECHO
        termios.tcsetattr(stdin_fd, termios.TCSANOW, new_settings)


def setupterm() -> None:
    # Nest the try except since curses.error is not available if curses did not import
    try:
        curses.setupterm()
    except (curses.error, TypeError, io.UnsupportedOperation):
        global HAS_CURSES
        HAS_CURSES = False
    else:
        global MOVE_TO_BOL
        global CLEAR_TO_EOL
        # curses.tigetstr() returns None in some circumstances
        MOVE_TO_BOL = curses.tigetstr('cr') or MOVE_TO_BOL
        CLEAR_TO_EOL = curses.tigetstr('el') or CLEAR_TO_EOL


class Display(metaclass=Singleton):

    def __init__(self, verbosity: int = 0) -> None:

        self._final_q: FinalQueue | None = None

        # NB: this lock is used to both prevent intermingled output between threads and to block writes during forks.
        # Do not change the type of this lock or upgrade to a shared lock (eg multiprocessing.RLock).
        self._lock = threading.RLock()

        self.columns = None
        self.verbosity = verbosity

        if C.LOG_VERBOSITY is None:
            self.log_verbosity = verbosity
        else:
            self.log_verbosity = max(verbosity, C.LOG_VERBOSITY)

        # list of all deprecation messages to prevent duplicate display
        self._deprecations: set[str] = set()
        self._warns: set[str] = set()
        self._errors: set[str] = set()

        self.b_cowsay: bytes | None = None
        self.noncow = C.ANSIBLE_COW_SELECTION

        self.set_cowsay_info()

        if self.b_cowsay:
            try:
                cmd = subprocess.Popen([self.b_cowsay, "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                (out, err) = cmd.communicate()
                if cmd.returncode:
                    raise Exception
                self.cows_available: set[str] = {to_text(c) for c in out.split()}
                if C.ANSIBLE_COW_ACCEPTLIST and any(C.ANSIBLE_COW_ACCEPTLIST):
                    self.cows_available = set(C.ANSIBLE_COW_ACCEPTLIST).intersection(self.cows_available)
            except Exception:
                # could not execute cowsay for some reason
                self.b_cowsay = None

        self._set_column_width()

        try:
            # NB: we're relying on the display singleton behavior to ensure this only runs once
            _synchronize_textiowrapper(sys.stdout, self._lock)
            _synchronize_textiowrapper(sys.stderr, self._lock)
        except Exception as ex:
            self.warning(f"failed to patch stdout/stderr for fork-safety: {ex}")

        codecs.register_error('_replacing_warning_handler', self._replacing_warning_handler)
        try:
            sys.stdout.reconfigure(errors='_replacing_warning_handler')  # type: ignore[union-attr]
            sys.stderr.reconfigure(errors='_replacing_warning_handler')  # type: ignore[union-attr]
        except Exception as ex:
            self.warning(f"failed to reconfigure stdout/stderr with custom encoding error handler: {ex}")

        self.setup_curses = False

    def _replacing_warning_handler(self, exception: UnicodeError) -> tuple[str | bytes, int]:
        # This can't be removed as long as we have the possibility of encountering un-renderable strings
        # created with `surrogateescape`; the alternative of having display methods hard fail is untenable.
        self.warning('Non UTF-8 encoded data replaced with "?" while displaying text to stdout/stderr.')
        return '?', exception.end

    def set_queue(self, queue: FinalQueue) -> None:
        """Set the _final_q on Display, so that we know to proxy display over the queue
        instead of directly writing to stdout/stderr from forks

        This is only needed in ansible.executor.process.worker:WorkerProcess._run
        """
        if multiprocessing_context.parent_process() is None:
            raise RuntimeError('queue cannot be set in parent process')
        self._final_q = queue

    def set_cowsay_info(self) -> None:
        if C.ANSIBLE_NOCOWS:
            return

        if C.ANSIBLE_COW_PATH:
            self.b_cowsay = C.ANSIBLE_COW_PATH
        else:
            for b_cow_path in b_COW_PATHS:
                if os.path.exists(b_cow_path):
                    self.b_cowsay = b_cow_path

    @staticmethod
    def _proxy(
        func: c.Callable[t.Concatenate[Display, P], None]
    ) -> c.Callable[..., None]:
        @wraps(func)
        def wrapper(self, *args: P.args, **kwargs: P.kwargs) -> None:
            if self._final_q:
                # If _final_q is set, that means we are in a WorkerProcess
                # and instead of displaying messages directly from the fork
                # we will proxy them through the queue
                return self._final_q.send_display(func.__name__, *args, **kwargs)
            return func(self, *args, **kwargs)
        return wrapper

    @staticmethod
    def _meets_debug(
        func: c.Callable[..., None]
    ) -> c.Callable[..., None]:
        """This method ensures that debug is enabled before delegating to the proxy
        """
        @wraps(func)
        def wrapper(self, msg: str, host: str | None = None) -> None:
            if not C.DEFAULT_DEBUG:
                return
            return func(self, msg, host=host)
        return wrapper

    @staticmethod
    def _meets_verbosity(
        func: c.Callable[..., None]
    ) -> c.Callable[..., None]:
        """This method ensures the verbosity has been met before delegating to the proxy

        Currently this method is unused, and the logic is handled directly in ``verbose``
        """
        @wraps(func)
        def wrapper(self, msg: str, host: str | None = None, caplevel: int = None) -> None:
            if self.verbosity > caplevel:
                return func(self, msg, host=host, caplevel=caplevel)
            return
        return wrapper

    @_proxy
    def display(
        self,
        msg: str,
        color: str | None = None,
        stderr: bool = False,
        screen_only: bool = False,
        log_only: bool = False,
        newline: bool = True,
        caplevel: int | None = None,
    ) -> None:
        """ Display a message to the user

        Note: msg *must* be a unicode string to prevent UnicodeError tracebacks.
        """

        if not isinstance(msg, str):
            raise TypeError(f'Display message must be str, not: {msg.__class__.__name__}')

        # Convert Windows newlines to Unix newlines.
        # Some environments, such as Azure Pipelines, render `\r` as an additional `\n`.
        msg = msg.replace('\r\n', '\n')

        nocolor = msg

        if not log_only:

            has_newline = msg.endswith(u'\n')
            if has_newline:
                msg2 = msg[:-1]
            else:
                msg2 = msg

            if color:
                msg2 = stringc(msg2, color)

            if has_newline or newline:
                msg2 = msg2 + u'\n'

            # Note: After Display() class is refactored need to update the log capture
            # code in 'cli/scripts/ansible_connection_cli_stub.py' (and other relevant places).
            if not stderr:
                fileobj = sys.stdout
            else:
                fileobj = sys.stderr

            with self._lock:
                fileobj.write(msg2)

            # With locks, and the fact that we aren't printing from forks
            # just write, and let the system flush. Everything should come out peachy
            # I've left this code for historical purposes, or in case we need to add this
            # back at a later date. For now ``TaskQueueManager.cleanup`` will perform a
            # final flush at shutdown.
            # try:
            #     fileobj.flush()
            # except OSError as e:
            #     # Ignore EPIPE in case fileobj has been prematurely closed, eg.
            #     # when piping to "head -n1"
            #     if e.errno != errno.EPIPE:
            #         raise

        if logger and not screen_only:
            self._log(nocolor, color, caplevel)

    def _log(self, msg: str, color: str | None = None, caplevel: int | None = None):

        if logger and (caplevel is None or self.log_verbosity > caplevel):
            msg2 = msg.lstrip('\n')

            if caplevel is None or caplevel > 0:
                lvl = logging.INFO
            elif caplevel == -1:
                lvl = logging.ERROR
            elif caplevel == -2:
                lvl = logging.WARNING
            elif caplevel == -3:
                lvl = logging.DEBUG
            elif color:
                # set logger level based on color (not great)
                # but last resort and backwards compatible
                try:
                    lvl = color_to_log_level[color]
                except KeyError:
                    # this should not happen if mapping is updated with new color configs, but JIC
                    raise AnsibleAssertionError('Invalid color supplied to display: %s' % color)

            # actually log
            logger.log(lvl, msg2)

    def v(self, msg: str, host: str | None = None) -> None:
        return self.verbose(msg, host=host, caplevel=0)

    def vv(self, msg: str, host: str | None = None) -> None:
        return self.verbose(msg, host=host, caplevel=1)

    def vvv(self, msg: str, host: str | None = None) -> None:
        return self.verbose(msg, host=host, caplevel=2)

    def vvvv(self, msg: str, host: str | None = None) -> None:
        return self.verbose(msg, host=host, caplevel=3)

    def vvvvv(self, msg: str, host: str | None = None) -> None:
        return self.verbose(msg, host=host, caplevel=4)

    def vvvvvv(self, msg: str, host: str | None = None) -> None:
        return self.verbose(msg, host=host, caplevel=5)

    def verbose(self, msg: str, host: str | None = None, caplevel: int = 2) -> None:
        if self.verbosity > caplevel:
            self._verbose_display(msg, host=host, caplevel=caplevel)

        if self.log_verbosity > self.verbosity and self.log_verbosity > caplevel:
            self._verbose_log(msg, host=host, caplevel=caplevel)

    @_proxy
    def _verbose_display(self, msg: str, host: str | None = None, caplevel: int = 2) -> None:
        to_stderr = C.VERBOSE_TO_STDERR
        if host is None:
            self.display(msg, color=C.COLOR_VERBOSE, stderr=to_stderr)
        else:
            self.display("<%s> %s" % (host, msg), color=C.COLOR_VERBOSE, stderr=to_stderr)

    @_proxy
    def _verbose_log(self, msg: str, host: str | None = None, caplevel: int = 2) -> None:
        # we send to log if log was configured with higher verbosity
        if host is not None:
            msg = "<%s> %s" % (host, msg)
        self._log(msg, C.COLOR_VERBOSE, caplevel)

    @_meets_debug
    @_proxy
    def debug(self, msg: str, host: str | None = None) -> None:
        prefix = "%6d %0.5f" % (os.getpid(), time.time())
        if host is not None:
            prefix += f" [{host}]"
        self.display(f"{prefix}: {msg}", color=C.COLOR_DEBUG, caplevel=-3)

    def get_deprecation_message(
        self,
        msg: str,
        version: str | None = None,
        removed: bool = False,
        date: str | None = None,
        collection_name: str | None = None,
    ) -> str:
        """Return a deprecation message and help text for non-display purposes (e.g., exception messages)."""
        self.deprecated(
            msg="The `get_deprecation_message` method is deprecated.",
            help_text="Use the `deprecated` method instead.",
            version="2.23",
        )

        msg = self._get_deprecation_message_with_plugin_info(
            msg=msg,
            version=version,
            removed=removed,
            date=date,
            deprecator=deprecator_from_collection_name(collection_name),
        )

        if removed:
            msg = f'[DEPRECATED]: {msg}'
        else:
            msg = f'[DEPRECATION WARNING]: {msg}'

        return msg

    def _get_deprecation_message_with_plugin_info(
        self,
        *,
        msg: str,
        version: str | None,
        removed: bool = False,
        date: str | None,
        deprecator: _messages.PluginInfo | None,
    ) -> str:
        """Internal use only. Return a deprecation message and help text for display."""
        # DTFIX-FUTURE: the logic for omitting date/version doesn't apply to the payload, so it shows up in vars in some cases when it should not

        if removed:
            removal_fragment = 'This feature was removed'
        else:
            removal_fragment = 'This feature will be removed'

        if not deprecator or not deprecator.type:
            # indeterminate has no resolved_name or type
            # collections have a resolved_name but no type
            collection = deprecator.resolved_name if deprecator else None
            plugin_fragment = ''
        elif deprecator.resolved_name == 'ansible.builtin':
            # core deprecations from base classes (the API) have no plugin name, only 'ansible.builtin'
            plugin_type_name = str(deprecator.type) if deprecator.type is _messages.PluginType.MODULE else f'{deprecator.type} plugin'

            collection = deprecator.resolved_name
            plugin_fragment = f'the {plugin_type_name} API'
        else:
            parts = deprecator.resolved_name.split('.')
            plugin_name = parts[-1]
            plugin_type_name = str(deprecator.type) if deprecator.type is _messages.PluginType.MODULE else f'{deprecator.type} plugin'

            collection = '.'.join(parts[:2]) if len(parts) > 2 else None
            plugin_fragment = f'{plugin_type_name} {plugin_name!r}'

        if collection and plugin_fragment:
            plugin_fragment += ' in'

        if collection == 'ansible.builtin':
            collection_fragment = 'ansible-core'
        elif collection:
            collection_fragment = f'collection {collection!r}'
        else:
            collection_fragment = ''

        if not collection:
            when_fragment = 'in the future' if not removed else ''
        elif date:
            when_fragment = f'in a release after {date}'
        elif version:
            when_fragment = f'version {version}'
        else:
            when_fragment = 'in a future release' if not removed else ''

        if plugin_fragment or collection_fragment:
            from_fragment = 'from'
        else:
            from_fragment = ''

        deprecation_msg = ' '.join(f for f in [removal_fragment, from_fragment, plugin_fragment, collection_fragment, when_fragment] if f) + '.'

        return _join_sentences(msg, deprecation_msg)

    @staticmethod
    def _deduplicate(msg: str, messages: set[str]) -> bool:
        """
        Return True if the given message was previously seen, otherwise record the message as seen and return False.
        This is done very late (at display-time) to avoid loss of attribution of messages to individual tasks.
        Duplicates included in task results will always be visible to registered variables and callbacks.
        """

        if msg in messages:
            return True

        messages.add(msg)

        return False

    def deprecated(
        self,
        msg: str,
        version: str | None = None,
        removed: bool = False,
        date: str | None = None,
        collection_name: str | None = None,
        *,
        deprecator: _messages.PluginInfo | None = None,
        help_text: str | None = None,
        obj: t.Any = None,
    ) -> None:
        """
        Display a deprecation warning message, if enabled.
        Most callers do not need to provide `collection_name` or `deprecator` -- but provide only one if needed.
        Specify `version` or `date`, but not both.
        If `date` is a string, it must be in the form `YYYY-MM-DD`.
        """
        # DTFIX3: are there any deprecation calls where the feature is switching from enabled to disabled, rather than being removed entirely?
        # DTFIX3: are there deprecated features which should going through deferred deprecation instead?

        _skip_stackwalk = True

        self._deprecated_with_plugin_info(
            msg=msg,
            version=version,
            removed=removed,
            date=date,
            help_text=help_text,
            obj=obj,
            deprecator=_deprecator.get_best_deprecator(deprecator=deprecator, collection_name=collection_name),
            formatted_traceback=_traceback.maybe_capture_traceback(msg, _traceback.TracebackEvent.DEPRECATED),
        )

    def _deprecated_with_plugin_info(
        self,
        *,
        msg: str,
        version: str | None,
        removed: bool = False,
        date: str | None,
        help_text: str | None,
        obj: t.Any,
        deprecator: _messages.PluginInfo | None,
        formatted_traceback: str | None = None,
    ) -> None:
        """
        This is the internal pre-proxy half of the `deprecated` implementation.
        Any logic that must occur on workers needs to be implemented here.
        """
        _skip_stackwalk = True

        if removed:
            formatted_msg = self._get_deprecation_message_with_plugin_info(
                msg=msg,
                version=version,
                removed=removed,
                date=date,
                deprecator=deprecator,
            )

            raise AnsibleError(formatted_msg)

        if source_context := _error_utils.SourceContext.from_value(obj):
            formatted_source_context = str(source_context)
        else:
            formatted_source_context = None

        deprecation = _messages.DeprecationSummary(
            event=_messages.Event(
                msg=msg,
                formatted_source_context=formatted_source_context,
                help_text=help_text,
                formatted_traceback=formatted_traceback,
            ),
            version=version,
            date=date,
            deprecator=deprecator,
        )

        if warning_ctx := _DeferredWarningContext.current(optional=True):
            warning_ctx.capture(deprecation)
            return

        self._deprecated(deprecation)

    @_proxy
    def _deprecated(self, warning: _messages.DeprecationSummary) -> None:
        """Internal implementation detail, use `deprecated` instead."""

        # This is the post-proxy half of the `deprecated` implementation.
        # Any logic that must occur in the primary controller process needs to be implemented here.

        if not _DeferredWarningContext.deprecation_warnings_enabled():
            return

        self.warning('Deprecation warnings can be disabled by setting `deprecation_warnings=False` in ansible.cfg.')

        msg = _format_message(warning, _traceback.is_traceback_enabled(_traceback.TracebackEvent.DEPRECATED))
        msg = f'[DEPRECATION WARNING]: {msg}'

        if self._deduplicate(msg, self._deprecations):
            return

        self.display(msg, color=C.config.get_config_value('COLOR_DEPRECATE'), stderr=True)

    def warning(
        self,
        msg: str,
        formatted: bool = False,
        *,
        help_text: str | None = None,
        obj: t.Any = None
    ) -> None:
        """Display a warning message."""
        _skip_stackwalk = True

        # deprecated: description='The formatted argument has no effect.' core_version='2.23'

        # This is the pre-proxy half of the `warning` implementation.
        # Any logic that must occur on workers needs to be implemented here.

        if source_context := _error_utils.SourceContext.from_value(obj):
            formatted_source_context = str(source_context)
        else:
            formatted_source_context = None

        warning = _messages.WarningSummary(
            event=_messages.Event(
                msg=msg,
                help_text=help_text,
                formatted_source_context=formatted_source_context,
                formatted_traceback=_traceback.maybe_capture_traceback(msg, _traceback.TracebackEvent.WARNING),
            ),
        )

        if warning_ctx := _DeferredWarningContext.current(optional=True):
            warning_ctx.capture(warning)
            return

        self._warning(warning)

    @_proxy
    def _warning(self, warning: _messages.WarningSummary) -> None:
        """Internal implementation detail, use `warning` instead."""

        # This is the post-proxy half of the `warning` implementation.
        # Any logic that must occur in the primary controller process needs to be implemented here.

        msg = _format_message(warning, _traceback.is_traceback_enabled(_traceback.TracebackEvent.WARNING))
        msg = f"[WARNING]: {msg}"

        if self._deduplicate(msg, self._warns):
            return

        self.display(msg, color=C.config.get_config_value('COLOR_WARN'), stderr=True, caplevel=-2)

    @_proxy
    def system_warning(self, msg: str) -> None:
        if C.SYSTEM_WARNINGS:
            self.warning(msg)

    @_proxy
    def banner(self, msg: str, color: str | None = None, cows: bool = True) -> None:
        """
        Prints a header-looking line with cowsay or stars with length depending on terminal width (3 minimum)
        """
        msg = to_text(msg)

        if self.b_cowsay and cows:
            try:
                self.banner_cowsay(msg)
                return
            except OSError:
                self.warning("somebody cleverly deleted cowsay or something during the PB run.  heh.")

        msg = msg.strip()
        try:
            star_len = self.columns - get_text_width(msg)
        except EnvironmentError:
            star_len = self.columns - len(msg)
        if star_len <= 3:
            star_len = 3
        stars = u"*" * star_len
        self.display(u"\n%s %s" % (msg, stars), color=color)

    @_proxy
    def banner_cowsay(self, msg: str, color: str | None = None) -> None:
        if u": [" in msg:
            msg = msg.replace(u"[", u"")
            if msg.endswith(u"]"):
                msg = msg[:-1]
        runcmd = [self.b_cowsay, b"-W", b"60"]
        if self.noncow:
            thecow = self.noncow
            if thecow == 'random':
                thecow = secrets.choice(list(self.cows_available))
            runcmd.append(b'-f')
            runcmd.append(to_bytes(thecow))
        runcmd.append(to_bytes(msg))
        cmd = subprocess.Popen(runcmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (out, err) = cmd.communicate()
        self.display(u"%s\n" % to_text(out), color=color)

    def error_as_warning(
        self,
        msg: str | None,
        exception: BaseException,
        *,
        help_text: str | None = None,
        obj: t.Any = None,
    ) -> None:
        """Display an exception as a warning."""
        _skip_stackwalk = True

        event = _error_factory.ControllerEventFactory.from_exception(exception, _traceback.is_traceback_enabled(_traceback.TracebackEvent.WARNING))

        if msg:
            if source_context := _error_utils.SourceContext.from_value(obj):
                formatted_source_context = str(source_context)
            else:
                formatted_source_context = None

            event = _messages.Event(
                msg=msg,
                help_text=help_text,
                formatted_source_context=formatted_source_context,
                formatted_traceback=_traceback.maybe_capture_traceback(msg, _traceback.TracebackEvent.WARNING),
                chain=_messages.EventChain(
                    msg_reason=_errors.MSG_REASON_DIRECT_CAUSE,
                    traceback_reason=_errors.TRACEBACK_REASON_EXCEPTION_DIRECT_WARNING,
                    event=event,
                ),
            )

        warning = _messages.WarningSummary(
            event=event,
        )

        if warning_ctx := _DeferredWarningContext.current(optional=True):
            warning_ctx.capture(warning)
            return

        self._warning(warning)

    def error(self, msg: str | BaseException, wrap_text: bool = True, stderr: bool = True) -> None:
        """Display an error message."""
        _skip_stackwalk = True

        # deprecated: description='The wrap_text argument has no effect.' core_version='2.23'
        # deprecated: description='The stderr argument has no effect.' core_version='2.23'

        # This is the pre-proxy half of the `error` implementation.
        # Any logic that must occur on workers needs to be implemented here.

        if isinstance(msg, BaseException):
            event = _error_factory.ControllerEventFactory.from_exception(msg, _traceback.is_traceback_enabled(_traceback.TracebackEvent.ERROR))
        else:
            event = _messages.Event(
                msg=msg,
                formatted_traceback=_traceback.maybe_capture_traceback(msg, _traceback.TracebackEvent.ERROR),
            )

        error = _messages.ErrorSummary(
            event=event,
        )

        self._error(error, stderr=True)

    @_proxy
    def _error(self, error: _messages.ErrorSummary, stderr: bool) -> None:
        """Internal implementation detail, use `error` instead."""

        # This is the post-proxy half of the `error` implementation.
        # Any logic that must occur in the primary controller process needs to be implemented here.

        msg = _format_message(error, _traceback.is_traceback_enabled(_traceback.TracebackEvent.ERROR))
        msg = f'[ERROR]: {msg}'

        if self._deduplicate(msg, self._errors):
            return

        self.display(msg, color=C.config.get_config_value('COLOR_ERROR'), stderr=stderr, caplevel=-1)

    @staticmethod
    def prompt(msg: str, private: bool = False) -> str:
        if private:
            return getpass.getpass(msg)
        else:
            return input(msg)

    def do_var_prompt(
        self,
        varname: str,
        private: bool = True,
        prompt: str | None = None,
        encrypt: str | None = None,
        confirm: bool = False,
        salt_size: int | None = None,
        salt: str | None = None,
        default: str | None = None,
        unsafe: bool = False,
    ) -> str:
        result = None
        if sys.__stdin__.isatty():

            do_prompt = self.prompt

            if prompt and default is not None:
                msg = "%s [%s]: " % (prompt, default)
            elif prompt:
                msg = "%s: " % prompt
            else:
                msg = 'input for %s: ' % varname

            if confirm:
                while True:
                    result = do_prompt(msg, private)
                    second = do_prompt("confirm " + msg, private)
                    if result == second:
                        break
                    self.display("***** VALUES ENTERED DO NOT MATCH ****")
            else:
                result = do_prompt(msg, private)
        else:
            result = None
            self.warning("Not prompting as we are not in interactive mode")

        # if result is false and default is not None
        if not result and default is not None:
            result = default

        if encrypt:
            # Circular import because encrypt needs a display class
            from ansible.utils.encrypt import do_encrypt
            result = do_encrypt(result, encrypt, salt_size=salt_size, salt=salt)

        # handle utf-8 chars
        result = to_text(result, errors='surrogate_or_strict')

        if not unsafe:
            # to maintain backward compatibility, assume these values are safe to template
            result = TrustedAsTemplate().tag(result)

        return result

    def _set_column_width(self) -> None:
        if os.isatty(1):
            tty_size = unpack('HHHH', fcntl.ioctl(1, termios.TIOCGWINSZ, pack('HHHH', 0, 0, 0, 0)))[1]
        else:
            tty_size = 0
        self.columns = max(79, tty_size - 1)

    def prompt_until(
        self,
        msg: str,
        private: bool = False,
        seconds: int | None = None,
        interrupt_input: c.Iterable[bytes] | None = None,
        complete_input: c.Iterable[bytes] | None = None,
    ) -> bytes:
        if self._final_q:
            from ansible.executor.process.worker import current_worker
            self._final_q.send_prompt(
                worker_id=current_worker.worker_id, prompt=msg, private=private, seconds=seconds,
                interrupt_input=interrupt_input, complete_input=complete_input
            )
            return current_worker.worker_queue.get()

        if HAS_CURSES and not self.setup_curses:
            setupterm()
            self.setup_curses = True

        if (
            self._stdin_fd is None
            or not os.isatty(self._stdin_fd)
            # Compare the current process group to the process group associated
            # with terminal of the given file descriptor to determine if the process
            # is running in the background.
            or os.getpgrp() != os.tcgetpgrp(self._stdin_fd)
        ):
            raise AnsiblePromptNoninteractive('stdin is not interactive')

        # When seconds/interrupt_input/complete_input are all None, this does mostly the same thing as input/getpass,
        # but self.prompt may raise a KeyboardInterrupt, which must be caught in the main thread.
        # If the main thread handled this, it would also need to send a newline to the tty of any hanging pids.
        # if seconds is None and interrupt_input is None and complete_input is None:
        #     try:
        #         return self.prompt(msg, private=private)
        #     except KeyboardInterrupt:
        #         # can't catch in the results_thread_main daemon thread
        #         raise AnsiblePromptInterrupt('user interrupt')

        self.display(msg)
        result = b''
        with self._lock:
            original_stdin_settings = termios.tcgetattr(self._stdin_fd)
            try:
                setup_prompt(self._stdin_fd, self._stdout_fd, seconds, not private)

                # flush the buffer to make sure no previous key presses
                # are read in below
                termios.tcflush(self._stdin, termios.TCIFLUSH)

                # read input 1 char at a time until the optional timeout or complete/interrupt condition is met
                return self._read_non_blocking_stdin(echo=not private, seconds=seconds, interrupt_input=interrupt_input, complete_input=complete_input)
            finally:
                # restore the old settings for the duped stdin stdin_fd
                termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, original_stdin_settings)

    def _read_non_blocking_stdin(
        self,
        echo: bool = False,
        seconds: int | None = None,
        interrupt_input: c.Iterable[bytes] | None = None,
        complete_input: c.Iterable[bytes] | None = None,
    ) -> bytes:
        if self._final_q:
            raise NotImplementedError

        if seconds is not None:
            start = time.time()
        if interrupt_input is None:
            try:
                interrupt = termios.tcgetattr(sys.stdin.buffer.fileno())[6][termios.VINTR]
            except Exception:
                interrupt = b'\x03'  # value for Ctrl+C

        try:
            backspace_sequences = [termios.tcgetattr(self._stdin_fd)[6][termios.VERASE]]
        except Exception:
            # unsupported/not present, use default
            backspace_sequences = [b'\x7f', b'\x08']

        result_string = b''
        while seconds is None or (time.time() - start < seconds):
            key_pressed = None
            try:
                os.set_blocking(self._stdin_fd, False)
                while key_pressed is None and (seconds is None or (time.time() - start < seconds)):
                    key_pressed = self._stdin.read(1)
                    # throttle to prevent excess CPU consumption
                    time.sleep(C.DEFAULT_INTERNAL_POLL_INTERVAL)
            finally:
                os.set_blocking(self._stdin_fd, True)
                if key_pressed is None:
                    key_pressed = b''

            if (interrupt_input is None and key_pressed == interrupt) or (interrupt_input is not None and key_pressed.lower() in interrupt_input):
                clear_line(self._stdout)
                raise AnsiblePromptInterrupt('user interrupt')
            if (complete_input is None and key_pressed in (b'\r', b'\n')) or (complete_input is not None and key_pressed.lower() in complete_input):
                clear_line(self._stdout)
                break
            elif key_pressed in backspace_sequences:
                clear_line(self._stdout)
                result_string = result_string[:-1]
                if echo:
                    self._stdout.write(result_string)
                self._stdout.flush()
            else:
                result_string += key_pressed
        return result_string

    @property
    def _stdin(self) -> t.BinaryIO | None:
        if self._final_q:
            raise NotImplementedError
        try:
            return sys.stdin.buffer
        except AttributeError:
            return None

    @property
    def _stdin_fd(self) -> int | None:
        try:
            return self._stdin.fileno()
        except (ValueError, AttributeError):
            return None

    @property
    def _stdout(self) -> t.BinaryIO:
        if self._final_q:
            raise NotImplementedError
        return sys.stdout.buffer

    @property
    def _stdout_fd(self) -> int | None:
        try:
            return self._stdout.fileno()
        except (ValueError, AttributeError):
            return None


_display = Display()


class _DeferredWarningContext(_ambient_context.AmbientContextBase):
    """
    Calls to `Display.warning()` and `Display.deprecated()` within this context will cause the resulting warnings to be captured and not displayed.
    The intended use is for task-initiated warnings to be recorded with the task result, which makes them visible to registered results, callbacks, etc.
    The active display callback is responsible for communicating any warnings to the user.
    """

    # DTFIX-FUTURE: once we start implementing nested scoped contexts for our own bookkeeping, this should be an interface facade that forwards to the nearest
    #               context that actually implements the warnings collection capability

    def __init__(self, *, variables: dict[str, object]) -> None:
        self._variables = variables  # DTFIX-FUTURE: move this to an AmbientContext-derived TaskContext (once it exists)
        self._deprecation_warnings: list[_messages.DeprecationSummary] = []
        self._warnings: list[_messages.WarningSummary] = []
        self._seen: set[_messages.WarningSummary] = set()

    @classmethod
    def deprecation_warnings_enabled(cls) -> bool:
        """Return True if deprecation warnings are enabled for the current calling context, otherwise False."""
        # DTFIX-FUTURE: move this capability into config using an AmbientContext-derived TaskContext (once it exists)
        if warning_ctx := cls.current(optional=True):
            variables = warning_ctx._variables
        else:
            variables = None

        return C.config.get_config_value('DEPRECATION_WARNINGS', variables=variables)

    def capture(self, warning: _messages.WarningSummary) -> None:
        """Add the warning/deprecation to the context if it has not already been seen by this context."""
        if warning in self._seen:
            return

        self._seen.add(warning)

        if isinstance(warning, _messages.DeprecationSummary):
            self._deprecation_warnings.append(warning)
        else:
            self._warnings.append(warning)

    def get_warnings(self) -> list[_messages.WarningSummary]:
        """Return a list of the captured non-deprecation warnings."""
        # DTFIX-FUTURE: return a read-only list proxy instead
        return self._warnings

    def get_deprecation_warnings(self) -> list[_messages.DeprecationSummary]:
        """Return a list of the captured deprecation warnings."""
        # DTFIX-FUTURE: return a read-only list proxy instead
        return self._deprecation_warnings


def _join_sentences(first: str | None, second: str | None) -> str:
    """Join two sentences together."""
    first = (first or '').strip()
    second = (second or '').strip()

    if first and first[-1] not in ('!', '?', '.'):
        first += '.'

    if second and second[-1] not in ('!', '?', '.'):
        second += '.'

    if first and not second:
        return first

    if not first and second:
        return second

    return ' '.join((first, second))


def _format_message(summary: _messages.SummaryBase, include_traceback: bool) -> str:
    if isinstance(summary, _messages.DeprecationSummary):
        deprecation_message = _display._get_deprecation_message_with_plugin_info(
            msg=summary.event.msg,
            version=summary.version,
            date=summary.date,
            deprecator=summary.deprecator,
        )

        event = dataclasses.replace(summary.event, msg=deprecation_message)
    else:
        event = summary.event

    return _event_formatting.format_event(event, include_traceback)


def _report_config_warnings(deprecator: _messages.PluginInfo) -> None:
    """Called by config to report warnings/deprecations collected during a config parse."""
    while config._errors:
        msg, exception = config._errors.pop()
        _display.error_as_warning(msg=msg, exception=exception)

    while config.WARNINGS:
        warn = config.WARNINGS.pop()
        _display.warning(warn)

    while config.DEPRECATED:
        # tuple with name and options
        dep = config.DEPRECATED.pop(0)
        msg = config.get_deprecated_msg_from_config(dep[1]).replace("\t", "")

        _display.deprecated(  # pylint: disable=ansible-deprecated-unnecessary-collection-name,ansible-invalid-deprecated-version
            msg=f"{dep[0]} option. {msg}",
            version=dep[1]['version'],
            deprecator=deprecator,
        )


# emit any warnings or deprecations
# in the event config fails before display is up, we'll lose warnings -- but that's OK, since everything is broken anyway
_report_config_warnings(_deprecator.ANSIBLE_CORE_DEPRECATOR)
