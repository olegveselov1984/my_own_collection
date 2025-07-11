#!/usr/bin/env bash

set -eux

# use default timeout
ANSIBLE_TIMEOUT='' ansible -m ping testhost -i ../../inventory "$@"

# env var is wrong type, this should be a fatal error pointing at the setting
ANSIBLE_TIMEOUT='lola' ansible -m ping testhost -i ../../inventory "$@" 2>&1 | grep "Config 'DEFAULT_TIMEOUT' from 'env: ANSIBLE_TIMEOUT' has an invalid value"

# https://github.com/ansible/ansible/issues/69577
ANSIBLE_REMOTE_TMP="$HOME/.ansible/directory_with_no_space"  ansible -m ping testhost -i ../../inventory "$@"

ANSIBLE_REMOTE_TMP="$HOME/.ansible/directory with space"  ansible -m ping testhost -i ../../inventory "$@"

ANSIBLE_CONFIG=nonexistent.cfg ansible-config dump --only-changed -v | grep 'No config file found; using defaults'

# https://github.com/ansible/ansible/pull/73715
ANSIBLE_CONFIG=inline_comment_ansible.cfg ansible-config dump --only-changed | grep "'ansibull'"

# test type headers are only displayed with --only-changed -t all for changed options
env -i PATH="$PATH" PYTHONPATH="$PYTHONPATH" ansible-config dump --only-changed -t all | grep -v "CONNECTION"
env -i PATH="$PATH" PYTHONPATH="$PYTHONPATH" ANSIBLE_SSH_PIPELINING=True ansible-config dump --only-changed -t all | grep "CONNECTION"

# test the config option validation
ansible-playbook validation.yml "$@"

# test types from config (just lists for now)
ANSIBLE_CONFIG=type_munging.cfg ansible-playbook types.yml "$@"

cleanup() {
	rm -f files/*.new.*
}

trap 'cleanup' EXIT

# check a-c init per format
for format in "vars" "ini" "env"
do
	ANSIBLE_LOOKUP_PLUGINS=./ ansible-config init types -t lookup -f "${format}" > "files/types.new.${format}"
	diff -u "files/types.${format}" "files/types.new.${format}"
done

# ensure we don't show default templates, but templated defaults
[ "$(ansible-config init |grep '={{' -c )" -eq 0 ]
