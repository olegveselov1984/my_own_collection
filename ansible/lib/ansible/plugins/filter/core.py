# (c) 2012, Jeroen Hoekx <jeroen@hoekx.be>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import base64
import functools
import glob
import hashlib
import json
import ntpath
import os.path
import re
import shlex
import time
import uuid
import yaml
import datetime
import typing as t

from collections.abc import Mapping
from functools import partial
from random import Random, SystemRandom, shuffle

from jinja2.filters import do_map, do_select, do_selectattr, do_reject, do_rejectattr, pass_environment, sync_do_groupby
from jinja2.environment import Environment

from ansible._internal._templating import _lazy_containers
from ansible.errors import AnsibleFilterError, AnsibleTypeError, AnsibleTemplatePluginError
from ansible.module_utils.datatag import native_type_name
from ansible.module_utils.common.json import get_encoder, get_decoder
from ansible.module_utils.six import string_types, integer_types, text_type
from ansible.module_utils.common.text.converters import to_bytes, to_native, to_text
from ansible.module_utils.common.collections import is_sequence
from ansible.parsing.yaml.dumper import AnsibleDumper
from ansible.template import accept_args_markers, accept_lazy_markers
from ansible._internal._templating._jinja_common import MarkerError, UndefinedMarker, validate_arg_type
from ansible._internal._yaml import _loader as _yaml_loader
from ansible.utils.display import Display
from ansible.utils.encrypt import do_encrypt, PASSLIB_AVAILABLE
from ansible.utils.hashing import md5s, checksum_s
from ansible.utils.unicode import unicode_wrap
from ansible.utils.vars import merge_hash

display = Display()

UUID_NAMESPACE_ANSIBLE = uuid.UUID('361E6D51-FAEC-444A-9079-341386DA8E2E')


@accept_lazy_markers
def to_yaml(a, *_args, default_flow_style: bool | None = None, **kwargs) -> str:
    """Serialize input as terse flow-style YAML."""
    return yaml.dump(a, Dumper=AnsibleDumper, allow_unicode=True, default_flow_style=default_flow_style, **kwargs)


@accept_lazy_markers
def to_nice_yaml(a, indent=4, *_args, default_flow_style=False, **kwargs) -> str:
    """Serialize input as verbose multi-line YAML."""
    return to_yaml(a, indent=indent, default_flow_style=default_flow_style, **kwargs)


def from_json(a, profile: str | None = None, **kwargs) -> t.Any:
    """Deserialize JSON with an optional decoder profile."""
    cls = get_decoder(profile or "tagless")

    return json.loads(a, cls=cls, **kwargs)


def to_json(a, profile: str | None = None, vault_to_text: t.Any = ..., preprocess_unsafe: t.Any = ..., **kwargs) -> str:
    """Serialize as JSON with an optional encoder profile."""

    if profile and vault_to_text is not ...:
        raise ValueError("Only one of `vault_to_text` or `profile` can be specified.")

    if profile and preprocess_unsafe is not ...:
        raise ValueError("Only one of `preprocess_unsafe` or `profile` can be specified.")

    # deprecated: description='deprecate vault_to_text' core_version='2.23'
    # deprecated: description='deprecate preprocess_unsafe' core_version='2.23'

    cls = get_encoder(profile or "tagless")

    return json.dumps(a, cls=cls, **kwargs)


def to_nice_json(a, indent=4, sort_keys=True, **kwargs):
    """Make verbose, human-readable JSON."""
    # TODO separators can be potentially exposed to the user as well
    kwargs.pop('separators', None)
    return to_json(a, indent=indent, sort_keys=sort_keys, separators=(',', ': '), **kwargs)


# CAUTION: Do not put non-string values here since they can have unwanted logical equality, such as 1.0 (equal to 1 and True) or 0.0 (equal to 0 and False).
_valid_bool_true = {'yes', 'on', 'true', '1'}
_valid_bool_false = {'no', 'off', 'false', '0'}


