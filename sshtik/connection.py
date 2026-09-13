"""One SSH connection, many channels.

The whole app hangs off a single authenticated paramiko Transport:
  - open_shell()  -> interactive PTY channel fed into the VTE terminal
  - sftp()        -> SFTP subsystem for the file-transfer helper
  - run()         -> short exec channels for side-queries (cwd, ps, mysql -e)
"""
import os
import shlex
import threading
import paramiko


class _LockingSFTP:
    """Serialises access to a paramiko SFTPClient across threads.

    paramiko multiplexes one channel per SFTPClient and is not safe for
    concurrent callers: two operations interleaving their packets desync the
    stream and can bring down the whole transport. Every public method is
    proxied under a single lock, and a dropped channel is transparently
    reopened."""
    def __init__(self, factory):
        self._factory = factory
        self._sftp = None
        self._lock = threading.RLock()

    def _client(self):
        chan = self._sftp.get_channel() if self._sftp is not None else None
        if self._sftp is None or chan is None or chan.closed:
            self._sftp = self._factory()
        return self._sftp

    def __getattr__(self, name):
        def method(*args, **kwargs):
            with self._lock:
                return getattr(self._client(), name)(*args, **kwargs)
        return method

    def close(self):
        with self._lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                finally:
                    self._sftp = None


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
                 timeout=15, via=None, resolved=None):
        """`via`: an existing SSHConnection to tunnel through (like ProxyJump).
        `resolved`: {"hostname","user","port"} already resolved on the via host
        (so the via host's ~/.ssh/config applies, not ours)."""
        r = resolved or resolve_ssh_config(host, user, port)
        self.host = host
        self.hostname = r["hostname"]
        self.user = r["user"]
        self.port = int(r["port"] or 22)
        self.via = via
        sock = None
        if via is not None:
            sock = via.transport.open_channel(
                "direct-tcpip", (self.hostname, self.port), ("127.0.0.1", 0), timeout=timeout)
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            self.hostname, port=self.port, username=self.user, password=password,
            key_filename=key_filename or r.get("key_filename"),
            allow_agent=True, look_for_keys=True, timeout=timeout, sock=sock,
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
            self._sftp = _LockingSFTP(self._client.open_sftp)
        return self._sftp

    def run(self, cmd, timeout=10):
        """Run a command on a fresh exec channel. Returns (rc, stdout, stderr)."""
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        return stdout.channel.recv_exit_status(), out, err

    # ---- side-channel introspection ------------------------------------
    def discover_shell_pid(self, attempts=10, delay=0.2):
        """Find the interactive shell of this connection without typing into it.

        All channels of one SSH connection are children of the same
        per-connection sshd process, so from an exec channel $PPID is that
        sshd and the child that owns a pty is our shell."""
        import time
        for _ in range(attempts):
            rc, out, _ = self.run("ps -o pid=,tty= --ppid $PPID 2>/dev/null")
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].startswith(("pts", "tty")) and parts[0].isdigit():
                    self.shell_pid = int(parts[0])
                    return self.shell_pid
            time.sleep(delay)
        return None

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

    # ---- nested ssh ("follow the hop") -----------------------------------
    def hop_target(self):
        """If the shell's foreground process is an `ssh` client, return
        {"hostname","user","port","label"} resolved with *this* host's ssh
        config (`ssh -G`), plus the ssh client's pid. Else None."""
        fg = self.foreground_process()
        if not fg or fg[1] != "ssh":
            return None
        pid, _, argv = fg
        args = [a for a in argv[1:] if a not in ("-G",)]
        rc, out, _ = self.run("ssh -G " + " ".join(shlex.quote(a) for a in args))
        if rc != 0:
            return None
        cfg = dict(line.split(None, 1) for line in out.splitlines() if " " in line)
        # first non-option arg is the destination; skip values of options that take one
        takes_value = set("bcDEeFIiJLlmOopQRSWw")
        label, it = None, iter(args)
        for a in it:
            if a.startswith("-") and len(a) == 2 and a[1] in takes_value:
                next(it, None)
            elif not a.startswith("-"):
                label = a; break
        label = (label or cfg.get("hostname", "?")).split("@")[-1]
        return {"hostname": cfg.get("hostname"), "user": cfg.get("user"),
                "port": int(cfg.get("port", 22)), "label": label, "ssh_pid": pid}

    def my_addresses(self, ssh_pid=None):
        """Names this host might appear as in `who` on a machine it ssh'd into."""
        cmd = "hostname; hostname -s 2>/dev/null; hostname -f 2>/dev/null"
        if ssh_pid:
            cmd += f"; ss -tnp 2>/dev/null | awk '/pid={ssh_pid},/{{print $4}}' | sed 's/:[0-9]*$//'"
        _, out, _ = self.run(cmd)
        return {a.strip() for a in out.splitlines() if a.strip()}

    def find_session_shell(self, from_names):
        """On this host, locate the interactive shell of the ssh session that
        came from one of `from_names` (newest wins); fall back to the newest
        session of this user. Sets and returns shell_pid."""
        script = r"""
who | awk -v u="$(id -un)" '$1==u {from=$NF; gsub(/[()]/,"",from); print $2, $3" "$4, from}' | sort -k2,3 -r
"""
        _, out, _ = self.run(script)
        sessions = [line.split(None, 2) for line in out.splitlines() if line.strip()]
        if not sessions:
            # no utmp entry (containers, some sshd configs): newest interactive shell on a pty
            _, out, _ = self.run(
                "ps -u \"$(id -un)\" -o pid=,tty=,comm= --sort=-start_time | "
                "awk '$2 ~ /^pts/ && $3 ~ /^(bash|zsh|sh|fish|dash|ksh)$/ {print $1; exit}'")
            pid = out.strip()
            self.shell_pid = int(pid) if pid.isdigit() else None
            return self.shell_pid
        match = [s for s in sessions if len(s) > 2 and s[2].split(":")[0] in from_names]
        tty = (match or sessions)[0][0]
        # the login shell is the process on that tty whose parent is sshd
        rc, out, _ = self.run(
            f"for p in $(ps -o pid= -t {shlex.quote(tty)}); do "
            f"pp=$(ps -o ppid= -p $p | tr -d ' '); c=$(ps -o comm= -p $pp 2>/dev/null); "
            f"case \"$c\" in sshd*) echo $p; break;; esac; done")
        pid = out.strip().split()[0] if out.strip() else ""
        if not pid.isdigit():
            _, out, _ = self.run(f"ps -o pid= -t {shlex.quote(tty)} | head -1")
            pid = out.strip()
        self.shell_pid = int(pid) if pid.isdigit() else None
        return self.shell_pid

    def alive(self):
        return self.transport is not None and self.transport.is_active()

    def close(self):
        if self._sftp:
            self._sftp.close()
        self._client.close()
