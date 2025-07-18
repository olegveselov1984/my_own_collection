
# Copyright: (c) 2014, Matthew Vernon <mcv21@cam.ac.uk>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations


DOCUMENTATION = r"""
---
module: known_hosts
short_description: Add or remove a host from the C(known_hosts) file
description:
   - The M(ansible.builtin.known_hosts) module lets you add or remove host keys from the C(known_hosts) file.
   - Starting at Ansible 2.2, multiple entries per host are allowed, but only one for each key type supported by ssh.
     This is useful if you're going to want to use the M(ansible.builtin.git) module over ssh, for example.
   - If you have a very large number of host keys to manage, you will find the M(ansible.builtin.template) module more useful.
version_added: "1.9"
options:
  name:
    aliases: [ 'host' ]
    description:
      - The host to add or remove (must match a host specified in key). It will be converted to lowercase so that C(ssh-keygen) can find it.
      - Must match with <hostname> or <ip> present in key attribute.
      - For custom SSH port, O(name) needs to specify port as well. See example section.
    type: str
    required: true
  key:
    description:
      - The SSH public host key, as a string.
      - Required if O(state=present), optional when O(state=absent), in which case all keys for the host are removed.
      - The key must be in the right format for SSH (see sshd(8), section "SSH_KNOWN_HOSTS FILE FORMAT").
      - Specifically, the key should not match the format that is found in an SSH pubkey file, but should rather have the hostname prepended to a
        line that includes the pubkey, the same way that it would appear in the known_hosts file. The value prepended to the line must also match
        the value of the name parameter.
      - Should be of format C(<hostname[,IP]> ssh-rsa <pubkey>).
      - For custom SSH port, O(key) needs to specify port as well. See example section.
    type: str
  path:
    description:
      - The known_hosts file to edit.
      - The known_hosts file will be created if needed. The rest of the path must exist prior to running the module.
    default: "~/.ssh/known_hosts"
    type: path
  hash_host:
    description:
      - Hash the hostname in the known_hosts file.
    type: bool
    default: "no"
    version_added: "2.3"
  state:
    description:
      - V(present) to add host keys.
      - V(absent) to remove host keys.
    choices: [ "absent", "present" ]
    default: "present"
    type: str
attributes:
  check_mode:
    support: full
  diff_mode:
    support: full
  platform:
    platforms: posix
extends_documentation_fragment:
  - action_common_attributes
author:
- Matthew Vernon (@mcv21)
"""

EXAMPLES = r"""
- name: Tell the host about our servers it might want to ssh to
  ansible.builtin.known_hosts:
    path: /etc/ssh/ssh_known_hosts
    name: foo.com.invalid
    key: "{{ lookup('ansible.builtin.file', 'pubkeys/foo.com.invalid') }}"

- name: Another way to call known_hosts
  ansible.builtin.known_hosts:
    name: host1.example.com   # or 10.9.8.77
    key: host1.example.com,10.9.8.77 ssh-rsa ASDeararAIUHI324324  # some key gibberish
    path: /etc/ssh/ssh_known_hosts
    state: present

- name: Add host with custom SSH port
  ansible.builtin.known_hosts:
    name: '[host1.example.com]:2222'
    key: '[host1.example.com]:2222 ssh-rsa ASDeararAIUHI324324' # some key gibberish
    path: /etc/ssh/ssh_known_hosts
    state: present
"""

# Makes sure public host keys are present or absent in the given known_hosts
# file.
#
# Arguments
# =========
#    name = hostname whose key should be added (alias: host)
#    key = line(s) to add to known_hosts file
#    path = the known_hosts file to edit (default: ~/.ssh/known_hosts)
#    hash_host = yes|no (default: no) hash the hostname in the known_hosts file
#    state = absent|present (default: present)

import base64
import copy
import hashlib
import hmac
import os
import os.path
import re
import tempfile

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.text.converters import to_bytes, to_native


