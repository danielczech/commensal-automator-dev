#!/usr/bin/env python
# Helpers functions for looking up various redis data.
# The convention is that "r" is our redis client.

from datetime import datetime, timezone
import os
import re
import redis
import sys
import time

from automator.logger import log

SLACK_CHANNEL = "meerkat-obs-log"
SLACK_PROXY_CHANNEL = "slack-messages"
PROPOSAL_ID = 'EXT-20220504-DM-01' 

def raw_files(r):
    """Returns a dict mapping host name to a list of raw files on the host."""
    hosts = [key.split(":")[-1] for key in r.keys("bluse_raw_watch:*")]
    pipe = r.pipeline()
    for host in hosts:
        pipe.smembers("bluse_raw_watch:" + host)
    results = pipe.execute()
    answer = {}
    for host, result in zip(hosts, results):
        answer[host] = sorted(result)
    return answer


def sb_id(r, subarray):
    """Returns the current schedule block id, in hyphenated form.
    For example, it could be:
    20221128-0003
    Raises a ValueError if there is none.
    May return "Unknown_SB" if the upstream code indicates the schedule block is unknown.
    """
    sb_id_list = r.get(f"{subarray}:sched_observation_schedule_1")
    if not sb_id_list:
        raise ValueError("no schedule block ids found")
    answer = sb_id_list.split(",")[0]
    if answer == "Unknown_SB":
        return answer
    if not re.match(r"[0-9]{8}-[0-9]{4}", answer):
        raise ValueError(f"bad sb_id_list value: {sb_id_list}")
    return answer


def coordinator_subarrays(r):
    """Returns a list of all subarrays that have hosts allocated to the coordinator."""
    return sorted(key.split(":")[-1] for key in r.keys("coordinator:allocated_hosts:*"))


def allocated_hosts(r, subarray):
    """Returns the hosts allocated to a particular subarray."""
    results = r.lrange("coordinator:allocated_hosts:" + subarray, 0, -1)
    return sorted(s.split("/")[0] for s in results)


def get_nshot(r, subarray_name):
    """Get the current value of nshot from redis.
    """
    nshot_key = 'coordinator:trigger_mode:{}'.format(subarray_name)
    nshot = int(r.get(nshot_key).split(':')[1])
    return nshot

def set_nshot(r, subarray_name, val):
    """Set nshot for given subarray.
    """
    nshot_key = 'coordinator:trigger_mode:{}'.format(subarray_name)
    r.set(nshot_key, f"nshot:{val}")

def is_rec_enabled(r, subarray_name):
    """Is recording enabled?
    """
    rec_enabled_key = 'rec_enabled:{}'.format(subarray_name)
    rec_enabled = r.get(rec_enabled_key)
    if rec_enabled is None:
        log.warning('Could not determine recording permission. Defaulting to disabled.')
        rec_enabled = 0
    return int(rec_enabled)

def enable_recording(r, subarray_name):
    """Allow recording to proceed with new tracks for 
    the specified array.
    """
    rec_setting = is_rec_enabled(r, subarray_name)
    log.info('rec_setting {}, setting to 1'.format(rec_setting))
    rec_setting_key = 'rec_enabled:{}'.format(subarray_name)
    r.set(rec_setting_key, 1) 

def disable_recording(r, subarray_name):
    """Prevent recording from proceeding for new tracks for 
    the specified array.
    """
    rec_setting = is_rec_enabled(r, subarray_name)
    log.info('rec_setting {}, setting to 0'.format(rec_setting))
    rec_setting_key = 'rec_enabled:{}'.format(subarray_name)
    r.set(rec_setting_key, 0) 

def get_bluse_dwell(r, subarray_name):
    """Get specified dwell for BLUSE primary time observing.
    Dwell in seconds.
    """
    bluse_dwell_key = 'bluse_dwell:{}'.format(subarray_name)
    bluse_dwell = r.get(bluse_dwell_key)
    if bluse_dwell is None:
       log.warning('No specified primary time DWELL; defaulting to 290 sec.')
       bluse_dwell = 290   
    return int(bluse_dwell)

def reset_dwell(r, instances, dwell):
    """Reset DWELL for given list of instances.
    """
    chan_list = channel_list('bluse', instances)
    # Send messages to these specific hosts:
    for i in range(len(chan_list)):
        log.info(f"Resetting DWELL for {chan_list[i]}, new dwell: {dwell}")
        r.publish(chan_list[i], "DWELL=0")
        r.publish(chan_list[i], "PKTSTART=0")
        time.sleep(1.5) # Wait for processing node. NOTE: Is this long enough?
        r.publish(chan_list[i], f"DWELL={dwell}")