def to_bool(value: object) -> bool:
    """Convert well-known input values to a boolean value."""
    value_to_check: object

    if isinstance(value, str):
        value_to_check = value.lower()  # accept mixed case variants
    elif isinstance(value, int):  # bool is also an int
        value_to_check = str(value).lower()  # accept int (0, 1) and bool (True, False) -- not just string versions
    else:
        value_to_check = value

    try:
        if value_to_check in _valid_bool_true:
            return True

        if value_to_check in _valid_bool_false:
            return False

        # if we're still here, the value is unsupported- always fire a deprecation warning
        result = value_to_check == 1  # backwards compatibility with the old code which checked: value in ('yes', 'on', '1', 'true', 1)
    except TypeError:
        result = False

    # NB: update the doc string to reflect reality once this fallback is removed
    display.deprecated(
        msg=f'The `bool` filter coerced invalid value {value!r} ({native_type_name(value)}) to {result!r}.',
        version='2.23',
    )

    return result


def to_datetime(string, format="%Y-%m-%d %H:%M:%S"):
    return datetime.datetime.strptime(string, format)


def strftime(string_format, second=None, utc=False):
    """ return a date string using string. See https://docs.python.org/3/library/time.html#time.strftime for format """
    if utc:
        timefn = time.gmtime
    else:
        timefn = time.localtime
    if second is not None:
        try:
            second = float(second)
        except Exception:
            raise AnsibleFilterError('Invalid value for epoch value (%s)' % second)
    return time.strftime(string_format, timefn(second))


def quote(a):
    """ return its argument quoted for shell usage """
    if a is None:
        a = u''
    return shlex.quote(to_text(a))


def fileglob(pathname):
    """ return list of matched regular files for glob """
    return [g for g in glob.glob(pathname) if os.path.isfile(g)]


def regex_replace(value='', pattern='', replacement='', ignorecase=False, multiline=False, count=0, mandatory_count=0):
    """ Perform a `re.sub` returning a string """

    value = to_text(value, errors='surrogate_or_strict', nonstring='simplerepr')

    flags = 0
    if ignorecase:
        flags |= re.I
    if multiline:
        flags |= re.M
    _re = re.compile(pattern, flags=flags)
    (output, subs) = _re.subn(replacement, value, count=count)
    if mandatory_count and mandatory_count != subs:
        raise AnsibleFilterError("'%s' should match %d times, but matches %d times in '%s'"
                                 % (pattern, mandatory_count, count, value))
    return output


def regex_findall(value, regex, multiline=False, ignorecase=False):
    """ Perform re.findall and return the list of matches """

    value = to_text(value, errors='surrogate_or_strict', nonstring='simplerepr')

    flags = 0
    if ignorecase:
        flags |= re.I
    if multiline:
        flags |= re.M
    return re.findall(regex, value, flags)


def regex_search(value, regex, *args, **kwargs):
    """ Perform re.search and return the list of matches or a backref """

    value = to_text(value, errors='surrogate_or_strict', nonstring='simplerepr')

    groups = list()
    for arg in args:
        if arg.startswith('\\g'):
            match = re.match(r'\\g<(\S+)>', arg).group(1)
            groups.append(match)
        elif arg.startswith('\\'):
            match = int(re.match(r'\\(\d+)', arg).group(1))
            groups.append(match)
        else:
            raise AnsibleFilterError('Unknown argument')

    flags = 0
    if kwargs.get('ignorecase'):
        flags |= re.I
    if kwargs.get('multiline'):
        flags |= re.M

    match = re.search(regex, value, flags)
    if match:
        if not groups:
            return match.group()
        else:
            items = list()
            for item in groups:
                items.append(match.group(item))
            return items


def ternary(value, true_val, false_val, none_val=None):
    """  value ? true_val : false_val """
    if value is None and none_val is not None:
        return none_val
    elif bool(value):
        return true_val
    else:
        return false_val


def regex_escape(string, re_type='python'):
    """Escape all regular expressions special characters from STRING."""
    string = to_text(string, errors='surrogate_or_strict', nonstring='simplerepr')
    if re_type == 'python':
        return re.escape(string)
    elif re_type == 'posix_basic':
        # list of BRE special chars:
        # https://en.wikibooks.org/wiki/Regular_Expressions/POSIX_Basic_Regular_Expressions
        return regex_replace(string, r'([].[^$*\\])', r'\\\1')
    # TODO: implement posix_extended
    # It's similar to, but different from python regex, which is similar to,
    # but different from PCRE.  It's possible that re.escape would work here.
    # https://remram44.github.io/regex-cheatsheet/regex.html#programs
    elif re_type == 'posix_extended':
        raise AnsibleFilterError('Regex type (%s) not yet implemented' % re_type)
    else:
        raise AnsibleFilterError('Invalid regex type (%s)' % re_type)


