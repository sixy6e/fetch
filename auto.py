"""
Auto-download of ancillary files.

It allows Operations to specify a serious of source locations (http/ftp/rss URLs)
and destination locations to download to.

This is intended to replace Operations maintenance of many diverse and
complicated scripts with a single, central configuration file.
"""
import fcntl
import logging
import os
import stat
import sys
import heapq
import time
import multiprocessing
import signal
from croniter import croniter
from setproctitle import setproctitle

from . import DataSource, FetchReporter
from onreceipt.fetch.load import load_config


_log = logging.getLogger(__name__)


class _PrintReporter(FetchReporter):
    """
    Print events to the log.
    """

    def file_complete(self, uri, name, path):
        """
        :type uri: str
        :type name: str
        :type path: str
        """
        _log.info('Completed %r: %r -> %r', name, uri, path)

    def file_error(self, uri, message):
        """
        :type uri: str
        :type message: str
        """
        _log.info('Error (%r): %r)', uri, message)


def _can_lock(lock_file):
    """
    Use the given file as a lock.

    Return true if successful.

    :type lock_file: str
    :rtype: bool
    """
    umask_original = os.umask(0)
    try:
        fp = os.open(lock_file, os.O_WRONLY | os.O_CREAT, stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    finally:
        os.umask(umask_original)

    try:
        fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        return False

    return True


def _redirect_output(log_file):
    """
    Redirect all output to the given file.

    :type log_file: str
    """
    output = open(log_file, 'w')
    sys.stdout = output
    sys.stderr = output
    logging.getLogger().removeHandler(_log_handler)
    handler = logging.StreamHandler(stream=output)
    handler.setFormatter(_log_formatter)
    logging.getLogger().addHandler(handler)


def _run_module(reporter, name, module, scheduled_time, log_directory, lock_directory):
    """
    Run the given module in a subprocess
    :type reporter: FetchReporter
    :type name: str
    :type module: DataSource
    :rtype: multiprocessing.Process
    """

    def _run_proc(reporter, readable_name, module, log_file, lock_file):
        """
        (from a new process), run the given module.
        :param reporter:
        :param readable_name:
        :param module:
        :return:
        """
        _redirect_output(log_file)

        if not _can_lock(lock_file):
            _log.debug('Lock is activated. Skipping run. %r', readable_name)
            sys.exit(0)

        setproctitle(readable_name)
        _init_signals()
        _log.debug('Triggering %s: %r', readable_name, module)
        module.trigger(reporter)

    _id = _sanitize_for_filename(name)
    lock_file = os.path.join(
        lock_directory,
        '{id}.lck'.format(id=_id)
    )
    scheduled_time_st = time.strftime('%H%M', time.localtime(scheduled_time))
    log_file = os.path.join(
        log_directory,
        '{time}-{id}.log'.format(
            id=_id,
            time=scheduled_time_st
        )
    )
    readable_name = 'fetch {} {}'.format(scheduled_time_st, name)

    _log.info('Spawning %r. Log %r, Lock %r', readable_name, log_file, lock_file)
    _log.debug('Module info %r', module)
    p = multiprocessing.Process(
        target=_run_proc,
        name=readable_name,
        args=(reporter, readable_name, module, log_file, lock_file)
    )
    p.start()
    return p


def _init_signals(trigger_exit=None, trigger_reload=None):
    """
    Set signal handlers
    :param trigger_reload: Handler for reload
    :type trigger_exit: Handler for exit
    """
    # For a SIGINT signal (Ctrl-C) or SIGTERM signal (`kill <pid>` command), we start a graceful shutdown.
    signal.signal(signal.SIGINT, trigger_exit if trigger_exit else signal.SIG_DFL)
    signal.signal(signal.SIGTERM, trigger_exit if trigger_exit else signal.SIG_DFL)

    # SIGHUP triggers a reload of config (following conventions of many daemons).
    signal.signal(signal.SIGHUP, trigger_reload if trigger_reload else signal.SIG_DFL)


def _on_child_finish(child):
    """
    Handle child process cleanup: Check for errors.

    :type child: multiprocessing.Process
    """
    exit_code = child.exitcode
    if exit_code is None:
        _log.warn('Child not finished %s %s', child.name, child.pid)
        return

    _log.debug('Child finished %r %r', child.name, child.pid)

    # TODO: Send mail, alert or something?
    if exit_code != 0:
        _log.error('Error return code %s from %r', exit_code, child.name)


def _filter_finished_children(running_children):
    """
    Filter and check the exit codes of finished children.

    :type running_children: set of multiprocessing.Process
    :rtype: set of multiprocessing.Process
    """
    still_running = set()

    for child in running_children:
        exit_code = child.exitcode
        if exit_code is None:
            still_running.add(child)
            continue

        _on_child_finish(child)

    return still_running


def _sanitize_for_filename(filename):
    """
    :type filename: str
    :rtype: str
    >>> _sanitize_for_filename('some one')
    'some-one'
    >>> _sanitize_for_filename('s@me one')
    's-me-one'
    """
    return "".join([x if x.isalnum() else "-" for x in filename])


def get_day_log_dir(log_directory, time_secs):
    """
    Get log directory for this day.
    :type log_directory: str
    :type time_secs: float
    :return:
    """
    # We use localtime because the cron scheduling uses localtime.
    t = time.localtime(time_secs)
    day_log_dir = os.path.join(
        log_directory,
        time.strftime('%Y', t),
        time.strftime('%m-%d', t)
    )
    if not os.path.exists(day_log_dir):
        os.makedirs(day_log_dir)

    return day_log_dir


def _on_shutdown(running_children):
    # Shut down -- Join all children.
    all_children = running_children.union(multiprocessing.active_children())
    _log.info('Shutting down. Joining %r children', len(all_children))
    for p in all_children:
        p.join()
        _on_child_finish(p)


class Schedule(object):
    """
    Track scheduled items.

    Keeps items ordered by date so the next item can be easily retrieved.
    """
    def __init__(self, items):
        """
        :type items: list of ScheduledItem
        """
        self.schedule = []
        now = time.time()
        for scheduled_item in items:
            self.add_item(scheduled_item, base_date=now)

    def peek_next(self):
        """
        See the next scheduled item without removing it.
        :rtype: (float, ScheduledItem)
        """
        return self.schedule[0]

    def pop_next(self):
        """
        Remove the next scheduled item.
        :rtype: (float, ScheduledItem)
        """
        return heapq.heappop(self.schedule)

    def add_item(self, item, base_date=None):
        """
        Add item to the schedule, with an optional base date.
        :type base_date: float
        :type item: ScheduledItem
        :return:
        """
        if base_date is None:
            base_date = time.time()

        next_trigger = croniter(item.cron_pattern, start_time=base_date).get_next()
        heapq.heappush(self.schedule, (next_trigger, item))
        return next_trigger


class RunConfig(object):
    """
    Runtime configuration.
    """
    def __init__(self):
        self.are_exiting = False
        #: :type: Schedule
        self.schedule = None

        self.base_directory = None
        self.log_directory = None
        self.lock_directory = None

    def reload(self):
        """
        Reload configuration
        """
        config = load_config()
        self.schedule = Schedule(config.rules)
        self.base_directory = config.directory

        # Cannot change lock directory 'live'
        if not self.lock_directory:
            self.lock_directory = os.path.join(self.base_directory, 'lock')
            if not os.path.exists(self.lock_directory):
                os.makedirs(self.lock_directory)

        self.log_directory = os.path.join(self.base_directory, 'log')
        if not os.path.exists(self.log_directory):
            os.makedirs(self.log_directory)


def run_loop():
    """
    Main loop
    """

    o = RunConfig()

    def _reload_config():
        """Reload configuration."""
        _log.info('Reloading configuration')
        o.reload()
        _log.debug('%s rules loaded', len(o.schedule.schedule))

    def trigger_exit(signal_, frame_):
        """Start a graceful shutdown"""
        o.are_exiting = True

    def trigger_reload(signal_, frame_):
        """Handle signal to reload config"""
        _reload_config()

    _reload_config()

    if not os.path.exists(o.base_directory):
        raise ValueError('Configured base folder does not exist: %r' % o.base_directory)

    _init_signals(trigger_exit=trigger_exit, trigger_reload=trigger_reload)

    reporter = _PrintReporter()
    # Keep track of running children to view their exit codes later.
    running_children = set()

    while not o.are_exiting:
        running_children = _filter_finished_children(running_children)

        # active_children() also cleans up zombie subprocesses.
        child_count = len(multiprocessing.active_children())

        _log.debug('%r recorded children, %r total children', len(running_children), child_count)

        if not o.schedule:
            _log.info('No scheduled items. Sleeping.')
            time.sleep(500)
            continue

        now = time.time()
        # Pick the first from the sorted list (ie. the closest to now)
        scheduled_time, scheduled_item = o.schedule.peek_next()

        if scheduled_time < now:
            # Trigger time has passed, so let's run it.

            scheduled_time, scheduled_item = o.schedule.pop_next()

            p = _run_module(
                reporter,
                scheduled_item.name,
                scheduled_item.module,
                scheduled_time=scheduled_time,
                # Use a unique log directory for each day
                log_directory=get_day_log_dir(o.log_directory, scheduled_time),
                lock_directory=o.lock_directory
            )
            running_children.add(p)

            # Schedule next run for this module
            next_trigger = o.schedule.add_item(scheduled_item, base_date=now)

            _log.debug('Next trigger in %.1f minutes', next_trigger - now)
        else:
            # Sleep until next action is ready.
            sleep_seconds = (scheduled_time - now) + 0.1
            _log.debug('Sleeping for %.1f minutes until action %r', sleep_seconds / 60.0, scheduled_item.name)
            time.sleep(sleep_seconds)

    _on_shutdown(running_children)


_log_handler = logging.StreamHandler(stream=sys.stderr)
_log_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
_log_handler.setFormatter(_log_formatter)

if __name__ == '__main__':
    logging.getLogger().addHandler(_log_handler)
    logging.getLogger().setLevel(logging.WARNING)

    logging.getLogger('onreceipt').setLevel(logging.INFO)
    logging.getLogger('onreceipt.fetch').setLevel(logging.INFO)
    _log.setLevel(logging.DEBUG)

    run_loop()
