"""Launching tool for DGL distributed training"""
import os
import stat
import sys
import subprocess
import argparse
import signal
import logging
import time
import json
import multiprocessing
import re
from functools import partial
from threading import Thread
from typing import Optional

DEFAULT_PORT = 30050

def cleanup_proc(get_all_remote_pids, conn):
    '''This process tries to clean up the remote training tasks.
    '''
    print('cleanupu process runs')
    # This process should not handle SIGINT.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    data = conn.recv()
    # If the launch process exits normally, this process doesn't need to do anything.
    if data == 'exit':
        sys.exit(0)
    else:
        remote_pids = get_all_remote_pids()
        # Otherwise, we need to ssh to each machine and kill the training jobs.
        for (ip, port), pids in remote_pids.items():
            kill_process(ip, port, pids)
    print('cleanup process exits')

def kill_process(ip, port, pids):
    '''ssh to a remote machine and kill the specified processes.
    '''
    curr_pid = os.getpid()
    killed_pids = []
    # If we kill child processes first, the parent process may create more again. This happens
    # to Python's process pool. After sorting, we always kill parent processes first.
    pids.sort()
    for pid in pids:
        assert curr_pid != pid
        print('kill process {} on {}:{}'.format(pid, ip, port), flush=True)
        kill_cmd = 'ssh -o StrictHostKeyChecking=no -p ' + str(port) + ' ' + ip + ' \'kill {}\''.format(pid)
        subprocess.run(kill_cmd, shell=True)
        killed_pids.append(pid)
    # It's possible that some of the processes are not killed. Let's try again.
    for i in range(3):
        killed_pids = get_killed_pids(ip, port, killed_pids)
        if len(killed_pids) == 0:
            break
        else:
            killed_pids.sort()
            for pid in killed_pids:
                print('kill process {} on {}:{}'.format(pid, ip, port), flush=True)
                kill_cmd = 'ssh -o StrictHostKeyChecking=no -p ' + str(port) + ' ' + ip + ' \'kill -9 {}\''.format(pid)
                subprocess.run(kill_cmd, shell=True)

def get_killed_pids(ip, port, killed_pids):
    '''Get the process IDs that we want to kill but are still alive.
    '''
    killed_pids = [str(pid) for pid in killed_pids]
    killed_pids = ','.join(killed_pids)
    ps_cmd = 'ssh -o StrictHostKeyChecking=no -p ' + str(port) + ' ' + ip + ' \'ps -p {} -h\''.format(killed_pids)
    res = subprocess.run(ps_cmd, shell=True, stdout=subprocess.PIPE)
    pids = []
    for p in res.stdout.decode('utf-8').split('\n'):
        l = p.split()
        if len(l) > 0:
            pids.append(int(l[0]))
    return pids

def execute_remote(
    cmd: str,
    ip: str,
    port: int,
    username: Optional[str] = ""
) -> Thread:
    """Execute command line on remote machine via ssh.

    Args:
        cmd: User-defined command (udf) to execute on the remote host.
        ip: The ip-address of the host to run the command on.
        port: Port number that the host is listening on.
        thread_list:
        username: Optional. If given, this will specify a username to use when issuing commands over SSH.
            Useful when your infra requires you to explicitly specify a username to avoid permission issues.

    Returns:
        thread: The Thread whose run() is to run the `cmd` on the remote host. Returns when the cmd completes
            on the remote host.
    """
    ip_prefix = ""
    if username:
        ip_prefix += "{username}@".format(username=username)

    # Construct ssh command that executes `cmd` on the remote host
    ssh_cmd = "ssh -o StrictHostKeyChecking=no -p {port} {ip_prefix}{ip} '{cmd}'".format(
        port=str(port),
        ip_prefix=ip_prefix,
        ip=ip,
        cmd=cmd,
    )

    # thread func to run the job
    def run(ssh_cmd):
        subprocess.check_call(ssh_cmd, shell=True)

    thread = Thread(target=run, args=(ssh_cmd,))
    thread.setDaemon(True)
    thread.start()
    return thread

