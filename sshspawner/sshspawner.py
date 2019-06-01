import asyncio, asyncssh
import os
from textwrap import dedent
import warnings
import random

from traitlets import Bool, Unicode, Integer, List, observe

from jupyterhub.spawner import Spawner


class SSHSpawner(Spawner):

    remote_hosts = List(Unicode(),
        help=dedent("""Remote hosts available for spawning notebook servers.

        This is a list of remote hosts where notebook servers can be spawned.
        The `choose_remote_host()` method will select one of these hosts at 
        random, unless it is overridden by another algorithm that could say,
        perform load balancing.

        If this contains a single remote host value, that host will always be
        selected (unless `choose_remote_host()` does something odd like just
        return some other value).  That would be appropriate if there is just
        one remote host available, or, if the remote host is itself a load 
        balancer or is doing round-robin DNS.  That is usually a better choice
        than trying to handle load-balancing through this spawner."""),
        config=True)

    remote_host = Unicode("",
        help=dedent("""Remote host selected for spawning notebook servers.
        
        This is selected by the `choose_remote_host()` method.  See also
        `remote_hosts` documentation."""))

    # TODO Check for removal, there's already `ip`.
    remote_ip = Unicode("",
        help=dedent("""Remote IP of spawned notebook server.

        Because the selected remote host may be a load-balancer the spawned
        notebook may have a different IP from that of `remote_host`.  This 
        value is returned from the spawned server usually."""))

    path = Unicode("/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin",
            help="Default PATH (should include jupyter and python)",
            config=True)

    # The get_port.py script is in scripts/get_port.py
    # FIXME See if we avoid having to deploy a script on remote side?
    # For instance, we could just install sshspawner on the remote side
    # as a package and have it put get_port.py in the right place.
    # If we were fancy it could be configurable so it could be restricted
    # to specific ports.
    remote_port_command = Unicode("/usr/bin/python /usr/local/bin/get_port.py",
            help="Command to return unused port on remote host",
            config=True)

    # FIXME Fix help, what happens when not set?
    hub_api_url = Unicode("",
            help=dedent("""If set, Spawner will configure the containers to use
            the specified URL to connect the hub api. This is useful when the
            hub_api is bound to listen on all ports or is running inside of a
            container."""),
            config=True)

    ssh_keyfile = Unicode("~/.ssh/id_rsa",
            help=dedent("""Key file used to authenticate hub with remote host.

            `~` will be expanded to the user's home directory and `{username}`
            will be expanded to the user's username"""),
            config=True)

    pid = Integer(0,
            help=dedent("""Process ID of single-user server process spawned for
            current user."""))

    def load_state(self, state):
        """Restore state about ssh-spawned server after a hub restart.

        The ssh-spawned processes need IP and the process id."""
        super().load_state(state)
        if "pid" in state:
            self.pid = state["pid"]
        if "remote_ip" in state:
            self.remote_ip = state["remote_ip"]

    def get_state(self):
        """Save state needed to restore this spawner instance after hub restore.

        The ssh-spawned processes need IP and the process id."""
        state = super().get_state()
        if self.pid:
            state["pid"] = self.pid
        if self.remote_ip:
            state["remote_ip"] = self.remote_ip
        return state

    def clear_state(self):
        """Clear stored state about this spawner (ip, pid)"""
        super().clear_state()
        self.remote_ip = "remote_ip"
        self.pid = 0

    async def start(self):
        """Start single-user server on remote host."""

        self.remote_host = self.choose_remote_host()
        
        self.remote_ip, port = await self.remote_random_port()
        if self.remote_ip is None or port is None or port == 0:
            return False
        cmd = []

        cmd.extend(self.cmd)
        cmd.extend(self.get_args())

        if self.hub_api_url != "":
            old = "--hub-api-url={}".format(self.hub.api_url)
            new = "--hub-api-url={}".format(self.hub_api_url)
            for index, value in enumerate(cmd):
                if value == old:
                    cmd[index] = new
        for index, value in enumerate(cmd):
            if value[0:6] == '--port':
                cmd[index] = '--port=%d' % (port)

        remote_cmd = ' '.join(cmd)

        self.pid = await self.exec_notebook(remote_cmd)

        self.log.debug("Starting User: {}, PID: {}".format(self.user.name, self.pid))

        if self.pid < 0:
            return None

        return (self.remote_ip, port)

    async def poll(self):
        """Poll ssh-spawned process to see if it is still running.

        If it is still running return None. If it is not running return exit
        code of the process if we have access to it, or 0 otherwise."""

        if not self.pid:
                # no pid, not running
            self.clear_state()
            return 0

        # send signal 0 to check if PID exists
        alive = await self.remote_signal(0)
        self.log.debug("Polling returned {}".format(alive))

        if not alive:
            self.clear_state()
            return 0
        else:
            return None

    async def stop(self, now=False):
        """Stop single-user server process for the current user."""
        alive = await self.remote_signal(15)
        self.clear_state()

    def get_remote_user(self, username):
        """Map JupyterHub username to remote username."""
        return username

    def choose_remote_host(self):
        """
        Given the list of possible nodes from which to choose, make the choice of which should be the remote host.
        """
        remote_host = random.choice(self.remote_hosts)
        return remote_host

    @observe('remote_host')
    def _log_remote_host(self, change):
        self.log.debug("Remote host was set to %s." % self.remote_host)

    @observe('remote_ip')
    def _log_remote_ip(self, change):
        self.log.debug("Remote IP was set to %s." % self.remote_ip)

    # FIXME this needs to now return IP and port too
    async def remote_random_port(self):
        """Select unoccupied port on the remote host and return it. 
        
        If this fails for some reason return `None`."""

        username = self.get_remote_user(self.user.name)
        kf = self.ssh_keyfile.format(username=username)
        cf = kf + "-cert.pub"
        k = asyncssh.read_private_key(kf)
        c = asyncssh.read_certificate(cf)

        # this needs to be done against remote_host, first time we're calling up
        async with asyncssh.connect(self.remote_host,username=username,client_keys=[(k,c)],known_hosts=None) as conn:
            result = await conn.run(self.remote_port_command)
            stdout = result.stdout
            stderr = result.stderr
            retcode = result.exit_status

        if stdout != b"":
            ip, port = stdout.split()
            port = int(port)
            self.log.debug("ip={} port={}".format(ip, port))
        else:
            ip, port = None, None
            self.log.error("Failed to get a remote port")
            self.log.error("STDERR={}".format(stderr))
            self.log.debug("EXITSTATUS={}".format(retcode))
        return (ip, port)

    # FIXME add docstring
    async def exec_notebook(self, command):
        """TBD"""

        env = super(SSHSpawner, self).get_env()
        env['JUPYTERHUB_API_URL'] = self.hub_api_url
        env['PATH'] = self.path
        username = self.get_remote_user(self.user.name)
        kf = self.ssh_keyfile.format(username=username)
        cf = kf + "-cert.pub"
        k = asyncssh.read_private_key(kf)
        c = asyncssh.read_certificate(cf)
        bash_script_str = "#!/bin/bash\n"

        for item in env.items():
            # item is a (key, value) tuple
            # command = ('export %s=%s;' % item) + command
            bash_script_str += 'export %s=%s\n' % item
        bash_script_str += 'unset XDG_RUNTIME_DIR\n'

        bash_script_str += 'touch .jupyter.log\n'
        bash_script_str += 'chmod 600 .jupyter.log\n'
        bash_script_str += '%s < /dev/null >> .jupyter.log 2>&1 & pid=$!\n' % command
        bash_script_str += 'echo $pid\n'

        run_script = "/tmp/{}_run.sh".format(self.user.name)
        with open(run_script, "w") as f:
            f.write(bash_script_str)
        if not os.path.isfile(run_script):
            raise Exception("The file " + run_script + "was not created.")
        else:
            with open(run_script, "r") as f:
                self.log.debug(run_script + " was written as:\n" + f.read())

        async with asyncssh.connect(self.remote_ip, username=username,client_keys=[(k,c)],known_hosts=None) as conn:
            result = await conn.run("bash -s", stdin=run_script)
            stdout = result.stdout
            stderr = result.stderr
            retcode = result.exit_status

        self.log.debug("exec_notebook status={}".format(retcode))
        if stdout != b'':
            pid = int(stdout)
        else:
            return -1

        return pid

    async def remote_signal(self, sig):
        """Signal on the remote host."""

        username = self.get_remote_user(self.user.name)
        kf = self.ssh_keyfile.format(username=username)
        cf = kf + "-cert.pub"
        k = asyncssh.read_private_key(kf)
        c = asyncssh.read_certificate(cf)

        command = "kill -s %s %d < /dev/null"  % (sig, self.pid)

        async with asyncssh.connect(self.remote_ip, username=username,client_keys=[(k,c)],known_hosts=None) as conn:
            result = await conn.run(command)
            stdout = result.stdout
            stderr = result.stderr
            retcode = result.exit_status
        self.log.debug("command: {} returned {} --- {} --- {}".format(command, stdout, stderr, retcode))
        return (retcode == 0)
