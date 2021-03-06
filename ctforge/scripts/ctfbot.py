#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# CTForge: Forge your own CTF.

# Copyright (C) 2016-2019  Marco Squarcina
# Copyright (C) 2016-2019  Mauro Tempesta
# Copyright (C) 2016-2019  Lorenzo Veronese

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


"""
The ctf swiss army knife: round initializer, flag dispatcher, service checker

This program can be used in three different modes, depending on the current
task we want to execute.

In advance mode, the script calls a stored procedure to prepare the database
for a new round and inserts fresh flags for all the service, team pairs. In
dispatch mode, all the flags are sent to the teams' vms by calling an external
script. In check mode, each service is tested by executing a script that
performs a safe access and retrieves the flag. The return code of the script
represents the status of the service, which is then saved into the database for
being used later during the score computation phase.

"""

import os
import re
import sys
import time
import copy
import signal
import random
import argparse
import logging
import math
import threading
import subprocess
import os.path
from queue import Queue, Empty
from collections import defaultdict
import psycopg2

from ctforge import database, utils, config


__authors__     = ['Marco Squarcina <squarcina at unive.it>',
                   'Mauro Tempesta <tempesta at unive.it>']
__license__     =  'MIT'
__copyright__   =  'Copyright 2013-17, University of Venice'

# global variables

# custom logger
logger = logging.getLogger('bot')
# queue of tasks: each task is a pair (i_team, i_service)
tasks = Queue()
# just one connection to the database for all the threads
db_conn = None
# regex used to extract flag ids from dispatcher output
flag_id_regex = re.compile(b'<flagid>(.*?)</flagid>', re.DOTALL | re.MULTILINE)


def abort():
    """Terminate the program after if it is not possible to proceed."""

    if db_conn is not None:
        db_conn.close()
    sys.exit(1)

def interrupt(signal=None, frame=None):
    """Instruct the threads to terminate and quit."""

    logger.error('Ctrl-C received, stopping the execution')
    # empty the tasks queue
    try:
        while True:
            tasks.get_nowait()
    except Empty:
        pass
    # set the killing event
    Worker.killing_time.set()

class Team:
    """Simple class for representing a team entity, storing the id ip
    and team name (name)."""

    def __init__(self, id, ip, name):
        # team id
        self.id = id
        # ip of the vulnerable machine of the team
        self.ip = ip
        # gang name for the team
        self.name = name

    def __repr__(self):
        return 'Team: {}, {}, {}'.format(self.id, self.ip, self.name)

class Service:
    """Simple class for representing a service entity."""

    def __init__(self, id, name, active, flag_lifespan, flag_id=False):
        # service id
        self.id = id
        # name of the service
        self.name = name
        # whether the service is active or not
        self.active = active
        # how long a flag is valid (number of rounds)
        self.flag_lifespan = flag_lifespan
        # whether the service supports flag ids
        self.flag_id = flag_id

    def __repr__(self):
        return 'Service: {}, {}, {}'.format(self.id, self.name, self.active)

