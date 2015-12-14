import sys
import time
import uuid
from copy import deepcopy
import asyncio
import logging
import argparse

import zgres.plugin
import zgres.config
from zgres import utils

_missing = object()

_PLUGIN_API = [
        # Run any initialization code plugins need. Allways called first. Return value ignored
        dict(name='initialize',
            required=False,
            type='multiple'),
        # Get the id of this postgresql cluster
        dict(name='get_my_id',
            required=True,
            type='single'),

        dict(name='notify_state', # subscribe to changes in cluster state
            required=False,
            type='multiple'),
        dict(name='notify_conn_info', # subscribe to changes in cluster connection info
            required=False,
            type='multiple'),
        dict(name='master_lock_changed', # subscribe to changes in cluster master
            required=False,
            type='multiple'),
        
        dict(name='veto_takeover', # passed the state just before state is updated in the DCS, return True if we are not willing to takeover. This will result in the "willing" key in the state being None. The veto should only take into account values in the passed state object.
            required=False,
            type='multiple'),
        dict(name='best_replicas', # passed an iterator of the "willing" replicas (i.e. replicas with a non-null "willing" value in the state of sufficient age) and returns an iterator of the "best" replicas for failover
            required=True,
            type='single'),

        ######### Dealing with the Distributed Configuration system
        # set the database identifier, return True if it can be set, false if not.
        dict(name='dcs_set_database_identifier',
            required=True,
            type='multiple'),
        dict(name='dcs_get_database_identifier',
            required=True,
            type='single'),

        dict(name='dcs_set_timeline',
            required=True,
            type='multiple'),
        dict(name='dcs_get_timeline',
            required=True,
            type='single'),

        dict(name='dcs_lock',
            required=True,
            type='single'),
        dict(name='dcs_unlock',
            required=True,
            type='multiple'),
        dict(name='dcs_get_lock_owner',
            required=True,
            type='single'),

        dict(name='dcs_watch',
            required=True,
            type='multiple'),

        dict(name='dcs_set_state',
            required=True,
            type='multiple'),
        dict(name='dcs_list_state',
            required=True,
            type='single'),

        dict(name='dcs_delete_conn_info',
            required=True,
            type='multiple'),
        dict(name='dcs_set_conn_info',
            required=True,
            type='multiple'),
        dict(name='dcs_list_conn_info',
            required=True,
            type='single'),

        dict(name='dcs_disconnect',
            required=True,
            type='multiple'),

        ######### Dealing with the local postgresql cluster
        dict(name='pg_connect_info', # return a dict with the connection info
            required=True,
            type='single'),
        dict(name='pg_get_database_identifier',
            required=True,
            type='single'),
        dict(name='pg_get_timeline',
            required=True,
            type='single'),
        # stop postgresql if it is not already stopped
        dict(name='pg_stop',
            required=True,
            type='multiple'),
        # start postgresql if it is not already running
        dict(name='pg_start',
            required=True,
            type='multiple'),
        dict(name='pg_reload',
            required=True,
            type='multiple'),
        # halt: should prevent the existing database from running again.
        # either stop the whole machine, move data directory aside, pg_rewind or prepare for re-bootstrapping as a slave
        dict(name='pg_reset',
            required=True,
            type='multiple'),
        # create a new postgresql database
        dict(name='pg_initdb',
            required=True,
            type='multiple'),
        dict(name='pg_stop_replication',
            required=True,
            type='multiple'),
        dict(name='pg_setup_replication',
            required=True,
            type='multiple'),

        # create a backup and put it where replicas can get it
        dict(name='pg_backup',
            required=True,
            type='multiple'),
        dict(name='pg_restore',
            required=True,
            type='multiple'),
        dict(name='pg_replication_role', # returns one of: None, 'master', 'replica'
            required=True,
            type='single'),

        # monitoring
        dict(name='start_monitoring',
            required=True,
            type='multiple'),

        # extra keys for "conn" information provided by plugins
        # at least the one plugin must provde this so that application servers can connect
        dict(name='get_conn_info',
            required=True,
            type='multiple'),
        ]

def willing_replicas(states):
    for id, state in states:
        if state.get('willing', None) is None:
            continue
        if state['willing'] + 600 < time.time():
            yield id, state