def get_remote_pids(ip, port, cmd_regex):
    """Get the process IDs that run the command in the remote machine.
    """
    pids = []
    curr_pid = os.getpid()
    # Here we want to get the python processes. We may get some ssh processes, so we should filter them out.
    ps_cmd = 'ssh -o StrictHostKeyChecking=no -p ' + str(port) + ' ' + ip + ' \'ps -aux | grep python | grep -v StrictHostKeyChecking\''
    res = subprocess.run(ps_cmd, shell=True, stdout=subprocess.PIPE)
    for p in res.stdout.decode('utf-8').split('\n'):
        l = p.split()
        if len(l) < 2:
            continue
        # We only get the processes that run the specified command.
        res = re.search(cmd_regex, p)
        if res is not None and int(l[1]) != curr_pid:
            pids.append(l[1])

    pid_str = ','.join([str(pid) for pid in pids])
    ps_cmd = 'ssh -o StrictHostKeyChecking=no -p ' + str(port) + ' ' + ip + ' \'pgrep -P {}\''.format(pid_str)
    res = subprocess.run(ps_cmd, shell=True, stdout=subprocess.PIPE)
    pids1 = res.stdout.decode('utf-8').split('\n')
    all_pids = []
    for pid in set(pids + pids1):
        if pid == '' or int(pid) == curr_pid:
            continue
        all_pids.append(int(pid))
    all_pids.sort()
    return all_pids

def get_all_remote_pids(hosts, ssh_port, udf_command):
    '''Get all remote processes.
    '''
    remote_pids = {}
    for node_id, host in enumerate(hosts):
        ip, _ = host
        # When creating training processes in remote machines, we may insert some arguments
        # in the commands. We need to use regular expressions to match the modified command.
        cmds = udf_command.split()
        new_udf_command = ' .*'.join(cmds)
        pids = get_remote_pids(ip, ssh_port, new_udf_command)
        remote_pids[(ip, ssh_port)] = pids
    return remote_pids


def construct_torch_dist_launcher_cmd(
    num_trainers: int,
    num_nodes: int,
    node_rank: int,
    master_addr: str,
    master_port: int
) -> str:
    """Constructs the torch distributed launcher command.
    Helper function.

    Args:
        num_trainers:
        num_nodes:
        node_rank:
        master_addr:
        master_port:

    Returns:
        cmd_str.
    """
    torch_cmd_template = "-m torch.distributed.launch " \
                         "--nproc_per_node={nproc_per_node} " \
                         "--nnodes={nnodes} " \
                         "--node_rank={node_rank} " \
                         "--master_addr={master_addr} " \
                         "--master_port={master_port}"
    return torch_cmd_template.format(
        nproc_per_node=num_trainers,
        nnodes=num_nodes,
        node_rank=node_rank,
        master_addr=master_addr,
        master_port=master_port
    )


def wrap_udf_in_torch_dist_launcher(
    udf_command: str,
    num_trainers: int,
    num_nodes: int,
    node_rank: int,
    master_addr: str,
    master_port: int,
) -> str:
    """Wraps the user-defined function (udf_command) with the torch.distributed.launch module.

     Example: if udf_command is "python3 run/some/trainer.py arg1 arg2", then new_df_command becomes:
         "python3 -m torch.distributed.launch <TORCH DIST ARGS> run/some/trainer.py arg1 arg2

    udf_command is assumed to consist of pre-commands (optional) followed by the python launcher script (required):
    Examples:
        # simple
        python3.7 path/to/some/trainer.py arg1 arg2

        # multi-commands
        (cd some/dir && python3.7 path/to/some/trainer.py arg1 arg2)

    IMPORTANT: If udf_command consists of multiple python commands, then this will result in undefined behavior.

    Args:
        udf_command:
        num_trainers:
        num_nodes:
        node_rank:
        master_addr:
        master_port:

    Returns:

    """
    torch_dist_cmd = construct_torch_dist_launcher_cmd(
        num_trainers=num_trainers,
        num_nodes=num_nodes,
        node_rank=node_rank,
        master_addr=master_addr,
        master_port=master_port
    )
    # Auto-detect the python binary that kicks off the distributed trainer code.
    # Note: This allowlist order matters, this will match with the FIRST matching entry. Thus, please add names to this
    #       from most-specific to least-specific order eg:
    #           (python3.7, python3.8) -> (python3)
    # The allowed python versions are from this: https://www.dgl.ai/pages/start.html
    python_bin_allowlist = (
        "python3.6", "python3.7", "python3.8", "python3.9", "python3",
        # for backwards compatibility, accept python2 but technically DGL is a py3 library, so this is not recommended
        "python2.7", "python2",
    )
    # If none of the candidate python bins match, then we go with the default `python`
    python_bin = "python"
    for candidate_python_bin in python_bin_allowlist:
        if candidate_python_bin in udf_command:
            python_bin = candidate_python_bin
            break

    # transforms the udf_command from:
    #     python path/to/dist_trainer.py arg0 arg1
    # to:
    #     python -m torch.distributed.launch [DIST TORCH ARGS] path/to/dist_trainer.py arg0 arg1
    # Note: if there are multiple python commands in `udf_command`, this may do the Wrong Thing, eg launch each
    #       python command within the torch distributed launcher.
    new_udf_command = udf_command.replace(python_bin, f"{python_bin} {torch_dist_cmd}")

    return new_udf_command