class Worker(threading.Thread):
    """The main execution unit while in dispatch/check mode."""

    killing_time = threading.Event()

    def __init__(self, n, dispatch, check, timeout=10):
        super(Worker, self).__init__()
        # numeric identifier of the worker
        self.n = n
        # team instance being processed
        self.team = None
        # service instance processed
        self.service = None
        # active flags for the pair (team, service), sorted by round id
        # in decreasing order (i.e. the first is the most recent)
        self.flags = []
        self.flag_ids = {}
        # worker modalities
        self.dispatch = dispatch
        self.check = check
        # seconds to wait before killing a spawned script
        self.timeout = timeout
        # make it a daemon thread
        self.daemon = True
        # current round
        self.rnd = self._get_curr_round()

    def run(self):
        """Extract and execute jobs from the tasks queue until there is
        nothing left to do."""

        # fetch tasks from the queue
        while not Worker.killing_time.is_set():
            try:
                self.team, self.service = tasks.get_nowait()
                # check whether we need to dispatch the flag or check the service
                # status and proceed according to the given mode. The program logic
                # is not f*cked if we execute both actions. Theoretically, one
                # could advance the round, dispatch new flags and check services
                # with a single execution of this script
                if self.dispatch:
                    # get the flags for the current round
                    self._get_flags()
                    self._dispatch_flag()
                # get the updated flags with flag ids
                self._get_flags()
                if self.check:
                    self._check_service()
            except Empty:
                # terminate if the queue is empty
                break

    def _get_flags(self):
        """Retrieve the active flags for the current pair (team, service)."""

        try:
            with db_conn.cursor() as cur:
                cur.execute((
                    'SELECT flag, flag_id FROM flags '
                    'WHERE team_id = %s AND service_id = %s AND '
                    'get_current_round() - round <= %s - 1 '
                    'ORDER BY round DESC')
                    , [self.team.id, self.service.id, self.service.flag_lifespan])
                res = cur.fetchall()
        except psycopg2.Error as e:
            logger.critical(self._logalize('Error while retrieving the active flags, aborting: {}'.format(e)))
            abort()

        if not res:
            # no active flag, this should never happen
            logger.critical(self._logalize('No flags for the current team/service, aborting'))
            abort()

        self.flags = [r['flag'] for r in res]
        self.flag_ids = { r['flag']: r['flag_id'] for r in res }

    def _dispatch_flag(self):
        """Send the flag to a given team for a given service by executing an
        external script. The script is killed if it takes too long to
        complete."""

        # execute the script, getting the return status and the stdout
        flag =  self.flags[0]
        status, stdout = self._execute(
            os.path.join(config['BOT_SCRIPT_PATH'],
                         self.service.name, 'dispatch'),
            flag, keep_stdout=True)
        if status != 0:
            logger.critical(self._logalize(
                'Dispatcher Error for {}: non-zero exit code!'.format(flag)))
            return
        # if the service supports flag ids
        if self.service.flag_id:
            if not stdout:
                logger.critical(self._logalize(
                    'Error while generating flag id for {}: stdout is empty!'.format(self.flags[0])))
                return
            # stdout is the flag_id
            flag_ids = flag_id_regex.findall(stdout)
            flag_id = flag_ids[0].decode().strip()
            if len(flag_ids) == 0 or len(flag_ids) > 1:
                logger.critical(self._logalize(
                    'Error while generating flag id for {}: multiple or no flag ids found!'.format(self.flags[0])))
                return
            try:
                with db_conn.cursor() as cur:
                    cur.execute((
                        'UPDATE flags SET flag_id = %s '
                        'WHERE flag = %s AND team_id = %s AND service_id = %s'
                        '  AND round = get_current_round()')
                                , [flag_id, self.flags[0], self.team.id, self.service.id])
                    db_conn.commit()
            except psycopg2.Error as e:
                # an error occurred, no recovery possible
                logger.critical(self._logalize('Unable to insert the flag id: {}'.format(e)))
            else:
                # update successful
                logger.debug(self._logalize('Flag ID added {} -> {}'.format(self.flags[0], repr(flag_id))))

    def _check_service(self):
        """Check if a given service on a given host is working as expected by
        executing an external script. The return value of each script is
        recorded and processed according to the following rule:

        *    0:     service is working fine
        *   -1:     oserror while executing the script, since it's probably our
                    fault we do not add any record in the integrity_checks table
        * >125:     the shell spawned by Popen is unable to execute the script,
                    it's probably our fault as above
        * else:     the service is corrupted

        The script is killed if it takes too long to complete and the
        service marked as corrupted."""

        # execute the script and assume the service status by the return address: run the checker using
        # one of the valid flags (selected randomly)
        flag =  random.choice(self.flags)
        flag_id = self.flag_ids[flag] if self.service.flag_id else None
        status = self._execute(os.path.join(config['BOT_SCRIPT_PATH'],
                                            self.service.name, 'check'),
                               flag, flag_id=flag_id)
        if status == -1 or status > 125:
            # our fault: we don't add anything in the integrity_checks table
            return
        success = status == 0
        try:
            with db_conn.cursor() as cur:
                cur.execute((
                    'INSERT INTO integrity_checks (round, team_id, service_id, successful) '
                    'VALUES (get_current_round(), %s, %s, %s)')
                    , [self.team.id, self.service.id, success])
            db_conn.commit()
        except psycopg2.Error as e:
            # an error occurred, no recovery possible
            logger.critical(self._logalize('Unable to insert the integrity check report: {}'.format(e)))
        else:
            # update successful
            logger.debug(self._logalize('Status added as {}'.format(status)))

    def _execute(self, script_name, flag, flag_id=None, keep_stdout=False):
        """Execute the provided script killing the process if it timeouts."""

        # set status of the service to corrupted by default
        status = 1
        # stdout is empty by default
        stdout = ''
        command_lst = [script_name, self.team.ip, flag, str(self.rnd)]
        if flag_id:
            command_lst.append(flag_id)
        command = ' '.join(command_lst)
        try:
            logger.debug(self._logalize('Executing {}'.format(command)))
            # Do not inherit python virtualenv
            env = copy.deepcopy(os.environ)
            try:
                del env['VIRTUAL_ENV']
            except KeyError:
                pass

            # ignore stdout and stderr if not(keep_stdout)
            process = subprocess.Popen(command_lst, preexec_fn=os.setsid,
                                       stdout=subprocess.PIPE if keep_stdout else subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL,
                                       env=env)
            stdout, _ = process.communicate(timeout=self.timeout)
            status = process.returncode
        except subprocess.TimeoutExpired:
            # the remote VM could be down, this is not a critical error but we
            # should anyway give it a look
            logger.warning(self._logalize('Timeout exceeded while executing {}'.format(command)))
            # politely kill the process tree and wait a small amount of time for
            # the process to clear resources
            process.terminate()
            time_tmp = time.time()
            while process.poll() is None and time.time() < (time_tmp + 3):
                time.sleep(0.1)
            if process.poll() is None:
                process.kill()
        except FileNotFoundError as e:
            logger.error(self._logalize('Script not found: {}'.format(e)))
        except ProcessLookupError:
            # we tried to kill an already terminated program or the script is not found
            pass
        except Exception as e:
            # wtf happened? this is an unknown error. Assume it's our fault
            status = -1
            logger.critical(self._logalize('Error while executing {}: {}'.format(command, e)))

        if keep_stdout:
            return (status, stdout)

        return status

    def _get_curr_round(self):
        """Return the current round by quering the database."""

        with db_conn.cursor() as cur:
            # advance the round and clear the flag tables
            try:
                cur.execute('SELECT get_current_round() AS round')
                rnd = cur.fetchone()['round']
            except Exception as e:
                # wtf happened? this is an unknown error. Assume it's our fault
                logger.critical(self._logalize('Cannot retrieve current round: {}'.format(e)))

        return rnd

    def _logalize(self, message):
        """Return a pretty string ready to be logged."""

        return 'Worker-{} ({}, {}): {}'.format(self.n, self.team.ip,
                                               self.service.name, message)