def enforce_state(module, params):
    """
    Add or remove key.
    """

    results = dict(changed=False)
    host = params["name"].lower()
    key = params.get("key", None)
    path = params.get("path")
    hash_host = params.get("hash_host")
    state = params.get("state")
    # Find the ssh-keygen binary
    sshkeygen = module.get_bin_path("ssh-keygen", True)

    if not key and state != "absent":
        module.fail_json(msg="No key specified when adding a host")

    if key and hash_host:
        key = hash_host_key(host, key)

    # Trailing newline in files gets lost, so re-add if necessary
    if key and not key.endswith('\n'):
        key += '\n'

    sanity_check(module, host, key, sshkeygen)

    found, replace_or_add, found_line = search_for_host_key(module, host, key, path, sshkeygen)

    results['diff'] = compute_diff(path, found_line, replace_or_add, state, key)

    # check if we are trying to remove a non matching key,
    # in that case return with no change to the host
    if state == 'absent' and not found_line and key:
        return results

    # We will change state if found==True & state!="present"
    # or found==False & state=="present"
    # i.e found XOR (state=="present")
    # Alternatively, if replace is true (i.e. key present, and we must change
    # it)
    if module.check_mode:
        results['changed'] = replace_or_add or (state == "present") != found
        module.exit_json(**results)

    # Now do the work.

    # Only remove whole host if found and no key provided
    if found and not key and state == "absent":
        module.run_command([sshkeygen, '-R', host, '-f', path], check_rc=True)
        results['changed'] = True

    # Next, add a new (or replacing) entry
    if replace_or_add or found != (state == "present"):
        try:
            inf = open(path, "r")
        except FileNotFoundError:
            inf = None
        except OSError as ex:
            raise Exception(f"Failed to read {path!r}.") from ex
        try:
            with tempfile.NamedTemporaryFile(mode='w+', dir=os.path.dirname(path), delete=False) as outf:
                if inf is not None:
                    for line_number, line in enumerate(inf):
                        if found_line == (line_number + 1) and (replace_or_add or state == 'absent'):
                            continue  # skip this line to replace its key
                        outf.write(line)
                    inf.close()
                if state == 'present':
                    outf.write(key)
        except OSError as ex:
            raise Exception(f"Failed to write to file {path!r}.") from ex
        else:
            module.atomic_move(outf.name, path)

        results['changed'] = True

    return results


def sanity_check(module, host, key, sshkeygen):
    """Check supplied key is sensible

    host and key are parameters provided by the user; If the host
    provided is inconsistent with the key supplied, then this function
    quits, providing an error to the user.
    sshkeygen is the path to ssh-keygen, found earlier with get_bin_path
    """
    # If no key supplied, we're doing a removal, and have nothing to check here.
    if not key:
        return
    # Rather than parsing the key ourselves, get ssh-keygen to do it
    # (this is essential for hashed keys, but otherwise useful, as the
    # key question is whether ssh-keygen thinks the key matches the host).

    # The approach is to write the key to a temporary file,
    # and then attempt to look up the specified host in that file.

    if re.search(r'\S+(\s+)?,(\s+)?', host):
        module.fail_json(msg="Comma separated list of names is not supported. "
                             "Please pass a single name to lookup in the known_hosts file.")

    with tempfile.NamedTemporaryFile(mode='w+') as outf:
        try:
            outf.write(key)
            outf.flush()
        except OSError as ex:
            raise Exception(f"Failed to write to temporary file {outf.name!r}.") from ex

        sshkeygen_command = [sshkeygen, '-F', host, '-f', outf.name]
        rc, stdout, stderr = module.run_command(sshkeygen_command)

    if stdout == '':  # host not found
        module.fail_json(msg="Host parameter does not match hashed host field in supplied key")