def submit_jobs(args, udf_command):
    """Submit distributed jobs (server and client processes) via ssh"""
    hosts = []
    thread_list = []
    server_count_per_machine = 0

    # Get the IP addresses of the cluster.
    ip_config = args.workspace + '/' + args.ip_config
    with open(ip_config) as f:
        for line in f:
            result = line.strip().split()
            if len(result) == 2:
                ip = result[0]
                port = int(result[1])
                hosts.append((ip, port))
            elif len(result) == 1:
                ip = result[0]
                port = DEFAULT_PORT
                hosts.append((ip, port))
            else:
                raise RuntimeError("Format error of ip_config.")
            server_count_per_machine = args.num_servers
    # Get partition info of the graph data
    part_config = args.workspace + '/' + args.part_config
    with open(part_config) as conf_f:
        part_metadata = json.load(conf_f)
    assert 'num_parts' in part_metadata, 'num_parts does not exist.'
    # The number of partitions must match the number of machines in the cluster.
    assert part_metadata['num_parts'] == len(hosts), \
            'The number of graph partitions has to match the number of machines in the cluster.'

    tot_num_clients = args.num_trainers * (1 + args.num_samplers) * len(hosts)
    # launch server tasks
    server_cmd = 'DGL_ROLE=server DGL_NUM_SAMPLER=' + str(args.num_samplers)
    server_cmd = server_cmd + ' ' + 'OMP_NUM_THREADS=' + str(args.num_server_threads)
    server_cmd = server_cmd + ' ' + 'DGL_NUM_CLIENT=' + str(tot_num_clients)
    server_cmd = server_cmd + ' ' + 'DGL_CONF_PATH=' + str(args.part_config)
    server_cmd = server_cmd + ' ' + 'DGL_IP_CONFIG=' + str(args.ip_config)
    server_cmd = server_cmd + ' ' + 'DGL_NUM_SERVER=' + str(args.num_servers)
    server_cmd = server_cmd + ' ' + 'DGL_GRAPH_FORMAT=' + str(args.graph_format)
    for i in range(len(hosts)*server_count_per_machine):
        ip, _ = hosts[int(i / server_count_per_machine)]
        cmd = server_cmd + ' ' + 'DGL_SERVER_ID=' + str(i)
        cmd = cmd + ' ' + udf_command
        cmd = 'cd ' + str(args.workspace) + '; ' + cmd
        thread_list.append(execute_remote(cmd, ip, args.ssh_port, username=args.ssh_username))

    # launch client tasks
    client_cmd = 'DGL_DIST_MODE="distributed" DGL_ROLE=client DGL_NUM_SAMPLER=' + str(args.num_samplers)
    client_cmd = client_cmd + ' ' + 'DGL_NUM_CLIENT=' + str(tot_num_clients)
    client_cmd = client_cmd + ' ' + 'DGL_CONF_PATH=' + str(args.part_config)
    client_cmd = client_cmd + ' ' + 'DGL_IP_CONFIG=' + str(args.ip_config)
    client_cmd = client_cmd + ' ' + 'DGL_NUM_SERVER=' + str(args.num_servers)
    if os.environ.get('OMP_NUM_THREADS') is not None:
        client_cmd = client_cmd + ' ' + 'OMP_NUM_THREADS=' + os.environ.get('OMP_NUM_THREADS')
    else:
        client_cmd = client_cmd + ' ' + 'OMP_NUM_THREADS=' + str(args.num_omp_threads)
    if os.environ.get('PYTHONPATH') is not None:
        client_cmd = client_cmd + ' ' + 'PYTHONPATH=' + os.environ.get('PYTHONPATH')
    client_cmd = client_cmd + ' ' + 'DGL_GRAPH_FORMAT=' + str(args.graph_format)

    for node_id, host in enumerate(hosts):
        ip, _ = host
        # Transform udf_command to follow torch's dist launcher format: `PYTHON_BIN -m torch.distributed.launch ... UDF`
        torch_dist_udf_command = wrap_udf_in_torch_dist_launcher(
            udf_command=udf_command,
            num_trainers=args.num_trainers,
            num_nodes=len(hosts),
            node_rank=node_id,
            master_addr=hosts[0][0],
            master_port=1234,
        )
        cmd = client_cmd + ' ' + torch_dist_udf_command
        cmd = 'cd ' + str(args.workspace) + '; ' + cmd
        thread_list.append(execute_remote(cmd, ip, args.ssh_port, username=args.ssh_username))

    # Start a cleanup process dedicated for cleaning up remote training jobs.
    conn1,conn2 = multiprocessing.Pipe()
    func = partial(get_all_remote_pids, hosts, args.ssh_port, udf_command)
    process = multiprocessing.Process(target=cleanup_proc, args=(func, conn1))
    process.start()

    def signal_handler(signal, frame):
        logging.info('Stop launcher')
        # We need to tell the cleanup process to kill remote training jobs.
        conn2.send('cleanup')
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)

    for thread in thread_list:
        thread.join()
    # The training processes complete. We should tell the cleanup process to exit.
    conn2.send('exit')
    process.join()