def from_yaml(data):
    if data is None:
        return None

    if isinstance(data, str):
        return yaml.load(data, Loader=_yaml_loader.AnsibleInstrumentedLoader)  # type: ignore[arg-type]

    display.deprecated(f"The from_yaml filter ignored non-string input of type {native_type_name(data)!r}.", version='2.23', obj=data)
    return data


def from_yaml_all(data):
    if data is None:
        return []  # backward compatibility; ensure consistent result between classic/native Jinja for None/empty string input

    if isinstance(data, str):
        return yaml.load_all(data, Loader=_yaml_loader.AnsibleInstrumentedLoader)  # type: ignore[arg-type]

    display.deprecated(f"The from_yaml_all filter ignored non-string input of type {native_type_name(data)!r}.", version='2.23', obj=data)
    return data


@pass_environment
def rand(environment, end, start=None, step=None, seed=None):
    if seed is None:
        r = SystemRandom()
    else:
        r = Random(seed)
    if isinstance(end, integer_types):
        if not start:
            start = 0
        if not step:
            step = 1
        return r.randrange(start, end, step)
    elif hasattr(end, '__iter__'):
        if start or step:
            raise AnsibleFilterError('start and step can only be used with integer values')
        return r.choice(end)
    else:
        raise AnsibleFilterError('random can only be used on sequences and integers')


def randomize_list(mylist, seed=None):
    try:
        mylist = list(mylist)
        if seed:
            r = Random(seed)
            r.shuffle(mylist)
        else:
            shuffle(mylist)
    except Exception:
        pass
    return mylist


def get_hash(data, hashtype='sha1'):
    try:
        h = hashlib.new(hashtype)
    except Exception as e:
        # hash is not supported?
        raise AnsibleFilterError(e)

    h.update(to_bytes(data, errors='surrogate_or_strict'))
    return h.hexdigest()


def get_encrypted_password(password, hashtype='sha512', salt=None, salt_size=None, rounds=None, ident=None):
    passlib_mapping = {
        'md5': 'md5_crypt',
        'blowfish': 'bcrypt',
        'sha256': 'sha256_crypt',
        'sha512': 'sha512_crypt',
    }

    hashtype = passlib_mapping.get(hashtype, hashtype)

    if PASSLIB_AVAILABLE and hashtype not in passlib_mapping and hashtype not in passlib_mapping.values():
        raise AnsibleFilterError(f"{hashtype} is not in the list of supported passlib algorithms: {', '.join(passlib_mapping)}")

    return do_encrypt(password, hashtype, salt=salt, salt_size=salt_size, rounds=rounds, ident=ident)


def to_uuid(string, namespace=UUID_NAMESPACE_ANSIBLE):
    uuid_namespace = namespace
    if not isinstance(uuid_namespace, uuid.UUID):
        try:
            uuid_namespace = uuid.UUID(namespace)
        except (AttributeError, ValueError) as e:
            raise AnsibleFilterError("Invalid value '%s' for 'namespace': %s" % (to_native(namespace), to_native(e)))
    # uuid.uuid5() requires bytes on Python 2 and bytes or text or Python 3
    return to_text(uuid.uuid5(uuid_namespace, to_native(string, errors='surrogate_or_strict')))


@accept_args_markers
def mandatory(a: object, msg: str | None = None) -> object:
    """Make a variable mandatory."""
    # DTFIX-FUTURE: deprecate this filter; there are much better ways via undef, etc...
    #                also remember to remove unit test checking for _undefined_name
    if isinstance(a, UndefinedMarker):
        if msg is not None:
            raise AnsibleFilterError(to_text(msg))

        if a._undefined_name is not None:
            name = f'{to_text(a._undefined_name)!r} '
        else:
            name = ''

        raise AnsibleFilterError(f"Mandatory variable {name}not defined.")

    return a


