# (c) 2017, ansible by Red Hat
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

# moved actual classes to __init__ kept here for backward compat with 3rd parties
from ansible.plugins.cache import BaseCacheModule, BaseFileCacheModule  # pylint: disable=unused-import

from ansible.utils.display import Display as _Display

_Display().deprecated(
    msg="The `ansible.plugins.cache.base` Python module is deprecated.",
    help_text="Import from `ansible.plugins.cache` instead.",
    version="2.23",
)
