"""A tool for installing test requirements on the controller and target host."""

from __future__ import annotations

# pylint: disable=wrong-import-position

import resource

# Setting a low soft RLIMIT_NOFILE value will improve the performance of subprocess.Popen on Python 2.x when close_fds=True.
# This will affect all Python subprocesses. It will also affect the current Python process if set before subprocess is imported for the first time.
SOFT_RLIMIT_NOFILE = 1024

CURRENT_RLIMIT_NOFILE = resource.getrlimit(resource.RLIMIT_NOFILE)
DESIRED_RLIMIT_NOFILE = (SOFT_RLIMIT_NOFILE, CURRENT_RLIMIT_NOFILE[1])

if DESIRED_RLIMIT_NOFILE < CURRENT_RLIMIT_NOFILE:
    resource.setrlimit(resource.RLIMIT_NOFILE, DESIRED_RLIMIT_NOFILE)
    CURRENT_RLIMIT_NOFILE = DESIRED_RLIMIT_NOFILE

import base64
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import typing as t
import urllib.request

ENCODING = 'utf-8'

Text = type(u'')

VERBOSITY = 0
CONSOLE = sys.stderr


def main():  # type: () -> None
    """Main program entry point."""
    global VERBOSITY  # pylint: disable=global-statement

    payload = json.loads(to_text(base64.b64decode(PAYLOAD)))

    VERBOSITY = payload['verbosity']

    script = payload['script']
    commands = payload['commands']

    with tempfile.NamedTemporaryFile(prefix='ansible-test-', suffix='-pip.py') as pip:
        pip.write(to_bytes(script))
        pip.flush()

        for name, options in commands:
            try:
                globals()[name](pip.name, options)
            except ApplicationError as ex:
                print(ex)
                sys.exit(1)


# noinspection PyUnusedLocal
def bootstrap(pip: str, options: dict[str, t.Any]) -> None:
    """Bootstrap pip and related packages in an empty virtual environment."""
    pip_version = options['pip_version']
    packages = options['packages']
    setuptools = options['setuptools']
    wheel = options['wheel']

    del options

    url = 'https://ci-files.testing.ansible.com/ansible-test/get-pip-%s.py' % pip_version
    cache_path = os.path.expanduser('~/.ansible/test/cache/get_pip_%s.py' % pip_version.replace(".", "_"))
    temp_path = cache_path + '.download'

    if os.path.exists(cache_path):
        log('Using cached pip %s bootstrap script: %s' % (pip_version, cache_path))
    else:
        log('Downloading pip %s bootstrap script: %s' % (pip_version, url))

        make_dirs(os.path.dirname(cache_path))

        try:
            download_file(url, temp_path)
        except Exception as ex:
            raise ApplicationError(("""
Download failed: %s

The bootstrap script can be manually downloaded and saved to: %s

If you're behind a proxy, consider commenting on the following GitHub issue:

https://github.com/ansible/ansible/issues/77304
""" % (ex, cache_path)).strip())

        shutil.move(temp_path, cache_path)

        log('Cached pip %s bootstrap script: %s' % (pip_version, cache_path))

    env = common_pip_environment()
    env.update(GET_PIP=cache_path)

    pip_options = common_pip_options()
    pip_options.extend(packages)

    if not setuptools:
        pip_options.append('--no-setuptools')

    if not wheel:
        pip_options.append('--no-wheel')

    command = [sys.executable, pip] + pip_options

    execute_command(command, env=env)


def install(pip: str, options: dict[str, t.Any]) -> None:
    """Perform a pip install."""
    requirements = options['requirements']
    constraints = options['constraints']
    packages = options['packages']

    del options

    tempdir = tempfile.mkdtemp(prefix='ansible-test-', suffix='-requirements')

    try:
        pip_options = common_pip_options()
        pip_options.extend(packages)

        for path, content in requirements:
            if path.split(os.sep)[0] in ('test', 'requirements'):
                # Support for pre-build is currently limited to requirements embedded in ansible-test and those used by ansible-core.
                # Requirements from ansible-core can be found in the 'test' and 'requirements' directories.
                # This feature will probably be extended to support collections after further testing.
                # Requirements from collections can be found in the 'tests' directory.
                for pre_build in parse_pre_build_instructions(content):
                    pre_build.execute(pip)

            write_text_file(os.path.join(tempdir, path), content, True)
            pip_options.extend(['-r', path])

        for path, content in constraints:
            write_text_file(os.path.join(tempdir, path), content, True)
            pip_options.extend(['-c', path])

        command = [sys.executable, pip, 'install'] + pip_options

        env = common_pip_environment()

        execute_command(command, env=env, cwd=tempdir)
    finally:
        remove_tree(tempdir)