def channel_list(hpgdomain, instances):
    """Build a list of Hashpipe-Redis Gateway channels from a list 
       of instance names (of format: host/instance)
    """
    channel_list = [hpgdomain + '://' + instance + '/set' for instance in instances]
    return channel_list

def is_primary_time(r, subarray_name):
    """Check if the current observation ID is for BLUSE primary
    time. 
    """
    subarray = 'subarray_{}'.format(subarray_name[-1])
    p_id_key = '{}_script_proposal_id'.format(subarray)
    p_id = r.get('{}:{}'.format(subarray_name, p_id_key))
    if p_id == PROPOSAL_ID:
        log.info("BLUSE proposal ID detected for {}".format(subarray_name))
        return True
    return False

def get_last_rec_bluse(r, subarray_name):
    """Return True if last recording was made under the BLUSE
    proposal ID (thus primary time). 
    """
    key = f"{subarray_name}:primary_time"
    val = r.get(key)
    try:
        val = int(val)
    except:
        log.error(f"Could not determine primary time status: {val}")
    return val 

def set_last_rec_bluse(r, subarray_name, value):
    """Set the value (bool) of the last recording proposal
    ID flag (True if BLUSE ID, False if not).
    """ 
    key = f"{subarray_name}:primary_time"
    log.info(f"setting primary time status for {subarray_name}: {value}")
    r.set(key, value)

def primary_time_in_progress(r):
    """Returns True if still in the midst of primary observing
    sequence.
    """
    # Note: as of now, this will only ever happen with a single subarray.
    # Note: we need to check proposal id at the time of the most recent 
    # recording. 
    if is_primary_time(r, 'array_1'):
        nshot = get_nshot(r, 'array_1')
        if nshot > 0:
            log.info('Not processing: nshot is {}'.format(nshot))
            return True
    return False

def all_hosts(r):
    return sorted(key.split("//")[-1].split("/")[0]
                  for key in r.keys("bluse://*/0/status"))


def multicast_subscribed(r):
    """Returns a list of the hosts that are subscribed to F-engine multicast groups.
    """
    return [host for (host, destip) in  get_status(r, "bluse", "DESTIP")
            if destip != "0.0.0.0"]


def multiget_status(r, domain, keys):
    """Fetches status from hashpipe processes.
    domain is "bluse" or "blproc".

    Returns a list of (host, value-list) tuples.
    """
    hosts = all_hosts(r)
    pipe = r.pipeline()
    for host in hosts:
        redis_key = "{}://{}/0/status".format(domain, host)
        pipe.hmget(redis_key, keys)
    results = pipe.execute()
    return list(zip(hosts, results))


def get_status(r, domain, key):
    """Like multiget_status but just one key.

    Returns a list of (host, value) tuples.
    """
    return [(host, results[0]) for (host, results) in multiget_status(r, domain, [key])]


def broken_daqs(r):
    """Return a sorted list of which daqs look broken."""
    answer = []
    for host, result in get_status(r, "bluse", "DAQPULSE"):
        if result is None:
            continue
        delta = datetime.strptime(result, "%c") - datetime.now()
        if abs(delta.total_seconds()) > 60:
            answer.append(host)
    return answer


def ready_to_record(r):
    """Returns a sorted list of all hosts for which the coordinator is ready to record.
    This means they will start recording on the next tracking event.
    """
    answer = set()
    subarrays = coordinator_subarrays(r)
    for subarray in subarrays:
        nshot = get_nshot(r, subarray)
        if nshot > 0:
            answer = answer.union(allocated_hosts(r, subarray))
    return sorted(answer)


def get_recording(r):
    """Returns a sorted list of all hosts that are currently recording.
    """
    hkeys = ["PKTIDX", "PKTSTART", "PKTSTOP"]
    for _ in range(10):
        try:
            answer = set()
            for host, strkeys in multiget_status(r, "bluse", hkeys):
                if strkeys[1] == "0":
                    # PKTSTART=0 indicates not-in-use even if the other keys are absent
                    continue
                for k, val in zip(hkeys, strkeys):
                    if val is None:
                        log.warning("on host {} the key {} is not set".format(host, k))
                        raise IOError("synthetic error to invoke retry logic")
                if None in strkeys:
                    # This is a race condition and we don't know what it means.
                    # Let's treat it as "in use"
                    answer.add(host)
                    continue
                pktidx, pktstart, pktstop = map(int, strkeys)
                if pktstart > 0 and pktidx < pktstop:
                    answer.add(host)
            return sorted(answer)
        except IOError:
            time.sleep(1)
            continue
    raise IOError("get_recording failed even after retry")


