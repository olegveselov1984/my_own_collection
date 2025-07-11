# Copyright: (c) 2023, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import annotations

from ansible.errors import AnsibleActionFail
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

VALID_BACKENDS = frozenset(("yum", "yum4", "dnf", "dnf4", "dnf5"))


class ActionModule(ActionBase):

    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        self._supports_check_mode = True
        self._supports_async = True

        result = super(ActionModule, self).run(tmp, task_vars)
        del tmp  # tmp no longer has any effect

        # Carry-over concept from the package action plugin
        if 'use' in self._task.args and 'use_backend' in self._task.args:
            raise AnsibleActionFail("parameters are mutually exclusive: ('use', 'use_backend')")

        module = self._task.args.get('use', self._task.args.get('use_backend', 'auto'))

        if module in {'yum', 'auto'}:
            try:
                # if we delegate, we should use delegated host's facts
                expr = "hostvars[delegate_to].ansible_facts.pkg_mgr" if self._task.delegate_to else "ansible_facts.pkg_mgr"
                module = self._templar.resolve_variable_expression(expr, local_variables=dict(delegate_to=self._task.delegate_to))
            except Exception:
                pass  # could not get it from template!

        if module not in VALID_BACKENDS:
            facts = self._execute_module(
                module_name="ansible.legacy.setup", module_args=dict(filter="ansible_pkg_mgr", gather_subset="!all"),
                task_vars=task_vars)

            if facts.get("failed", False):
                raise AnsibleActionFail(
                    f"Failed to fetch ansible_pkg_mgr to determine the dnf action backend: {facts.get('msg')}",
                    result=facts,
                )

            display.debug("Facts %s" % facts)
            module = facts.get("ansible_facts", {}).get("ansible_pkg_mgr", "auto")
            if (not self._task.delegate_to or self._task.delegate_facts) and module != 'auto':
                result['ansible_facts'] = {'pkg_mgr': module}

        if module not in VALID_BACKENDS:
            result.update(
                {
                    'failed': True,
                    'msg': ("Could not detect which major revision of dnf is in use, which is required to determine module backend.",
                            "You should manually specify use_backend to tell the module whether to use the dnf4 or dnf5 backend})"),
                }
            )

        else:
            if module in {"yum4", "dnf4"}:
                module = "dnf"

            # eliminate collisions with collections search while still allowing local override
            module = 'ansible.legacy.' + module

            if not self._shared_loader_obj.module_loader.has_plugin(module):
                result.update({'failed': True, 'msg': "Could not find a dnf module backend for %s." % module})
            else:
                new_module_args = self._task.args.copy()
                if 'use_backend' in new_module_args:
                    del new_module_args['use_backend']
                if 'use' in new_module_args:
                    del new_module_args['use']

                display.vvvv("Running %s as the backend for the dnf action plugin" % module)
                result.update(self._execute_module(
                    module_name=module, module_args=new_module_args, task_vars=task_vars, wrap_async=self._task.async_val))

        return result