class PreBuild:
    """Parsed pre-build instructions."""

    def __init__(self, requirement):  # type: (str) -> None
        self.requirement = requirement
        self.constraints = []  # type: list[str]

    def execute(self, pip):  # type: (str) -> None
        """Execute these pre-build instructions."""
        tempdir = tempfile.mkdtemp(prefix='ansible-test-', suffix='-pre-build')

        try:
            pip_options = common_pip_options()
            pip_options.append(self.requirement)

            constraints = '\n'.join(self.constraints) + '\n'
            constraints_path = os.path.join(tempdir, 'constraints.txt')

            write_text_file(constraints_path, constraints, True)

            env = common_pip_environment()
            env.update(PIP_CONSTRAINT=constraints_path)

            command = [sys.executable, pip, 'wheel'] + pip_options

            execute_command(command, env=env, cwd=tempdir)
        finally:
            remove_tree(tempdir)


def parse_pre_build_instructions(requirements):  # type: (str) -> list[PreBuild]
    """Parse the given pip requirements and return a list of extracted pre-build instructions."""
    # CAUTION: This code must be kept in sync with the sanity test hashing code in:
    #          test/lib/ansible_test/_internal/commands/sanity/__init__.py

    pre_build_prefix = '# pre-build '
    pre_build_requirement_prefix = pre_build_prefix + 'requirement: '
    pre_build_constraint_prefix = pre_build_prefix + 'constraint: '

    lines = requirements.splitlines()
    pre_build_lines = [line for line in lines if line.startswith(pre_build_prefix)]

    instructions = []  # type: list[PreBuild]

    for line in pre_build_lines:
        if line.startswith(pre_build_requirement_prefix):
            instructions.append(PreBuild(line[len(pre_build_requirement_prefix):]))
        elif line.startswith(pre_build_constraint_prefix):
            instructions[-1].constraints.append(line[len(pre_build_constraint_prefix):])
        else:
            raise RuntimeError('Unsupported pre-build comment: ' + line)

    return instructions


def uninstall(pip: str, options: dict[str, t.Any]) -> None:
    """Perform a pip uninstall."""
    packages = options['packages']
    ignore_errors = options['ignore_errors']

    del options

    pip_options = common_pip_options()
    pip_options.extend(packages)

    command = [sys.executable, pip, 'uninstall', '-y'] + pip_options

    env = common_pip_environment()

    try:
        execute_command(command, env=env, capture=True)
    except SubprocessError:
        if not ignore_errors:
            raise


# noinspection PyUnusedLocal
def version(pip: str, options: dict[str, t.Any]) -> None:
    """Report the pip version."""
    del options

    pip_options = common_pip_options()

    command = [sys.executable, pip, '-V'] + pip_options

    env = common_pip_environment()

    execute_command(command, env=env, capture=True)


def common_pip_environment():  # type: () -> t.Dict[str, str]
    """Return common environment variables used to run pip."""
    env = os.environ.copy()

    # When ansible-test installs requirements outside a virtual environment, it does so under one of two conditions:
    # 1) The environment is an ephemeral one provisioned by ansible-test.
    # 2) The user has provided the `--requirements` option to force installation of requirements.
    # It seems reasonable to bypass PEP 668 checks in both of these cases.
    # Doing so with an environment variable allows it to work under any version of pip which supports it, without breaking older versions.
    # NOTE: pip version 23.0 enforces PEP 668 but does not support the override, in which case upgrading pip is required.
    env.update(PIP_BREAK_SYSTEM_PACKAGES='1')

    return env


def common_pip_options():  # type: () -> t.List[str]
    """Return a list of common pip options."""
    return [
        '--disable-pip-version-check',
    ]


def devnull():  # type: () -> t.IO[bytes]
    """Return a file object that references devnull."""
    try:
        return devnull.file  # type: ignore[attr-defined]
    except AttributeError:
        devnull.file = open(os.devnull, 'w+b')  # type: ignore[attr-defined]  # pylint: disable=consider-using-with

    return devnull.file  # type: ignore[attr-defined]


