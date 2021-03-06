"""
Submit jobs to slurm or torque, or with multiprocessing.

============================================================================

        AUTHOR: Michael D Dacre, mike.dacre@gmail.com
  ORGANIZATION: Stanford University
       LICENSE: MIT License, property of Stanford, use as you wish
       CREATED: 2016-44-20 23:03
 Last modified: 2016-03-30 21:33

   DESCRIPTION: Allows simple job submission with either torque, slurm, or
                with the multiprocessing module.
                To set the environement, set QUEUE to one of ['torque',
                'slurm', 'normal'], or run get_cluster_environment().
                To submit a job, run submit().

                All jobs write out a job file before submission, even though
                this is not necessary (or useful) with multiprocessing. In
                normal mode, this is a .cluster file, in slurm is is a
                .cluster.sbatch and a .cluster.script file, in torque it is a
                .cluster.qsub file.

                The name argument is required for submit, it is used to
                generate the STDOUT and STDERR files. Irrespective of mode
                the STDOUT file will be name.cluster.out and the STDERR file
                will be name.cluster.err.

                Note: `.cluster` is added to all names to make deletion less
                dangerous

                Dependency tracking is supported in torque or slurm mode,
                to use it pass a list of job ids to submit or submit_file with
                the `dependencies` keyword argument.

                To clean up cluster files, run clean(directory), if directory
                is not provided, the current directory is used.
                This will delete all files in that were generated by this
                script.

       CAUTION: The clean() function will delete **EVERY** file with
                extensions matching those in this file::
                    .cluster.err
                    .cluster.out
                    .cluster.sbatch & .cluster.script for slurm mode
                    .cluster.qsub for torque mode
                    .cluster for normal mode

============================================================================
"""
import os
import re
from time import sleep
from textwrap import dedent
from subprocess import check_output, CalledProcessError
from multiprocessing import Pool, pool

# Us
from ASEr import run
from ASEr import logme

#########################
#  Which system to use  #
#########################

# Default is normal, change to 'slurm' or 'torque' as needed.
QUEUE          = 'normal'
ALLOWED_QUEUES = ['torque', 'slurm', 'normal']

#########################################################
#  The multiprocessing pool, only used in 'local' mode  #
#########################################################

POOL = None

# Reset broken multithreading
# Some of the numpy C libraries can break multithreading, this command
# fixes the issue.
check_output("taskset -p 0xff %d &>/dev/null" % os.getpid(), shell=True)


def get_cluster_environment():
    """Detect the local cluster environment and set QUEUE globally.

    Uses which to search for sbatch first, then qsub. If neither is found,
    QUEUE is set to local.

    :returns: QUEUE variable ('torque', 'slurm', or 'local')
    """
    global QUEUE
    if run.which('sbatch'):
        QUEUE = 'slurm'
    elif run.which('qsub'):
        QUEUE = 'torque'
    else:
        QUEUE = 'local'
    if QUEUE == 'slurm' or QUEUE == 'torque':
        logme.log('{} detected, using for cluster submissions'.format(QUEUE),
                  'debug')
    else:
        logme.log('No cluster environment detected, using multiprocessing',
                  'debug')
    return QUEUE


#####################################
#  Wait for cluster jobs to finish  #
#####################################


def wait(jobs):
    """Wait for jobs to finish.

    :jobs:    A single job or list of jobs to wait for. With torque or slurm,
              these should be job IDs, with normal mode, these are
              multiprocessing job objects (returned by submit())
    """
    check_queue()  # Make sure the QUEUE is usable

    # Sanitize argument
    if not isinstance(jobs, (list, tuple)):
        jobs = [jobs]
    for job in jobs:
        if not isinstance(job, (str, int, pool.ApplyResult)):
            raise ClusterError('job must be int, string, or ApplyResult, ' +
                               'is {}'.format(type(job)))

    if QUEUE == 'normal':
        for job in jobs:
            if not isinstance(job, pool.ApplyResult):
                raise ClusterError('jobs must be ApplyResult objects')
            job.wait()
    elif QUEUE == 'torque':
        # Wait for 5 seconds before checking, as jobs take a while to be queued
        # sometimes
        sleep(5)

        s = re.compile(r' +')  # For splitting qstat output
        # Jobs must be strings for comparison operations
        jobs = [str(j) for j in jobs]
        while True:
            c = 0
            try:
                q = check_output(['qstat', '-a']).decode().rstrip().split('\n')
            except CalledProcessError:
                if c == 5:
                    raise
                c += 1
                sleep(2)
                continue
            # Check header
            if not re.split(r' {2,100}', q[3])[9] == 'S':
                raise ClusterError('Unrecognized torque qstat format')
            # Build a list of completed jobs
            complete = []
            for j in q[5:]:
                i = s.split(j)
                if i[9] == 'C':
                    complete.append(i[0].split('.')[0])
            # Build a list of all jobs
            all  = [s.split(j)[0].split('.')[0] for j in q[5:]]
            # Trim down job list
            jobs = [j for j in jobs if j in all]
            jobs = [j for j in jobs if j not in complete]
            if len(jobs) == 0:
                return
            sleep(2)
    elif QUEUE == 'slurm':
        # Wait for 2 seconds before checking, as jobs take a while to be queued
        # sometimes
        sleep(2)

        # Jobs must be strings for comparison operations
        jobs = [str(j) for j in jobs]
        while True:
            # Slurm allows us to get a custom output for faster parsing
            q = check_output(
                ['squeue', '-h', '-o', "'%A,%t'"]).decode().rstrip().split(',')
            # Build a list of jobs
            complete = [i[0] for i in q if i[1] == 'CD']
            failed   = [i[0] for i in q if i[1] == 'F']
            all      = [i[0] for i in q]
            # Trim down job list, ignore failures
            jobs = [i for i in jobs if i not in all]
            jobs = [i for i in jobs if i not in complete]
            jobs = [i for i in jobs if i not in failed]
            if len(jobs) == 0:
                return
            sleep(2)