def coordinator_hosts(r):
    """Returns a list of all hosts the coordinator is using.

    These are the hosts that the coordinator may write more files to, even if it
    receives no further instructions from us.
    We don't want to start processing on a host while the coordinator is still using it.

    TODO: carefully avoid all race conditions here
    """
    return sorted(set(ready_to_record(r) + get_recording(r)))


def sb_id_from_filename(filename):
    """Works on either raw file names or directory names.
    Returns None if the filename doesn't fit the pattern.
    """
    parts = filename.strip("/").split("/")
    if len(parts) < 3:
        return None
    x, y = parts[1:3]
    if len(x) != 8 or not x.isnumeric() or not y.isnumeric():
        return None
    return "{}/{}".format(x, y)


def timestamped_dir_from_filename(filename):
    """Extracts timestamped section from filenames like:
    /buf0/<timestamp-part>/blah/blah/etc
    """
    parts = filename.strip("/").split("/")
    if len(parts) < 2:
        return None
    answer = parts[1]
    if not re.match(r"^[0-9]{8}T[0-9]{6}Z-[^/]*$", answer):
        return None
    return answer


def infer_subarray(r, hosts):
    """Guess what subarray the data on these hosts is from.
    If there is no exact match, return None.
    """
    hosts_set = set(hosts)
    for subarray in coordinator_subarrays(r):
        array_hosts = allocated_hosts(r, subarray)
        if hosts_set == set(array_hosts):
            return subarray
    return None


def suggest_recording(r, processing=None, verbose=False):
    """Returns a list of all subarrays that we can start recording on.

    processing is a set of hosts that are busy processing, so we can't
    use them to record.
    """
    subbed = set(multicast_subscribed(r))

    # Determine what hosts are already being used
    busy = set()
    if processing is not None:
        busy = busy.union(processing)
        if verbose:
            print("hosts that are busy processing:", sorted(busy))
    elif verbose:
        print("assuming there is no processing happening right now")
    hosts_with_files = set(raw_files(r).keys())
    if verbose:
        print("hosts that have raw files:", sorted(hosts_with_files))
    busy = busy.union(hosts_with_files)
    coord_using = coordinator_hosts(r)
    if verbose:
        print("hosts that the coordinator is already using:", sorted(coord_using))
    busy = busy.union(coord_using)

    # See what subarrays don't want any of these hosts
    subarrays = coordinator_subarrays(r)
    if not subarrays:
        if verbose:
            print("no subarrays are active, so we can't record")
        return []
    
    answer = []
    for subarray in subarrays:
        hosts = set(allocated_hosts(r, subarray))
        if not subbed.intersection(hosts):
            if verbose:
                print("we cannot record on {} because no hosts are subscribed".format(
                    subarray))
            continue
        inter = busy.intersection(hosts)
        if inter:
            if verbose:
                print("we cannot record on {} because {} are in use".format(
                    subarray, sorted(inter)))
            continue
        if verbose:
            print("we can record on {} because {} are unused".format(
                subarray, sorted(hosts)))
        answer.append(subarray)
    return answer


def hosts_by_dir(filemap):
    """Create a map of input directory to a set of hosts, given a map of host to files
    """
    answer = {}
    for host, filenames in filemap.items():
        for filename in filenames:
            dirname = os.path.dirname(filename)
            if dirname not in answer:
                answer[dirname] = set()
            answer[dirname].add(host)
    return answer


def suggest_processing(r, processing=None, verbose=False):
    """Returns a map of (input dir, set of hosts) tuples that we could process.

    processing is a set of hosts that are already busy processing, so we can't
    use them to start a new round of processing.
    """
    # Determine what hosts we can use
    busy = set()
    if processing is not None:
        busy = busy.union(processing)
        if verbose:
            print("hosts that are already processing:", sorted(busy))
    elif verbose:
        print("assuming there is no processing already happening")
    coord_using = coordinator_hosts(r)
    if verbose:
        print("hosts that the coordinator is using:", sorted(coord_using))
    busy = busy.union(coord_using)

    # See what files there are to process
    filemap = raw_files(r)
    if not filemap:
        if verbose:
            print("there are no raw files anywhere, so we can't process")
        return {}
    potential = hosts_by_dir(filemap)

    # Filter for input directories where none of the hosts are busy
    answer = {}
    for dirname, hosts in potential.items():
        inter = hosts.intersection(busy)
        if inter:
            if verbose:
                print("{} has busy hosts: {}".format(dirname, sorted(inter)))
        else:
            if verbose:
                print("{} is ready on hosts: {}".format(dirname, sorted(hosts)))
            answer[dirname] = hosts

    return answer