def download_file(url, path):  # type: (str, str) -> None
    """Download the given URL to the specified file path."""
    with open(to_bytes(path), 'wb') as saved_file:
        with contextlib.closing(urllib.request.urlopen(url)) as download:
            shutil.copyfileobj(download, saved_file)


class ApplicationError(Exception):
    """Base class for application exceptions."""


class SubprocessError(ApplicationError):
    """A command returned a non-zero status."""

    def __init__(self, cmd, status, stdout, stderr):  # type: (t.List[str], int, str, str) -> None
        message = 'A command failed with status %d: %s' % (status, shlex.join(cmd))

        if stderr:
            message += '\n>>> Standard Error\n%s' % stderr.strip()

        if stdout:
            message += '\n>>> Standard Output\n%s' % stdout.strip()

        super(SubprocessError, self).__init__(message)


def log(message, verbosity=0):  # type: (str, int) -> None
    """Log a message to the console if the verbosity is high enough."""
    if verbosity > VERBOSITY:
        return

    print(message, file=CONSOLE)
    CONSOLE.flush()


def execute_command(cmd, cwd=None, capture=False, env=None):  # type: (t.List[str], t.Optional[str], bool, t.Optional[t.Dict[str, str]]) -> None
    """Execute the specified command."""
    log('Execute command: %s' % shlex.join(cmd), verbosity=1)

    cmd_bytes = [to_bytes(c) for c in cmd]

    if capture:
        stdout = subprocess.PIPE
        stderr = subprocess.PIPE
    else:
        stdout = None
        stderr = None

    cwd_bytes = to_optional_bytes(cwd)
    process = subprocess.Popen(cmd_bytes, cwd=cwd_bytes, stdin=devnull(), stdout=stdout, stderr=stderr, env=env)  # pylint: disable=consider-using-with
    stdout_bytes, stderr_bytes = process.communicate()
    stdout_text = to_optional_text(stdout_bytes) or u''
    stderr_text = to_optional_text(stderr_bytes) or u''

    if process.returncode != 0:
        raise SubprocessError(cmd, process.returncode, stdout_text, stderr_text)


def write_text_file(path, content, create_directories=False):  # type: (str, str, bool) -> None
    """Write the given text content to the specified path, optionally creating missing directories."""
    if create_directories:
        make_dirs(os.path.dirname(path))

    with open_binary_file(path, 'wb') as file_obj:
        file_obj.write(to_bytes(content))


def remove_tree(path):  # type: (str) -> None
    """Remove the specified directory tree."""
    try:
        shutil.rmtree(to_bytes(path))
    except FileNotFoundError:
        pass


def make_dirs(path):  # type: (str) -> None
    """Create a directory at path, including any necessary parent directories."""
    os.makedirs(to_bytes(path), exist_ok=True)


def open_binary_file(path, mode='rb'):  # type: (str, str) -> t.IO[bytes]
    """Open the given path for binary access."""
    if 'b' not in mode:
        raise Exception('mode must include "b" for binary files: %s' % mode)

    return io.open(to_bytes(path), mode)  # pylint: disable=consider-using-with,unspecified-encoding


def to_optional_bytes(value, errors='strict'):  # type: (t.Optional[str | bytes], str) -> t.Optional[bytes]
    """Return the given value as bytes encoded using UTF-8 if not already bytes, or None if the value is None."""
    return None if value is None else to_bytes(value, errors)


def to_optional_text(value, errors='strict'):  # type: (t.Optional[str | bytes], str) -> t.Optional[t.Text]
    """Return the given value as text decoded using UTF-8 if not already text, or None if the value is None."""
    return None if value is None else to_text(value, errors)


def to_bytes(value, errors='strict'):  # type: (str | bytes, str) -> bytes
    """Return the given value as bytes encoded using UTF-8 if not already bytes."""
    if isinstance(value, bytes):
        return value

    if isinstance(value, Text):
        return value.encode(ENCODING, errors)

    raise Exception('value is not bytes or text: %s' % type(value))


def to_text(value, errors='strict'):  # type: (str | bytes, str) -> t.Text
    """Return the given value as text decoded using UTF-8 if not already text."""
    if isinstance(value, bytes):
        return value.decode(ENCODING, errors)

    if isinstance(value, Text):
        return value

    raise Exception('value is not bytes or text: %s' % type(value))


PAYLOAD = b'{payload}'  # base-64 encoded JSON payload which will be populated before this script is executed

if __name__ == '__main__':
    main()
