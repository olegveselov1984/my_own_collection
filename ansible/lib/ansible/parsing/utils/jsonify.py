# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
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

import json

from ansible.utils.display import Display

Display().deprecated(f'{__name__!r} is deprecated.', version='2.23', help_text='Call `json.dumps` directly instead.')


def jsonify(result, format=False):
    """Format JSON output."""

    if result is None:
        return "{}"

    indent = None
    if format:
        indent = 4

    try:
        return json.dumps(result, sort_keys=True, indent=indent, ensure_ascii=False)
    except UnicodeDecodeError:
        return json.dumps(result, sort_keys=True, indent=indent)