def join_gateway_group(r, instances, group_name, gateway_domain):
    """Instruct hashpipe instances to join a hashpipe-redis gateway group.
    
    Hashpipe-redis gateway keys can be published for all these nodes
    simultaneously by publishing to the Redis channel:
    <gateway_domain>:<group_name>///set
    """
    # Instruct each instance to join specified group:
    for i in range(len(instances)):
        node_gateway_channel = '{}://{}/gateway'.format(gateway_domain, instances[i])
        msg = 'join={}'.format(group_name)
        r.publish(node_gateway_channel, msg)
    log.info('Instances {} instructed to join gateway group: {}'.format(instances, group_name))

    
def leave_gateway_group(r, group_name, gateway_domain):
    """Instruct hashpipe instances to leave a hashpipe-redis gateway group.
    """
    message = 'leave={}'.format(group_name)
    publish_gateway_message(r, group_name, gateway_domain, message)
    log.info('Instances instructed to leave the gateway group: {}'.format(group_name))

    
def publish_gateway_message(r, group_name, gateway_domain, message):
    """Publish a message to a hashpipe-redis group gateway <group_name>.
    """
    group_gateway_channel = '{}:{}///gateway'.format(gateway_domain, group_name)
    r.publish(group_gateway_channel, message)

    
def set_group_key(r, group_name, gateway_domain, key, value):
    """Set a hashpipe-redis gateway key for a specified hashpipe-redis gateway
    group.
    """
    group_channel = '{}:{}///set'.format(gateway_domain, group_name)
    # Message to set key:
    message = '{}={}'.format(key, value)
    r.publish(group_channel, message)
    log.info('Set {} to {} for {} instances in group {}'.format(key, 
                                                                value, 
                                                                gateway_domain,
                                                                group_name))

def hpguppi_procstat(r):
    """Returns a map from hpguppi states to a list of hosts in them.

    Expected states: IDLE, START, END, None
    """
    answer = {}
    for host, procstat in get_status(r, "blproc", "PROCSTAT"):
        if procstat not in answer:
            answer[procstat] = []
        answer[procstat].append(host)
    return answer


def last_seticore_error(r):
    """Returns a tuple of (host, log lines) for the most recent seticore run that
    ended in an error.
    Returns (None, []) if no errors are found.
    """
    answer_host = None
    answer_run_line = None
    answer_lines = []
    for host in all_hosts(r):
        filename = "/home/obs/seticore_slurm/seticore_{}.err".format(host)
        try:
            lines = open(filename).readlines()
        except:
            continue
        reversed_lines = []
        for line in reversed(lines[-100:]):
            reversed_lines.append(line.strip("\n"))
            if "running seticore" in line:
                break
        else:
            # We have more than 100 lines of error output, seems weird
            continue
        possible_run_line = reversed_lines.pop()
        if not reversed_lines:
            # There's no error here
            continue
        if answer_run_line is None or possible_run_line > answer_run_line:
            # This one looks like the most recent error so far
            answer_host = host
            answer_run_line = possible_run_line
            answer_lines = list(reversed(reversed_lines))

    # Truncate the error lines for nicer display
    half_window = 5
    if len(answer_lines) > 2 * half_window + 1:
        snipped = len(answer_lines) - 2 * half_window
        answer_lines = answer_lines[:half_window] + [
            "<{} lines snipped>".format(snipped)] + answer_lines[-half_window:]
    return answer_host, answer_lines


def timestring():
    """A standard format to report the current time in"""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def pktidx_to_timestamp(r, pktidx, subarray):
    """
    Converts a PKTIDX value into a floating point unix timestamp in UTC, using
    metadata from redis for a given subarray.
    """
    if pktidx < 0:
        raise ValueError("cannot convert pktidx {} to a timestamp".format(pktidx))

    pipe = r.pipeline()
    for subkey in ["hclocks", "synctime", "fenchan", "chan_bw"]:
        pipe.get(subarray + ":" + subkey)
    results = pipe.execute()
    hclocks, synctime, fenchan, chan_bw = map(float, results)

    # Seconds since SYNCTIME: PKTIDX*HCLOCKS/(2e6*FENCHAN*ABS(CHAN_BW))
    timestamp = synctime + pktidx * hclocks / (2e6 * fenchan * abs(chan_bw))

    return timestamp