def main():
    parser = argparse.ArgumentParser(description='Launch a distributed job')
    parser.add_argument('--ssh_port', type=int, default=22, help='SSH Port.')
    parser.add_argument(
        "--ssh_username", default="",
        help="Optional. When issuing commands (via ssh) to cluster, use the provided username in the ssh cmd. "
             "Example: If you provide --ssh_username=bob, then the ssh command will be like: 'ssh bob@1.2.3.4 CMD' "
             "instead of 'ssh 1.2.3.4 CMD'"
    )
    parser.add_argument('--workspace', type=str,
                        help='Path of user directory of distributed tasks. \
                        This is used to specify a destination location where \
                        the contents of current directory will be rsyncd')
    parser.add_argument('--num_trainers', type=int,
                        help='The number of trainer processes per machine')
    parser.add_argument('--num_omp_threads', type=int,
                        help='The number of OMP threads per trainer')
    parser.add_argument('--num_samplers', type=int, default=0,
                        help='The number of sampler processes per trainer process')
    parser.add_argument('--num_servers', type=int,
                        help='The number of server processes per machine')
    parser.add_argument('--part_config', type=str,
                        help='The file (in workspace) of the partition config')
    parser.add_argument('--ip_config', type=str,
                        help='The file (in workspace) of IP configuration for server processes')
    parser.add_argument('--num_server_threads', type=int, default=1,
                        help='The number of OMP threads in the server process. \
                        It should be small if server processes and trainer processes run on \
                        the same machine. By default, it is 1.')
    parser.add_argument('--graph_format', type=str, default='csc',
                        help='The format of the graph structure of each partition. \
                        The allowed formats are csr, csc and coo. A user can specify multiple \
                        formats, separated by ",". For example, the graph format is "csr,csc".')
    args, udf_command = parser.parse_known_args()
    assert len(udf_command) == 1, 'Please provide user command line.'
    assert args.num_trainers is not None and args.num_trainers > 0, \
            '--num_trainers must be a positive number.'
    assert args.num_samplers is not None and args.num_samplers >= 0, \
            '--num_samplers must be a non-negative number.'
    assert args.num_servers is not None and args.num_servers > 0, \
            '--num_servers must be a positive number.'
    assert args.num_server_threads > 0, '--num_server_threads must be a positive number.'
    assert args.workspace is not None, 'A user has to specify a workspace with --workspace.'
    assert args.part_config is not None, \
            'A user has to specify a partition configuration file with --part_config.'
    assert args.ip_config is not None, \
            'A user has to specify an IP configuration file with --ip_config.'
    if args.num_omp_threads is None:
        # Here we assume all machines have the same number of CPU cores as the machine
        # where the launch script runs.
        args.num_omp_threads = max(multiprocessing.cpu_count() // 2 // args.num_trainers, 1)
        print('The number of OMP threads per trainer is set to', args.num_omp_threads)

    udf_command = str(udf_command[0])
    if 'python' not in udf_command:
        raise RuntimeError("DGL launching script can only support Python executable file.")
    submit_jobs(args, udf_command)

if __name__ == '__main__':
    fmt = '%(asctime)s %(levelname)s %(message)s'
    logging.basicConfig(format=fmt, level=logging.INFO)
    main()