#########################
#  Submissions scripts  #
#########################


def submit(command, name, threads=None, time=None, cores=None, mem=None,
           partition=None, modules=[], path=None, dependencies=None):
    """Submit a script to the cluster.

    Used in all modes::
    :command:   The command to execute.
    :name:      The name of the job.

    Used for normal mode::
    :threads:   Total number of threads to use at a time, defaults to all.

    Used for torque and slurm::
    :time:      The time to run for in HH:MM:SS.
    :cores:     How many cores to run on.
    :mem:       Memory to use in MB.
    :partition: Partition to run on, default 'normal'.
    :modules:   Modules to load with the 'module load' command.
    :path:      Where to create the script, if None, current dir used.

    Returns:
        Job number in torque/slurm mode, 0 in normal mode
    """
    check_queue()  # Make sure the QUEUE is usable

    if QUEUE == 'slurm' or QUEUE == 'torque':
        return submit_file(make_job_file(command, name, time, cores,
                                         mem, partition, modules, path),
                           dependencies=dependencies)
    elif QUEUE == 'normal':
        return submit_file(make_job_file(command, name), name=name,
                           threads=threads)


def submit_file(script_file, name=None, dependencies=None, threads=None):
    """Submit a job file to the cluster.

    If QUEUE is torque, qsub is used; if QUEUE is slurm, sbatch is used;
    if QUEUE is normal, the file is executed with subprocess.

    :dependencies: A job number or list of job numbers.
                   In slurm: `--dependency=afterok:` is used
                   For torque: `-W depend=afterok:` is used

    :threads:      Total number of threads to use at a time, defaults to all.
                   ONLY USED IN NORMAL MODE

    :name:         The name of the job, only used in normal mode.

    :returns:      job number for torque or slurm
                   multiprocessing job object for normal mode
    """
    check_queue()  # Make sure the QUEUE is usable

    # Sanitize arguments
    name = str(name)

    # Check dependencies
    if dependencies:
        if isinstance(dependencies, (str, int)):
            dependencies = [dependencies]
        if not isinstance(dependencies, (list, tuple)):
            raise Exception('dependencies must be a list, int, or string.')
        dependencies = [str(i) for i in dependencies]

    if QUEUE == 'slurm':
        if dependencies:
            dependencies = '--dependency=afterok:{}'.format(
                ':'.join([str(d) for d in dependencies]))
            args = ['sbatch', dependencies, script_file]
        else:
            args = ['sbatch', script_file]
        # Try to submit job 5 times
        count = 0
        while True:
            try:
                job = int(check_output(args).decode().rstrip().split(' ')[-1])
            except CalledProcessError:
                if count == 5:
                    raise
                count += 1
                sleep(1)
                continue
            break
        return job
    elif QUEUE == 'torque':
        if dependencies:
            dependencies = '-W depend={}'.format(
                ','.join(['afterok:' + d for d in dependencies]))
            args = ['qsub', dependencies, script_file]
        else:
            args = ['qsub', script_file]
        # Try to submit job 5 times
        count = 0
        while True:
            try:
                job = int(check_output(args).decode().rstrip().split('.')[0])
            except CalledProcessError:
                if count == 5:
                    raise
                count += 1
                sleep(1)
                continue
            break
        return job
    elif QUEUE == 'normal':
        global POOL
        if not POOL:
            POOL = Pool(threads) if threads else Pool()
        command = 'bash {}'.format(script_file)
        args = dict(stdout=name + '.cluster.out', stderr=name + '.cluster.err')
        return POOL.apply_async(run.cmd, (command,), args)


#########################
#  Job file generation  #
#########################