def alert(r, message, name, slack_channel=SLACK_CHANNEL,
          slack_proxy_channel=SLACK_PROXY_CHANNEL):
    """Publish a message to the alerts Slack channel. 
    Args:
        message (str): Message to publish to Slack.
        name (str): Name of process issuing the alert.   
        slack_channel (str): Slack channel to publish message to. 
        slack_proxy_channel (str): Redis channel for the Slack proxy/bridge. 
    Returns:
        None  
    """
    log.info(message)
    # Format: <Slack channel>:<Slack message text>
    alert_msg = '{}:[{}] {}: {}'.format(slack_channel, timestring(), name, message)
    r.publish(slack_proxy_channel, alert_msg)


def show_status(r):
    broken = broken_daqs(r)
    if broken:
        print(len(broken), "daqs (bluse_hashpipe) are broken:")
        print(broken)
        print()
    subbed = multicast_subscribed(r)
    print(len(subbed), "hosts are subscribed to F-engine multicast:")
    print(subbed)
    print()
    ready = ready_to_record(r)
    print("the coordinator is ready to record (nshot>0) on", len(ready), "hosts:")
    print(ready)
    print()
    recording = get_recording(r)
    if recording:
        print(len(recording), "hosts are currently recording:")
        print(recording)
    else:
        print("no hosts are currently recording")
    dirmap = hosts_by_dir(raw_files(r))
    if not dirmap:
        print()
        print("no hosts have raw files")
    for d, hosts in sorted(dirmap.items()):
        print()
        print(len(hosts), "hosts have raw files in", d, ":")
        print(sorted(hosts))
    for stat, hosts in hpguppi_procstat(r).items():
        if stat in [None, "IDLE", "END"]:
            continue
        print()
        print(len(hosts), "hosts are in hpguppi_proc state", stat, ":")
        print(hosts)

        
def main():
    if len(sys.argv) < 2:
        print("no command specified")
        return
    
    command = sys.argv[1]
    args = sys.argv[2:]
    r = redis.StrictRedis(decode_responses=True)

    if command == "raw_files":
        rawmap = raw_files(r)
        for host, result in sorted(rawmap.items()):
            for r in result:
                print(host, r)
        return

    if command == "sb_id":
        arr = args[0]
        print(sb_id(r, arr))
        return

    if command == "coordinator_subarrays":
        for subarray in coordinator_subarrays(r):
            print(subarray)
        return

    if command == "allocated_hosts":
        subarray = args[0]
        hosts = allocated_hosts(r, subarray)
        print(subarray, "has", len(hosts), "allocated hosts:")
        print(" ".join(hosts))
        return

    if command == "get_nshot":
        subarray = args[0]
        nshot = get_nshot(r, subarray)
        print(nshot)
        return

    if command == "all_hosts":
        print(" ".join(all_hosts(r)))
        return

    if command == "coordinator_hosts":
        hosts = coordinator_hosts(r)
        print("the coordinator is using", len(hosts), "hosts:")
        print(" ".join(hosts))
        return

    if command == "suggest_recording":
        subarrays = suggest_recording(r, verbose=True)
        print(subarrays)
        return

    if command == "suggest_processing":
        dirmap = suggest_processing(r, verbose=True)
        print(sorted(dirmap.keys()))
        return

    if command == "status":
        show_status(r)
        return

    if command == "last_seticore_error":
        host, lines = last_seticore_error(r)
        if host is None:
            print("no recent seticore errors found")
        else:
            print("seticore error on {}:".format(host))
            for line in lines:
                print(line)
        return

    if command == "get_bluse_status":
        key = args[0]
        for host, value in sorted(get_status(r, "bluse", key)):
            print(host, value)
        return

    if command == "hpguppi_procstat":
        print()
        for stat, hosts in hpguppi_procstat(r).items():
            print(len(hosts), "hosts are in hpguppi_proc state", stat, ":")
            print(hosts)
            print()
        return

    if command == "pktidx_to_timestamp":
        pktidx_str, subarray = args
        pktidx = int(pktidx_str)
        print(pktidx_to_timestamp(r, pktidx, subarray))
        return
    
    print("unrecognized command:", command)

    
if __name__ == "__main__":
    main()
