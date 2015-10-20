from configparser import ConfigParser
from unittest import mock
import json
import asyncio

import pytest
from zake.fake_client import FakeClient
from kazoo.client import KazooState

from zgres import deadman
from . import FakeSleeper

@pytest.mark.asyncio
async def test_functional():
    """Test as much of the whole stack as we can."""
    config = {'deadman': {
        'plugins': 'zgres#zookeeper,zgres#apt,zgres#ec2-snapshot,zgres#ec2,zgres#follow-the-leader',
        }}
    zk = FakeClient()
    with mock.patch('zgres.zookeeper.KazooClient') as KazooClient, \
            mock.patch('zgres.ec2.boto.utils.get_instance_metadata'):
        KazooClient.return_value = zk
        app = deadman.App(config)
