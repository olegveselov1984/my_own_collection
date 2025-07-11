#!/usr/bin/python
from __future__ import annotations

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict(
        secret=dict(no_log=True, type='raw'),
        notsecret=dict(no_log=False, type='raw'),
    ))

    msg = "My secret is: (%s), but don't tell %s" % (module.params['secret'], module.params['notsecret'])
    module.exit_json(
        msg=msg,
        changed=bool(module.params['secret'] == module.params['notsecret']),
        secret=module.params['secret'],
        notsecret=module.params['notsecret'],
    )


if __name__ == '__main__':
    main()