def search_for_host_key(module, host, key, path, sshkeygen):
    """search_for_host_key(module,host,key,path,sshkeygen) -> (found,replace_or_add,found_line)

    Looks up host and keytype in the known_hosts file path; if it's there, looks to see
    if one of those entries matches key. Returns:
    found (Boolean): is host found in path?
    replace_or_add (Boolean): is the key in path different to that supplied by user?
    found_line (int or None): the line where a key of the same type was found
    if found=False, then replace is always False.
    sshkeygen is the path to ssh-keygen, found earlier with get_bin_path
    """
    if os.path.exists(path) is False:
        return False, False, None

    sshkeygen_command = [sshkeygen, '-F', host, '-f', path]

    # openssh >=6.4 has changed ssh-keygen behaviour such that it returns
    # 1 if no host is found, whereas previously it returned 0
    rc, stdout, stderr = module.run_command(sshkeygen_command, check_rc=False)
    if stdout == '' and stderr == '' and (rc == 0 or rc == 1):
        return False, False, None  # host not found, no other errors
    if rc != 0:  # something went wrong
        module.fail_json(msg="ssh-keygen failed (rc=%d, stdout='%s',stderr='%s')" % (rc, stdout, stderr))

    # If user supplied no key, we don't want to try and replace anything with it
    if not key:
        return True, False, None

    lines = stdout.split('\n')
    new_key = normalize_known_hosts_key(key)

    for lnum, l in enumerate(lines):
        if l == '':
            continue
        elif l[0] == '#':  # info output from ssh-keygen; contains the line number where key was found
            try:
                # This output format has been hardcoded in ssh-keygen since at least OpenSSH 4.0
                # It always outputs the non-localized comment before the found key
                found_line = int(re.search(r'found: line (\d+)', l).group(1))
            except IndexError:
                module.fail_json(msg="failed to parse output of ssh-keygen for line number: '%s'" % l)
        else:
            found_key = normalize_known_hosts_key(l)

            if 'options' in found_key and found_key['options'][:15] == '@cert-authority':
                if new_key == found_key:  # found a match
                    return True, False, found_line  # found exactly the same key, don't replace
            elif 'options' in found_key and found_key['options'][:7] == '@revoke':
                if new_key == found_key:  # found a match
                    return True, False, found_line  # found exactly the same key, don't replace
            else:
                if new_key['host'][:3] == '|1|' and found_key['host'][:3] == '|1|':  # do not change host hash if already hashed
                    new_key['host'] = found_key['host']
                if new_key == found_key:  # found a match
                    return True, False, found_line  # found exactly the same key, don't replace
                elif new_key['type'] == found_key['type']:  # found a different key for the same key type
                    return True, True, found_line

    # No match found, return found and replace, but no line
    return True, True, None


def hash_host_key(host, key):
    hmac_key = os.urandom(20)
    hashed_host = hmac.new(hmac_key, to_bytes(host), hashlib.sha1).digest()
    parts = key.strip().split()
    # @ indicates the optional marker field used for @cert-authority or @revoked
    i = 1 if parts[0][0] == '@' else 0
    parts[i] = '|1|%s|%s' % (to_native(base64.b64encode(hmac_key)), to_native(base64.b64encode(hashed_host)))
    return ' '.join(parts)


def normalize_known_hosts_key(key):
    """
    Transform a key, either taken from a known_host file or provided by the
    user, into a normalized form.
    The host part (which might include multiple hostnames or be hashed) gets
    replaced by the provided host. Also, any spurious information gets removed
    from the end (like the username@host tag usually present in hostkeys, but
    absent in known_hosts files)
    """
    key = key.strip()  # trim trailing newline
    k = key.split()
    d = dict()
    # The optional "marker" field, used for @cert-authority or @revoked
    if k[0][0] == '@':
        d['options'] = k[0]
        d['host'] = k[1]
        d['type'] = k[2]
        d['key'] = k[3]
    else:
        d['host'] = k[0]
        d['type'] = k[1]
        d['key'] = k[2]
    return d


def compute_diff(path, found_line, replace_or_add, state, key):
    diff = {
        'before_header': path,
        'after_header': path,
        'before': '',
        'after': '',
    }
    try:
        inf = open(path, "r")
    except FileNotFoundError:
        diff['before_header'] = '/dev/null'
    except OSError:
        pass
    else:
        diff['before'] = inf.read()
        inf.close()
    lines = diff['before'].splitlines(1)
    if (replace_or_add or state == 'absent') and found_line is not None and 1 <= found_line <= len(lines):
        del lines[found_line - 1]
    if state == 'present' and (replace_or_add or found_line is None):
        lines.append(key)
    diff['after'] = ''.join(lines)
    return diff


def main():

    module = AnsibleModule(
        argument_spec=dict(
            name=dict(required=True, type='str', aliases=['host']),
            key=dict(required=False, type='str', no_log=False),
            path=dict(default="~/.ssh/known_hosts", type='path'),
            hash_host=dict(required=False, type='bool', default=False),
            state=dict(default='present', choices=['absent', 'present']),
        ),
        supports_check_mode=True
    )

    # TODO: deprecate returning everything that was passed in
    results = copy.copy(module.params)
    results.update(enforce_state(module, module.params))
    module.exit_json(**results)


if __name__ == '__main__':
    main()