def combine(*terms, **kwargs):
    recursive = kwargs.pop('recursive', False)
    list_merge = kwargs.pop('list_merge', 'replace')
    if kwargs:
        raise AnsibleFilterError("'recursive' and 'list_merge' are the only valid keyword arguments")

    # allow the user to do `[dict1, dict2, ...] | combine`
    dictionaries = flatten(terms, levels=1)

    if not dictionaries:
        return {}

    if len(dictionaries) == 1:
        return dictionaries[0]

    # merge all the dicts so that the dict at the end of the array have precedence
    # over the dict at the beginning.
    # we merge the dicts from the highest to the lowest priority because there is
    # a huge probability that the lowest priority dict will be the biggest in size
    # (as the low prio dict will hold the "default" values and the others will be "patches")
    # and merge_hash create a copy of it's first argument.
    # so high/right -> low/left is more efficient than low/left -> high/right
    high_to_low_prio_dict_iterator = reversed(dictionaries)
    result = next(high_to_low_prio_dict_iterator)
    for dictionary in high_to_low_prio_dict_iterator:
        result = merge_hash(dictionary, result, recursive, list_merge)

    return result


def comment(text, style='plain', **kw):
    # Predefined comment types
    comment_styles = {
        'plain': {
            'decoration': '# '
        },
        'erlang': {
            'decoration': '% '
        },
        'c': {
            'decoration': '// '
        },
        'cblock': {
            'beginning': '/*',
            'decoration': ' * ',
            'end': ' */'
        },
        'xml': {
            'beginning': '<!--',
            'decoration': ' - ',
            'end': '-->'
        }
    }

    if style not in comment_styles:
        raise AnsibleTemplatePluginError(
            message=f"Invalid style {style!r}.",
            help_text=f"Available styles: {', '.join(comment_styles)}",
            obj=style,
        )

    # Pointer to the right comment type
    style_params = comment_styles[style]

    if 'decoration' in kw:
        prepostfix = kw['decoration']
    else:
        prepostfix = style_params['decoration']

    # Default params
    p = {
        'newline': '\n',
        'beginning': '',
        'prefix': (prepostfix).rstrip(),
        'prefix_count': 1,
        'decoration': '',
        'postfix': (prepostfix).rstrip(),
        'postfix_count': 1,
        'end': ''
    }

    # Update default params
    p.update(style_params)
    p.update(kw)

    # Compose substrings for the final string
    str_beginning = ''
    if p['beginning']:
        str_beginning = "%s%s" % (p['beginning'], p['newline'])
    str_prefix = ''
    if p['prefix']:
        if p['prefix'] != p['newline']:
            str_prefix = str(
                "%s%s" % (p['prefix'], p['newline'])) * int(p['prefix_count'])
        else:
            str_prefix = str(
                "%s" % (p['newline'])) * int(p['prefix_count'])
    str_text = ("%s%s" % (
        p['decoration'],
        # Prepend each line of the text with the decorator
        text.replace(
            p['newline'], "%s%s" % (p['newline'], p['decoration'])))).replace(
                # Remove trailing spaces when only decorator is on the line
                "%s%s" % (p['decoration'], p['newline']),
                "%s%s" % (p['decoration'].rstrip(), p['newline']))
    str_postfix = p['newline'].join(
        [''] + [p['postfix'] for x in range(p['postfix_count'])])
    str_end = ''
    if p['end']:
        str_end = "%s%s" % (p['newline'], p['end'])

    # Return the final string
    return "%s%s%s%s%s" % (
        str_beginning,
        str_prefix,
        str_text,
        str_postfix,
        str_end)


@pass_environment
def extract(environment: Environment, item, container, morekeys=None):
    if morekeys is None:
        keys = [item]
    elif isinstance(morekeys, list):
        keys = [item] + morekeys
    else:
        keys = [item, morekeys]

    value = container

    for key in keys:
        try:
            value = environment.getitem(value, key)
        except MarkerError as ex:
            value = ex.source

    return value


def b64encode(string, encoding='utf-8', urlsafe=False):
    func = base64.b64encode
    if urlsafe:
        func = base64.urlsafe_b64encode
    return to_text(func(to_bytes(string, encoding=encoding, errors='surrogate_or_strict')))


def b64decode(string, encoding='utf-8', urlsafe=False):
    func = base64.b64decode
    if urlsafe:
        func = base64.urlsafe_b64decode
    return to_text(func(to_bytes(string, errors='surrogate_or_strict')), encoding=encoding)


def flatten(mylist, levels=None, skip_nulls=True):

    ret = []
    for element in mylist:
        if skip_nulls and element in (None, 'None', 'null'):
            # ignore null items
            continue
        elif is_sequence(element):
            if levels is None:
                ret.extend(flatten(element, skip_nulls=skip_nulls))
            elif levels >= 1:
                # decrement as we go down the stack
                ret.extend(flatten(element, levels=(int(levels) - 1), skip_nulls=skip_nulls))
            else:
                ret.append(element)
        else:
            ret.append(element)

    return ret