def make_job_file(command, name, time=None, cores=1, mem=None, partition=None,
                  modules=[], path=None):
    """Make a job file compatible with the chosen cluster.

    If mode is normal, this is just a simple shell script.

    Note: Only requests one node.
    :command:   The command to execute.
    :name:      The name of the job.
    :time:      The time to run for in HH:MM:SS.
    :cores:     How many cores to run on.
    :mem:       Memory to use in MB.
    :partition: Partition to run on, default 'normal'.
    :modules:   Modules to load with the 'module load' command.
    :path:      Where to create the script, if None, current dir used.
    :returns:   The absolute path of the submission script.
    """
    check_queue()  # Make sure the QUEUE is usable

    # Sanitize arguments
    name    = str(name)
    cores   = cores if cores else 1  # In case cores are passed as None
    modules = [modules] if isinstance(modules, str) else modules
    usedir  = os.path.abspath(path) if path else os.path.abspath('.')
    precmd  = ''
    for module in modules:
        precmd += 'module load {}\n'.format(module)
    precmd += dedent("""\
        cd {}
        date +'%d-%H:%M:%S'
        echo "Running {}"
        """.format(usedir, name))
    pstcmd = dedent("""\
        exitcode=$?
        echo Done
        date +'%d-%H:%M:%S'
        if [[ $exitcode != 0 ]]; then
            echo Exited with code: $? >&2
        fi
        """)
    if QUEUE == 'slurm':
        scrpt = os.path.join(usedir, '{}.cluster.sbatch'.format(name))
        with open(scrpt, 'w') as outfile:
            outfile.write('#!/bin/bash\n')
            if partition:
                outfile.write('#SBATCH -p {}\n'.format(partition))
            outfile.write('#SBATCH --ntasks 1\n')
            outfile.write('#SBATCH --cpus-per-task {}\n'.format(cores))
            if time:
                outfile.write('#SBATCH --time={}\n'.format(time))
            if mem:
                outfile.write('#SBATCH --mem={}\n'.format(mem))
            outfile.write('#SBATCH -o {}.cluster.out\n'.format(name))
            outfile.write('#SBATCH -e {}.cluster.err\n'.format(name))
            outfile.write('cd {}\n'.format(usedir))
            outfile.write('srun bash {}.script\n'.format(
                os.path.join(usedir, name)))
        with open(os.path.join(usedir, name + '.script'), 'w') as outfile:
            outfile.write('#!/bin/bash\n')
            outfile.write('mkdir -p $LOCAL_SCRATCH\n')
            outfile.write(precmd)
            outfile.write(command + '\n')
            outfile.write(pstcmd)
    elif QUEUE == 'torque':
        scrpt = os.path.join(usedir, '{}.cluster.qsub'.format(name))
        with open(scrpt, 'w') as outfile:
            outfile.write('#!/bin/bash\n')
            if partition:
                outfile.write('#PBS -q {}\n'.format(partition))
            outfile.write('#PBS -l nodes=1:ppn={}\n'.format(cores))
            if time:
                outfile.write('#PBS -l walltime={}\n'.format(time))
            if mem:
                outfile.write('#PBS mem={}MB\n'.format(mem))
            outfile.write('#PBS -o {}.cluster.out\n'.format(name))
            outfile.write('#PBS -e {}.cluster.err\n\n'.format(name))
            outfile.write('mkdir -p $LOCAL_SCRATCH\n')
            outfile.write(precmd)
            outfile.write(command + '\n')
            outfile.write(pstcmd)
    elif QUEUE == 'normal':
        scrpt = os.path.join(usedir, '{}.cluster'.format(name))
        with open(scrpt, 'w') as outfile:
            outfile.write('#!/bin/bash\n')
            outfile.write(precmd)
            outfile.write(command + '\n')
            outfile.write(pstcmd)

    # Return the path to the script
    return scrpt


##############
#  Cleaning  #
##############


def clean(directory='.'):
    """Delete all files made by this module in directory.

    CAUTION: The clean() function will delete **EVERY** file with
             extensions matching those in this file::
                 .cluster.err
                 .cluster.out
                 .cluster.sbatch & .cluster.script for slurm mode
                 .cluster.qsub for torque mode
                 .cluster for normal mode

    :directory: The directory to run in, defaults to the current directory.
    :returns:   A set of deleted files
    """
    check_queue()  # Make sure the QUEUE is usable

    extensions = ['.cluster.err', '.cluster.out']
    if QUEUE == 'normal':
        extensions.append('.cluster')
    elif QUEUE == 'slurm':
        extensions = extensions + ['.cluster.sbatch', '.cluster.script']
    elif QUEUE == 'torque':
        extensions.append('.cluster.qsub')

    files = [i for i in os.listdir(os.path.abspath(directory))
             if os.path.isfile(i)]

    if not files:
        logme.log('No files found.', 'debug')
        return []

    deleted = []
    for f in files:
        for extension in extensions:
            if f.endswith(extension):
                os.remove(f)
                deleted.append(f)

    return deleted


###################
#  House Keeping  #
###################

class ClusterError(Exception):

    """A custom exception for cluster errors."""

    pass


def check_queue():
    """Raise exception if QUEUE is incorrect."""
    if QUEUE not in ALLOWED_QUEUES:
        raise ClusterError('QUEUE value {} is not recognized, '.format(QUEUE) +
                           'should be: normal, torque, or slurm')
