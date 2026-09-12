"""One SSH connection, many channels.

The whole app hangs off a single authenticated paramiko Transport:
  - open_shell()  -> interactive PTY channel fed into the VTE terminal
  - sftp()        -> SFTP subsystem for the file-transfer helper
  - run()         -> short exec channels for side-queries (cwd, ps, mysql -e)
"""
import os
import shlex
import paramiko


def resolve_ssh_config(host, user=None, port=None):
    """Apply ~/.ssh/config (HostName/User/Port/IdentityFile) to explicit args.
    Explicit args win over the config file."""
    cfg = paramiko.SSHConfig()
    path = os.path.expanduser("~/.ssh/config")
    if os.path.exists(path):
        with open(path) as f:
            cfg.parse(f)
    h = cfg.lookup(host)
    return {
        "hostname": h.get("hostname", host),
        "user": user or h.get("user"),
        "port": int(port or h.get("port", 22)),
        "key_filename": h.get("identityfile"),
    }


class SSHConnection:
    def __init__(self, host, user=None, port=None, password=None, key_filename=None,
                 timeout=15):
        r = resolve_ssh_config(host, user, port)
        self.host = host
        self.hostname = r["hostname"]
        self.user = r["user"]
        self.port = r["port"]
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            self.hostname, port=self.port, username=self.user, password=password,
            key_filename=key_filename or r["key_filename"],
            allow_agent=True, look_for_keys=True, timeout=timeout,
        )
        self.transport = self._client.get_transport()
        self.transport.set_keepalive(30)
        self._sftp = None
        self.shell_pid = None  # remote PID of the interactive shell, set by Terminal

    # ---- channels -------------------------------------------------------
    def open_shell(self, term="xterm-256color", cols=80, rows=24):
        chan = self.transport.open_session()
        chan.get_pty(term=term, width=cols, height=rows)
        chan.invoke_shell()
        return chan

    def sftp(self):
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def run(self, cmd, timeout=10):
        """Run a command on a fresh exec channel. Returns (rc, stdout, stderr)."""
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        return stdout.channel.recv_exit_status(), out, err

    # ---- side-channel introspection ------------------------------------
    def shell_cwd(self):
        """Current directory of the interactive shell (needs shell_pid)."""
        if not self.shell_pid:
            return None
        rc, out, _ = self.run(f"readlink /proc/{self.shell_pid}/cwd")
        return out.strip() if rc == 0 and out.strip() else None

    def foreground_process(self):
        """(pid, comm, argv) of the shell's foreground child, or None.

        Used by the DB helper to detect a running `mysql`/`mariadb` client
        and recover its -u/-h/-P/database args.
        """
        if not self.shell_pid:
            return None
        # tpgid = foreground process group of the shell's controlling tty
        rc, out, _ = self.run(
            f"awk '{{print $8}}' /proc/{self.shell_pid}/stat")
        if rc != 0:
            return None
        tpgid = out.strip()
        if not tpgid.isdigit() or int(tpgid) == self.shell_pid:
            return None
        rc, out, _ = self.run(
            f"cat /proc/{tpgid}/comm; tr '\\0' '\\n' < /proc/{tpgid}/cmdline")
        if rc != 0:
            return None
        lines = out.splitlines()
        return int(tpgid), lines[0], lines[1:]

    def close(self):
        if self._sftp:
            self._sftp.close()
        self._client.close()