def get_teams_services():
    """Retrieve the lists of team and service instances."""

    with db_conn.cursor() as cur:
        try:
            cur.execute('SELECT id, ip, name FROM teams')
            teams = [Team(**t) for t in cur.fetchall()]
            cur.execute('SELECT id, name, active, flag_lifespan, flag_id FROM services')
            services = [Service(**s) for s in cur.fetchall() if s['active']]
        except psycopg2.Error as e:
            logger.critical('Error while querying the database, aborting: {}'.format(e))
            abort()

    return teams, services

def advance_round(teams, services):
    """Advance the round: update results, truncate the active_flags table and
    store new flags in the database for each team and service."""

    with db_conn.cursor() as cur:
        try:
            # get the current round id
            cur.execute('SELECT get_current_round() AS round')
            rnd = cur.fetchone()['round']

            for service in services:
                # dictionary mapping each team to the set of captured flags
                captured_flags = defaultdict(set)
                # dictionary mapping each team to the set of lost flags
                lost_flags = defaultdict(set)
                # dictionary counting how many times a flag has been stolen
                count_flag_captures = defaultdict(lambda: 0)

                cur.execute((
                            'SELECT F.flag AS flag, S.team_id AS attacker, F.team_id AS defender '
                            'FROM flags AS F JOIN service_attacks S ON F.flag = S.flag '
                            'WHERE F.service_id = %s')
                            , [service.id])
                for row in cur:
                    flag = row['flag']
                    captured_flags[row['attacker']].add(flag)
                    lost_flags[row['defender']].add(flag)
                    count_flag_captures[flag] += 1

                cur.execute((
                            'SELECT team_id, COUNT(*) AS successful_checks '
                            'FROM integrity_checks '
                            'WHERE successful AND service_id = %s '
                            'GROUP BY team_id')
                            , [service.id])
                checks = {c['team_id']: c['successful_checks'] for c in cur}

                # compute the score of each team for the current service according to the
                # FaustCTF rules and add new entries to the `scores` table
                for team in teams:
                    attack = len(captured_flags[team.id])
                    for flag in captured_flags[team.id]:
                        attack += 1 / count_flag_captures[flag]
                    defense = -sum(math.sqrt(count_flag_captures[flag]) for flag in lost_flags[team.id])
                    sla = checks.get(team.id, 0) * math.sqrt(len(teams))

                    # if round is 0 and there is already a score of 0.
                    # we need to update those rows
                    if rnd == 0:
                        cur.execute((
                            'INSERT INTO scores '
                            'VALUES (%s, %s, %s, %s, %s, %s) '
                            'ON CONFLICT (round, team_id, service_id) '
                            'DO UPDATE SET attack = %s, defense = %s, sla = %s '
                            'WHERE scores.round = %s AND scores.team_id = %s AND scores.service_id = %s')
                            , [rnd, team.id, service.id, attack, defense, sla,
                               attack, defense, sla,
                               rnd, team.id, service.id])
                    else:
                        cur.execute((
                            'INSERT INTO scores '
                            'VALUES (%s, %s, %s, %s, %s, %s)')
                            , [rnd, team.id, service.id, attack, defense, sla])

            # create a new round entry
            cur.execute('INSERT INTO rounds (id) VALUES (%s)', [rnd+1])

        except psycopg2.Error as e:
            logger.critical('Error while incrementing the round, aborting: {}'.format(e))
            abort()
    db_conn.commit()

    rnd += 1
    logger.info('Round {} started'.format(rnd))
    # generate and insert the new flags to the database
    cur = db_conn.cursor()
    for service in services:
        for team in teams:
            inserted = False
            while not inserted:
                flag = utils.generate_flag(config['FLAG_PREFIX'], config['FLAG_SUFFIX'],
                                           config['FLAG_CHARS'], config['FLAG_LENGTH'])
                try:
                    cur.execute((
                        'INSERT INTO flags (flag, team_id, service_id, round) '
                        'VALUES (%s, %s, %s, %s)'),
                        (flag, team.id, service.id, rnd))
                except psycopg2.IntegrityError:
                    logger.warning('Duplicate flag, generating a new one')
                except psycopg2.Error as e:
                    logger.critical(('Error while adding a new flag to the '
                                      'database, aborting: {}').format(e))
                    abort()
                else:
                    inserted = True
                    logger.debug('New flag just added to the database: {}'.format(flag))
    db_conn.commit()
    cur.close()

