# (c) 2020 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import annotations

DOCUMENTATION = """
    name: unvault
    author: Ansible Core Team
    version_added: "2.10"
    short_description: read vaulted file(s) contents
    description:
        - This lookup returns the contents from vaulted (or not) file(s) on the Ansible controller's file system.
    options:
      _terms:
        description: path(s) of files to read
        required: True
    notes:
      - This lookup does not understand 'globbing' nor shell environment variables.
    seealso:
      - ref: playbook_task_paths
        description: Search paths used for relative files.
"""

EXAMPLES = """
- ansible.builtin.debug: msg="the value of foo.txt is {{ lookup('ansible.builtin.unvault', '/etc/foo.txt') | string | trim }}"
"""

RETURN = """
  _raw:
    description:
      - content of file(s) as bytes
    type: list
    elements: raw
"""

from ansible.errors import AnsibleParserError
from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display

display = Display()


class LookupModule(LookupBase):

    def run(self, terms, variables=None, **kwargs):

        ret = []

        self.set_options(var_options=variables, direct=kwargs)

        for term in terms:
            display.debug("Unvault lookup term: %s" % term)

            # Find the file in the expected search path
            lookupfile = self.find_file_in_search_path(variables, 'files', term)
            display.vvvv(u"Unvault lookup found %s" % lookupfile)
            if lookupfile:
                ret.append(self._loader.get_text_file_contents(lookupfile))
            else:
                raise AnsibleParserError('Unable to find file matching "%s" ' % term)

        return ret
