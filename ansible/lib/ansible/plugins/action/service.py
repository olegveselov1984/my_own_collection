# (c) 2015, Ansible Inc,
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

from ansible.errors import AnsibleActionFail
from ansible.executor.module_common import _apply_action_arg_defaults
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):

    TRANSFERS_FILES = False

    UNUSED_PARAMS = {
        'systemd': ['pattern', 'runlevel', 'sleep', 'arguments', 'args'],
    }

    # HACK: list of unqualified service manager names that are/were built-in, we'll prefix these with `ansible.legacy` to
    # avoid collisions with collections search
    BUILTIN_SVC_MGR_MODULES = set(['openwrt_init', 'service', 'systemd', 'sysvinit'])

    def run(self, tmp=None, task_vars=None):
        """ handler for package operations """

        self._supports_check_mode = True
        self._supports_async = True

        super(ActionModule, self).run(tmp, task_vars)
        del tmp  # tmp no longer has any effect

        module = self._task.args.get('use', 'auto').lower()

        if module == 'auto':
            try:
                # if we delegate, we should use delegated host's facts
                expr = "hostvars[delegate_to].ansible_facts.service_mgr" if self._task.delegate_to else "ansible_facts.service_mgr"
                module = self._templar.resolve_variable_expression(expr, local_variables=dict(delegate_to=self._task.delegate_to))
            except Exception:
                pass  # could not get it from template!

        try:
            if module == 'auto':
                facts = self._execute_module(
                    module_name='ansible.legacy.setup',
                    module_args=dict(gather_subset='!all', filter='ansible_service_mgr'), task_vars=task_vars)
                self._display.debug("Facts %s" % facts)
                module = facts.get('ansible_facts', {}).get('ansible_service_mgr', 'auto')

            if not module or module == 'auto' or not self._shared_loader_obj.module_loader.has_plugin(module):
                module = 'ansible.legacy.service'

            if module != 'auto':
                # run the 'service' module
                new_module_args = self._task.args.copy()
                if 'use' in new_module_args:
                    del new_module_args['use']

                if module in self.UNUSED_PARAMS:
                    for unused in self.UNUSED_PARAMS[module]:
                        if unused in new_module_args:
                            del new_module_args[unused]
                            self._display.warning('Ignoring "%s" as it is not used in "%s"' % (unused, module))

                # get defaults for specific module
                context = self._shared_loader_obj.module_loader.find_plugin_with_context(module, collection_list=self._task.collections)
                new_module_args = _apply_action_arg_defaults(context.resolved_fqcn, self._task, new_module_args, self._templar)

                # collection prefix known internal modules to avoid collisions from collections search, while still allowing library/ overrides
                if module in self.BUILTIN_SVC_MGR_MODULES:
                    module = 'ansible.legacy.' + module

                self._display.vvvv("Running %s" % module)
                return self._execute_module(module_name=module, module_args=new_module_args, task_vars=task_vars, wrap_async=self._task.async_val)
            else:
                raise AnsibleActionFail('Could not detect which service manager to use. Try gathering facts or setting the "use" option.')

        finally:
            if not self._task.async_val:
                self._remove_tmp_path(self._connection._shell.tmpdir)