def subelements(obj, subelements, skip_missing=False):
    """Accepts a dict or list of dicts, and a dotted accessor and produces a product
    of the element and the results of the dotted accessor

    >>> obj = [{"name": "alice", "groups": ["wheel"], "authorized": ["/tmp/alice/onekey.pub"]}]
    >>> subelements(obj, 'groups')
    [({'name': 'alice', 'groups': ['wheel'], 'authorized': ['/tmp/alice/onekey.pub']}, 'wheel')]

    """
    if isinstance(obj, dict):
        element_list = list(obj.values())
    elif isinstance(obj, list):
        element_list = obj[:]
    else:
        raise AnsibleFilterError('obj must be a list of dicts or a nested dict')

    if isinstance(subelements, list):
        subelement_list = subelements[:]
    elif isinstance(subelements, string_types):
        subelement_list = subelements.split('.')
    else:
        raise AnsibleTypeError('subelements must be a list or a string')

    results = []

    for element in element_list:
        values = element
        for subelement in subelement_list:
            try:
                values = values[subelement]
            except KeyError:
                if skip_missing:
                    values = []
                    break
                raise AnsibleFilterError("could not find %r key in iterated item %r" % (subelement, values))
            except TypeError as ex:
                raise AnsibleTypeError("the key %s should point to a dictionary, got '%s'" % (subelement, values)) from ex
        if not isinstance(values, list):
            raise AnsibleTypeError("the key %r should point to a list, got %r" % (subelement, values))

        for value in values:
            results.append((element, value))

    return results


def dict_to_list_of_dict_key_value_elements(mydict, key_name='key', value_name='value'):
    """ takes a dictionary and transforms it into a list of dictionaries,
        with each having a 'key' and 'value' keys that correspond to the keys and values of the original """

    if not isinstance(mydict, Mapping):
        raise AnsibleTypeError("dict2items requires a dictionary, got %s instead." % type(mydict))

    ret = []
    for key in mydict:
        ret.append({key_name: key, value_name: mydict[key]})
    return ret


def list_of_dict_key_value_elements_to_dict(mylist, key_name='key', value_name='value'):
    """ takes a list of dicts with each having a 'key' and 'value' keys, and transforms the list into a dictionary,
        effectively as the reverse of dict2items """

    if not is_sequence(mylist):
        raise AnsibleTypeError("items2dict requires a list, got %s instead." % type(mylist))

    try:
        return dict((item[key_name], item[value_name]) for item in mylist)
    except KeyError:
        raise AnsibleTypeError(
            "items2dict requires each dictionary in the list to contain the keys '%s' and '%s', got %s instead."
            % (key_name, value_name, mylist)
        )
    except TypeError:
        raise AnsibleTypeError("items2dict requires a list of dictionaries, got %s instead." % mylist)


def path_join(paths):
    """ takes a sequence or a string, and return a concatenation
        of the different members """
    if isinstance(paths, string_types):
        return os.path.join(paths)
    if is_sequence(paths):
        return os.path.join(*paths)
    raise AnsibleTypeError("|path_join expects string or sequence, got %s instead." % type(paths))


def commonpath(paths):
    """
    Retrieve the longest common path from the given list.

    :param paths: A list of file system paths.
    :type paths: List[str]
    :returns: The longest common path.
    :rtype: str
    """
    if not is_sequence(paths):
        raise AnsibleTypeError("|commonpath expects sequence, got %s instead." % type(paths))

    return os.path.commonpath(paths)


class GroupTuple(t.NamedTuple):
    """
    Custom named tuple for the groupby filter with a public interface; silently ignored by unknown type checks.
    This matches the internal implementation of the _GroupTuple returned by Jinja's built-in groupby filter.
    """

    grouper: t.Any
    list: list[t.Any]

    def __repr__(self) -> str:
        return tuple.__repr__(self)


_lazy_containers.register_known_types(GroupTuple)


@pass_environment
def _cleansed_groupby(*args, **kwargs):
    res = sync_do_groupby(*args, **kwargs)
    res = [GroupTuple(grouper=g.grouper, list=g.list) for g in res]

    return res

# DTFIX-FUTURE: make these dumb wrappers more dynamic


