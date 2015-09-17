#!/usr/bin/python3
"""Apply a configuration file to a node

For example, we have the configuration of the databases in Zookeeper. We write
that config out to a JSON file:

    /var/lib/zgres/config/databases.json

But, then we need to use that config to reconfigure various services, so we
write the code to reconfigure them to executable hook files in:

    /var/lib/zgres/hooks/

To actually apply the config by running the hooks, we run:

    zk-apply

This will run the hooks in /var/lib/zgres/hooks/ in order passing as the first
argument the directory containing the config file. If a hook fails, it is
logged, but the next hooks are run anyway.

NOTE: It is VERY important that hooks be idempotent! they WILL be called multiple times
with the same configuration.
"""
import os
import json
import sys
import logging
import argparse
from subprocess import call, check_call
from collections import abc

#
# Hook Tools
#

_DEFAULT_PREFIX = '/var/lib/zgres/'

class Config(abc.Mapping):
    """A proxy object for the config directory which deserializes the config"""

    def __init__(self, config_dir=None):
        if config_dir is None:
            config_dir = _DEFAULT_PREFIX
        self._config_dir = config_dir
        self._cache = {}

    def __getitem__(self, name):
        file = os.path.join(self._config_dir, name)
        if not os.path.exists(file):
            raise KeyError(name)
        cached = self._cache.get(name, self)
        if cached is not self:
            return cached
        with open(file, 'r') as f:
            data = f.read()
        _, ext = os.path.splitext(file)
        if ext == '.json':
            data = json.loads(data)
        else:
            raise NotImplementedError("Don't know how to deserialize {} files".format(ext))
        self._cache[name] = data
        return data

    def __iter__(self):
        for k in os.listdir(self._config_dir):
            yield k

    def __len__(self):
        return len(os.listdir(self._config_dir))


def render_template(template, destination, **data):
    with open(template, 'r') as f:
        template = f.read()
    result = template.format(**data)
    if os.path.exists(destination):
        with open(destination, 'r') as f:
            existing = f.read()
        if existing == result:
            return False
    head, tail = os.path.split(destination)
    tmpfile = os.path.join(head, '.' + tail + '.zgres_tmp')
    with open(tmpfile, 'w') as f:
        f.write(result)
    os.rename(tmpfile, destination)
    return True

#
# Apply
#

def _run_hooks(hooks, cfg_dir):
    failures = 0
    for filename in sorted(os.listdir(hooks)):
        if filename.startswith('.'):
            continue
        hook = os.path.join(hooks, filename)
        if not os.access(hook, os.X_OK):
            logging.warn('Not running non-executable hook: {}'.format(hook))
            continue
        returncode = _run_one_hook(hook, cfg_dir)
        if returncode != 0:
            logging.error('Failure when running hook: {}'.format(hook))
            failures += 1
    return failures

def _run_one_hook(hook, path):
    # private function so tests can patch it
    return call([hook, path])

def _apply(_prefix=_DEFAULT_PREFIX):
    cfg_dir = os.path.join(_prefix, 'config')
    hooks = os.path.join(_prefix, 'hooks')
    failures = _run_hooks(hooks, cfg_dir)
    return failures

class Plugin:

    def __init__(self, name, config, zk):
        pass

    def conn_info(self, state):
        with open('/var/lib/zgres/config/databases.json.tmp', 'w') as f:
            f.write(json.dumps(state, sort_keys=True))
        os.rename('/var/lib/zgres/config/databases.json.tmp', '/var/lib/zgres/config/databases.json')
        check_call('zgres-apply') # apply the configuration to the machine

#
# Command Line Scripts
#

def _setup_logging():
    logging.basicConfig(level=logging.WARN)

def _parse_args(parser, argv):
    # TODO: add args for setting loglevel here
    args = parser.parse_args(args=argv[1:])
    _setup_logging()
    return args

def apply_cli(argv=sys.argv):
    parser = argparse.ArgumentParser(description='Apply all loaded, but outstanding configs')
    args = _parse_args(parser, argv)
    sys.exit(_apply())