def main():
    global logger, db_conn

    # parse command line options, the round parameter is required
    parser = argparse.ArgumentParser(description='Flag dispatcher and checker')
    parser.add_argument('-c', '--conf', dest='conf', type=str,
        default='ctforge.conf', help='Configuration file')
    parser.add_argument('--advance', action='store_true', default=False,
        help='Advance the current round')
    parser.add_argument('--dispatch', action='store_true', default=False,
        help='Dispatch new flags to all the virtual machines')
    parser.add_argument('--check', action='store_true', default=False,
        help='Check the integrity of the services')
    parser.add_argument('-n',  dest='num_workers', type=int, default=1,
        help='Number of concurrent workers (default 1)')
    parser.add_argument('-t', dest='timeout', type=int, default=10,
        help='Seconds to wait before killing a spawned script (default 10)')
    parser.add_argument('-v', dest='verbose', action='store_true',
        default=False, help='Set logging level to debug')
    args = parser.parse_args()
    if not any([args.advance, args.dispatch, args.check]):
        sys.stderr.write('At least one action is required, aborting.\n')
        abort()

    # Read the configuration if specified
    if args.conf is not None:
        print('[*] Reading configuration from {}'.format(args.conf))
        sys.stdout.flush()

        try:
            config = utils.parse_conf(args.conf)
        except Exception:
            sys.stderr.write('Invalid configuration file, aborting!')
            sys.exit(1)

    # register the killer handler
    signal.signal(signal.SIGINT, interrupt)

    # set variables
    n_workers = args.num_workers
    log_level = logging.DEBUG if args.verbose else logging.INFO

    # set logging
    logger.setLevel(log_level)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    fh = logging.FileHandler(config['BOT_LOG_FILE'])
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    # if the verbose mode is selected, log also on the console
    if args.verbose:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    # start the global db connection
    db_conn = database.db_connect(logger=logger)

    # retrieve the list of teams and services
    teams, services = get_teams_services()
    if args.advance:
        # advance the round
        advance_round(teams, services)
    if args.check or args.dispatch:
        # fill the queue of tasks, choosing the team order randomly :)
        for service in services:
            for team in random.sample(teams, len(teams)):
                tasks.put_nowait((team, service))

        # create the list of workers
        workers = []
        for i in range(n_workers):
            worker = Worker(i, args.dispatch, args.check, args.timeout)
            workers.append(worker)
            worker.start()

        # wait responsively until the queue is empty or an event is thrown
        while not tasks.empty():
            Worker.killing_time.wait(1)

        # join all workers
        for worker in workers:
            worker.join()

    # close the connection to the db
    db_conn.close()

    # exit gracefully
    sys.exit(0)


if __name__ == '__main__':
    main()