@accept_args_markers
def ansible_default(
    value: t.Any,
    default_value: t.Any = '',
    boolean: bool = False,
) -> t.Any:
    """Updated `default` filter that only coalesces classic undefined objects; other Undefined-derived types (eg, ErrorMarker) pass through."""
    validate_arg_type('boolean', boolean, bool)

    if isinstance(value, UndefinedMarker):
        return default_value

    if boolean and not value:
        return default_value

    return value


@accept_lazy_markers
@functools.wraps(do_map)
def wrapped_map(*args, **kwargs) -> t.Any:
    return do_map(*args, **kwargs)


@accept_lazy_markers
@functools.wraps(do_select)
def wrapped_select(*args, **kwargs) -> t.Any:
    return do_select(*args, **kwargs)


@accept_lazy_markers
@functools.wraps(do_selectattr)
def wrapped_selectattr(*args, **kwargs) -> t.Any:
    return do_selectattr(*args, **kwargs)


@accept_lazy_markers
@functools.wraps(do_reject)
def wrapped_reject(*args, **kwargs) -> t.Any:
    return do_reject(*args, **kwargs)


@accept_lazy_markers
@functools.wraps(do_rejectattr)
def wrapped_rejectattr(*args, **kwargs) -> t.Any:
    return do_rejectattr(*args, **kwargs)


@accept_args_markers
def type_debug(obj: object) -> str:
    return native_type_name(obj)


class FilterModule(object):
    """ Ansible core jinja2 filters """

    def filters(self):
        return {
            # base 64
            'b64decode': b64decode,
            'b64encode': b64encode,

            # uuid
            'to_uuid': to_uuid,

            # json
            'to_json': to_json,
            'to_nice_json': to_nice_json,
            'from_json': from_json,

            # yaml
            'to_yaml': to_yaml,
            'to_nice_yaml': to_nice_yaml,
            'from_yaml': from_yaml,
            'from_yaml_all': from_yaml_all,

            # path
            'basename': partial(unicode_wrap, os.path.basename),
            'dirname': partial(unicode_wrap, os.path.dirname),
            'expanduser': partial(unicode_wrap, os.path.expanduser),
            'expandvars': partial(unicode_wrap, os.path.expandvars),
            'path_join': path_join,
            'realpath': partial(unicode_wrap, os.path.realpath),
            'relpath': partial(unicode_wrap, os.path.relpath),
            'splitext': partial(unicode_wrap, os.path.splitext),
            'win_basename': partial(unicode_wrap, ntpath.basename),
            'win_dirname': partial(unicode_wrap, ntpath.dirname),
            'win_splitdrive': partial(unicode_wrap, ntpath.splitdrive),
            'commonpath': commonpath,
            'normpath': partial(unicode_wrap, os.path.normpath),

            # file glob
            'fileglob': fileglob,

            # types
            'bool': to_bool,
            'to_datetime': to_datetime,

            # date formatting
            'strftime': strftime,

            # quote string for shell usage
            'quote': quote,

            # hash filters
            # md5 hex digest of string
            'md5': md5s,
            # sha1 hex digest of string
            'sha1': checksum_s,
            # checksum of string as used by ansible for checksumming files
            'checksum': checksum_s,
            # generic hashing
            'password_hash': get_encrypted_password,
            'hash': get_hash,

            # regex
            'regex_replace': regex_replace,
            'regex_escape': regex_escape,
            'regex_search': regex_search,
            'regex_findall': regex_findall,

            # ? : ;
            'ternary': ternary,

            # random stuff
            'random': rand,
            'shuffle': randomize_list,

            # undefined
            'mandatory': mandatory,

            # comment-style decoration
            'comment': comment,

            # debug
            'type_debug': type_debug,

            # Data structures
            'combine': combine,
            'extract': extract,
            'flatten': flatten,
            'dict2items': dict_to_list_of_dict_key_value_elements,
            'items2dict': list_of_dict_key_value_elements_to_dict,
            'subelements': subelements,
            'split': partial(unicode_wrap, text_type.split),
            # FDI038 - replace this with a standard type compat shim
            'groupby': _cleansed_groupby,

            # Jinja builtins that need special arg handling
            'd': ansible_default,  # replaces the implementation instead of wrapping it
            'default': ansible_default,  # replaces the implementation instead of wrapping it
            'map': wrapped_map,
            'select': wrapped_select,
            'selectattr': wrapped_selectattr,
            'reject': wrapped_reject,
            'rejectattr': wrapped_rejectattr,
        }