class App:

    _giveup_lock = asyncio.Lock()
    my_id = None
    config = None
    database_identifier = None
    tick_time = None
    _exit_code = 0

    def __init__(self, config):
        self.health_problems = {}
        self._state = {}
        self.config = config
        self.tick_time = config['deadman'].get('tick_time', 2) # float seconds to scale all timeouts
        self._conn_info = {} # TODO: populate from config file
        self._setup_plugins()
        self.logger = logging

    def _setup_plugins(self):
        self._plugins = zgres.plugin.get_plugins(
                self.config,
                'deadman',
                _PLUGIN_API,
                self)

    def follow(self, primary_conninfo): 
        # Change who we are replicating from
        logging.info('Now replicating from {}'.format(primary_conninfo))
        assert self._plugins.pg_replication_role() != 'master'
        self._plugins.pg_setup_replication(primary_conninfo=primary_conninfo)
        self._plugins.pg_reload()

    def replica_bootstrap(self):
        self._plugins.pg_stop()
        # some restore methods only restore data, not config files, so let's init first
        self._plugins.pg_initdb()
        try:
            self._plugins.pg_restore()
        except Exception:
            # try make sure we don't restore a master by mistake
            self._plugins.pg_reset()
            raise
        self._plugins.pg_setup_replication()
        my_database_id = self._plugins.pg_get_database_identifier()
        if self._plugins.pg_replication_role() != 'replica' or my_database_id != self.database_identifier:
            # destroy our current cluster
            self._plugins.pg_reset()
            logging.error("Something is seriously wrong: after restoring postgresql was NOT setup as a replica.")
            return 5
        return 0

    def master_bootstrap(self):
        # Bootstrap the master, make sure that the master can be
        # backed up and started before we set the database id
        self.logger.info('Initializing master DB')
        self._plugins.pg_initdb()
        self._plugins.pg_start()
        database_id = self._plugins.pg_get_database_identifier()
        self.logger.info('Initializing done, master database identifier: {}'.format(database_id))
        if self._plugins.dcs_lock('database_identifier'):
            self.logger.info('Got database identifer lock')
            if self._plugins.dcs_get_database_identifier() is not None:
                self.logger.info('Database identifier already set, restarting to become replica')
                return 0
            self.logger.info('No database identifer yet, performing first backup')
            self.database_identifier = database_id
            self._plugins.pg_backup()
            if not self._plugins.dcs_set_database_identifier(database_id):
                raise AssertionError('Something is VERY badly wrong.... this should never happen....')
            self.logger.info('Successfully bootstrapped master and set database identifier: {}'.format(database_id))
            return 0
        self.logger.info('Could not set database identifier in DCS. maybe another master beat us? trying again')
        return 5

    def initialize(self):
        """Initialize the application

        returns None if initialzation was successful
        or a number of seconds to wait before trying again to initialize
        """
        self.unhealthy('zgres.initialize', 'Initializing')
        self.logger.info('Initializing plugins')
        self._plugins.initialize()
        self.my_id = self._plugins.get_my_id()
        self.logger.info('My ID is: {}'.format(self.my_id))
        self.database_identifier = self._plugins.dcs_get_database_identifier()
        if self.database_identifier is None:
            self.logger.info('Could not find database identifier in DCS, bootstrapping master')
            return self.master_bootstrap()
        self.logger.info('Found database identifier in DCS: {}'.format(self.database_identifier))
        my_database_id = self._plugins.pg_get_database_identifier()
        if my_database_id != self.database_identifier:
            self.logger.info('My database identifer is different ({}), bootstrapping as replica'.format(my_database_id))
            return self.replica_bootstrap()
        replication_role = self._plugins.pg_replication_role()
        self.update_state(replication_role=replication_role)
        if replication_role is None:
            raise AssertionError('I should have a replication role already')
        elif replication_role == 'replica':
            self.logger.info('I am a replica, registering myself as such')
        elif replication_role == 'master':
            self.logger.info('I am NOT a replica, trying to take over as master')
            if self._plugins.dcs_lock('master'):
                self.logger.info('Got master lock, proceedint with startup')
            else:
                owner = self._plugins.dcs_get_lock_owner('master')
                self.logger.info('Failed to get master lock ({} has it), checking if a new master is running yet'.format(owner))
                self._plugins.pg_stop()
                my_timeline = self._plugins.pg_get_timeline()
                existing_timeline = self._plugins.dcs_get_timeline()
                if existing_timeline > my_timeline:
                    self.logger.info("a master has started while we didn't have the lock, resetting ourselves")
                    # we can't start again for risk of split brain
                    self._plugins.pg_reset()
                else:
                    self.logger.info('I could not get the master lock, but the master has not started up yet. (new master not functioning?) will try again in a bit')
                return 5
        self.logger.info('Making sure postgresql is running')
        self._plugins.pg_start()
        self.logger.info('Starting monitors')
        self._plugins.start_monitoring()
        self.logger.info('Starting to watch the DCS for events')
        self._plugins.dcs_watch(
                master_lock=self.master_lock_changed,
                state=self._plugins.notify_state,
                conn_info=self._plugins.notify_conn_info)
        self._get_conn_info_from_plugins()
        self.healthy('zgres.initialize')
        if self.health_problems:
            if replication_role == 'master':
                # I am an unhealthy master with the lock,
                # This is a wierd situation becase another master should have taken over before
                # we restarted and got the lock. let's check in a little while if we become healthy,
                # else try failover again
                loop = asyncio.get_event_loop()
                loop.call_later(300 * self.tick_time, loop.create_task, self._handle_unhealthy_master())
        return None

    def _get_conn_info_from_plugins(self):
        sources = dict((k, None) for k in self._conn_info)
        for plugin_name, info in self._plugins.get_conn_info():
            for k, v in info.items():
                source = sources.get(k, _missing)
                if source is None:
                    self.logger.info('plugin ({}) overriding connection info for {} set in config file, set to: {}'.format(plugin_name, k, v))
                elif source is not _missing:
                    self.logger.info('plugin ({}) overriding connection info for {} set by another plugin ({}), set to: {}'.format(plugin_name, k, source, v))
                sources[k] = plugin_name
                self._conn_info[k] = v
        self._state.update(deepcopy(self._conn_info))

    def update_state(self, **kw):
        changed = False
        for k, v in kw.items():
            if k in ['willing']:
                self.logger.warn('Cannot set state for {}={}, key {} is automatically set'.format(k, v, k))
                continue
            if k in self._conn_info:
                self.logger.warn('Cannot set state for {}={}, key {} has already been set in the connection info'.format(k, v, k))
                continue
            v = deepcopy(v) # for reliable change detection on mutable args
            existing = self._state.get(k, _missing)
            if v != existing:
                changed = True
                self._state[k] = v
        changed = self._update_auto_state() or changed
        if changed and 'zgres.initialize' not in self.health_problems:
            # don't update state in the DCS till we are finished updating
            self._plugins.dcs_set_state(self._state)

    def _update_auto_state(self):
        """Update any keys in state which the deadman App itself calculates"""
        state = self._state
        willing = True
        changed = False
        if state.get('health_problems', True):
            willing = False
        if state.get('replication_role', None) != 'replica':
            willing = False
        if willing and self._plugins.veto_takeover is not None:
            for plugin_name, vetoed in self._plugins.veto_takeover(deepcopy(self._state)):
                if vetoed:
                    willing = False
        if willing and state.get('willing', None) is None:
            state['willing'] = time.time()
            changed = True
        elif not willing and state.get('willing', None) is not None:
            state['willing'] = None
            changed = True
        return changed

    def _update_timeline(self):
        my_timeline = self._plugins.pg_get_timeline()
        self._plugins.dcs_set_timeline(my_timeline)

    def master_lock_changed(self, owner):
        """Respond to a change in the master lock.
        
        At least one plugin must call this callback when the master lock
        changes.  This method should also be called at least once on startup
        with the current master.
        """
        self._master_lock_owner = owner
        if owner == self.my_id:
            # I have the master lock, if I am replicating, stop.
            if self._plugins.pg_replication_role() == 'replica':
                self._plugins.pg_stop_replication()
                new_role = self._plugins.pg_replication_role()
                if new_role != 'master':
                    raise Exception('I should have become a master already!')
                self._update_timeline()
                self.update_state(replication_role=new_role)
        else:
            if self._plugins.pg_replication_role() == 'master':
                # if I am master, but I am not replicating, shut down
                self.restart(10)
            if owner is None:
                # No-one has the master lock, try take over
                loop = asyncio.get_event_loop()
                loop.call_soon(loop.create_task, self._try_takeover())
        if self._plugins.master_lock_changed is not None:
            self._plugins.master_lock_changed(owner)

    def _willing_replicas(self):
        return willing_replicas(self._plugins.dcs_list_state())

    def _am_i_best_replica(self):
        # Check how I am doing compared to my brethern
        better = []
        willing_replicas = list(self._willing_replicas()) # list() for easer testing
        for id, state in self._plugins.best_replicas(willing_replicas):
            if id == self.my_id:
                return True
            better.append((id, state))
        self.logger.info('Abstaining from leader election as I am not among the best replicas: {}'.format(better))
        return False

    async def _async_sleep(self, delay):
        await asyncio.sleep(delay * self.tick_time)

    def _sleep(self, delay):
        # blocking sleep
        time.sleep(delay * self.tick_time)

    async def _try_takeover(self):
        while True:
            self.logger.info('Sleeping a little to allow state to be updated in the DCS before trying to take over')
            await self._async_sleep(3) # let replicas update their state
            # The master is still missing and we should decide if we must take over
            if self._master_lock_owner is not None:
                self.logger.info('There is a new master: {}, stop trying to take over'.format(self._master_lock_owner))
                break
            if self._am_i_best_replica():
                # re-check my local state
                self.update_state()
                if self._state['willing'] is None:
                    self.logger.info('Abstaining from leader election as recheck of local state rendered me unwilling')
                    continue
                # try get the master lock, if this suceeds, master_lock_change will be called again
                # and will bring us out of replication
                self.logger.info('I am one of the best, trying to get the master lock')
                self._plugins.dcs_lock('master')
            else:
                self.logger.info('I am not yet the best replica, giving the others a chance')

    def unhealthy(self, key, reason, can_be_replica=False):
        """Plugins call this if they want to declare the instance unhealthy.

        If an instance is unhealthy, but can continue to serve as a replica, set can_be_replica=True
        """
        self.health_problems[key] = dict(reason=reason, can_be_replica=can_be_replica)
        self.update_state(health_problems=self.health_problems)
        if 'zgres.initialize' in self.health_problems:
            return
        logging.warn('I am unhelthy: ({}) {}'.format(key, reason))
        if self._plugins.pg_replication_role() == 'replica':
            if not can_be_replica:
                self._plugins.dcs_delete_conn_info()
        else:
            self._plugins.dcs_delete_conn_info()
            loop = asyncio.get_event_loop()
            loop.call_soon(loop.create_task, self._handle_unhealthy_master())

    async def _handle_unhealthy_master(self):
        if self._giveup_lock.locked():
            return # already trying
        async with self._giveup_lock:
            while self.health_problems:
                for i in self._willing_replicas():
                    # there is at least one willing replica
                    # give it a chance to take over by giving up
                    # the lock
                    self.restart(120)
                await self._async_sleep(30)

    def healthy(self, key):
        """Plugins call this if they want to declare the instance unhealthy"""
        reason = self.health_problems.pop(key, _missing)
        if reason is _missing:
            return # no-op, we were already healthy
        self.update_state(health_problems=self.health_problems)
        logging.warn('Stopped being unhealthy for this reason: ({}) {}'.format(key, reason))
        if self.health_problems:
            logging.warn('I am still unhelthy for these reasons: {}'.format(self.health_problems))
        else:
            # YAY, we're healthy again
            if self._plugins.pg_replication_role() == 'master':
                locked = self._plugins.dcs_lock('master')
                if not locked:
                    # for some reason we cannot lock the master, restart and try again
                    self.restart(60) # give the
            self._set_conn_info()

    def _set_conn_info(self):
        self._plugins.dcs_set_conn_info(self._conn_info)

    def run(self):
        loop = asyncio.get_event_loop()
        logging.info('Starting')
        timeout = self.initialize()
        if timeout is not None:
            self.restart(timeout)
        # Finished initialziation without issue, startup event loop
        loop.set_exception_handler(self._handle_exception)
        loop.run_forever()
        return self._exit_code

    def _handle_exception(self, loop, context):
        loop.default_exception_handler(context)
        logging.error('Unexpected exception, exiting...')
        self._exit_code = 1
        loop.call_soon(self.restart, 10)

    def _stop(self):
        # for testing
        loop = asyncio.get_event_loop()
        loop.stop()

    def restart(self, timeout):
        if self._plugins.pg_replication_role() == 'master':
            # If we are master, we must stop postgresql to avoid a split brain
            self._plugins.pg_stop()
        self._plugins.dcs_disconnect()
        logging.info('sleeping for {} ticks, then restarting'.format(timeout))
        self._sleep(timeout) # yes, this blocks everything. that's the point of it!
        self._stop()

    def pg_connect_info(self):
        # expose pg_connect for other plugins to use
        return self._plugins.pg_connect_info()

#
# Command Line Scripts
#

def deadman_cli(argv=sys.argv):
    parser = argparse.ArgumentParser(description="""Monitors/controls the local postgresql installation.

This daemon will do these things:

    - Register the local postgresql instance with Zookeeper by creating a file
      named the IP address to connect on.
    - Try to become master by creating the file:
        master-{cluster_name}
      in zookeeper. If we suceed we create the file /tmp/zgres_become_master.
    - Shutdown postgres temporarily if we are master and the zookeeper connection is lost.
    - Shutdown postgres permanently if master-{cluster_name} already exists and we didn't create it
        (split-brain avoidance)
    - Monitor the local postgresql installation, if it becomes unavailable,
      withdraw our zookeeper registrations.

It does not:
    - maintain streaming replication (use zgres-apply hooks for that)
    - do remastering (assumed to have happened before we start)
""")
    config = zgres.config.parse_args(parser, argv, config_file='deadman.ini')
    app = App(config)
    sys.exit(app.run())
