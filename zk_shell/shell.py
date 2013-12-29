# -*- coding: utf-8 -*-
#
# a zkCli.sh clone - though not everything is supported currently
# It supports the basic ops:
#
#  python contrib/shell.py localhost:2181
#  (CONNECTED) /> ls
#  zookeeper
#  (CONNECTED) /> create foo 'bar'
#  (CONNECTED) /> get foo
#  bar
#  (CONNECTED) /> cd foo
#  (CONNECTED) /foo> create ish 'barish'
#  (CONNECTED) /foo> cd ..
#  (CONNECTED) /> ls foo
#  ish
#  (CONNECTED) /> create temp- 'temp' true true
#  (CONNECTED) /> ls
#  zookeeper foo temp-0000000001
#  (CONNECTED) /> rmr foo
#  (CONNECTED) />
#  (CONNECTED) /> tree
#  .
#  ├── zookeeper
#  │   ├── config
#  │   ├── quota


from __future__ import print_function

import logging
import os
import re
import shlex
import sys
import time

from kazoo.exceptions import NoAuthError, NoNodeError, NotEmptyError

from .acl import ACLReader
from .augumented_client import AugumentedClient
from .augumented_cmd import (
    AugumentedCmd,
    BooleanOptional,
    IntegerOptional,
    interruptible,
    ensure_params,
    Multi,
    Optional,
    Required,
)
from .copy import copy, CopyError
from .watch_manager import get_watch_manager
from .util import pretty_bytes, to_bool


class Shell(AugumentedCmd):
    def __init__(self, hosts=[], timeout=10):
        AugumentedCmd.__init__(self, ".kz-shell-history")
        self._hosts = hosts
        self._connect_timeout = timeout
        self._zk = None
        self._read_only = False
        self.connected = False

        if len(self._hosts) > 0: self._connect(self._hosts)
        if not self.connected: self.update_curdir("/")

    def connected(f):
        def wrapped(self, args):
            if not self.connected:
                print("Not connected.")
            else:
                try:
                    return f(self, args)
                except NoAuthError as ex:
                    print("Not authenticated.")
        wrapped.__doc__ = f.__doc__
        return wrapped

    def check_path_exists(f):
        def wrapped(self, params):
            path = params.path
            params.path = self.abspath(path if path not in ["", "."] else self.curdir)
            if self._zk.exists(params.path):
                return f(self, params)
            print("Path %s doesn't exist" % (path))
            return False
        wrapped.__doc__ = f.__doc__
        return wrapped

    def check_path_absent(f):
        def wrapped(self, params):
            path = params.path
            params.path = self.abspath(path if path != '' else self.curdir)
            if not self._zk.exists(params.path):
                return f(self, params)
            print("Path %s already exists" % (path))
        wrapped.__doc__ = f.__doc__
        return wrapped

    @connected
    @ensure_params(Required("scheme"), Required("credential"))
    def do_add_auth(self, params):
        """
        allows you to authenticate your session.
        example:
        add_auth digest super:s3cr3t
        """
        self._zk.add_auth(params.scheme, params.credential)

    @connected
    @ensure_params(Required("path"), Multi("acls"))
    @check_path_exists
    def do_set_acls(self, params):
        """
        sets ACLs for a given path.
        example:
        set_acls /some/path world:anyone:r digest:user:aRxISyaKnTP2+OZ9OmQLkq04bvo=:cdrwa
        set_acls /some/path world:anyone:r username_password:user:p@ass0rd:cdrwa
        """
        acls = ACLReader.extract(params.acls)
        try:
            self._zk.set_acls(params.path, acls)
        except Exception as ex:
            print("Failed to set ACLs: %s. Error: %s" % (str(acls), str(ex)))

    def complete_set_acls(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"))
    @check_path_exists
    def do_get_acls(self, params):
        """
        gets ACLs for a given path.
        example:
        get_acls /zookeeper
        [ACL(perms=31, acl_list=['ALL'], id=Id(scheme=u'world', id=u'anyone'))]
        """
        print(self._zk.get_acls(params.path)[0])

    def complete_get_acls(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Optional("path"), Optional("watch"))
    @check_path_exists
    def do_ls(self, params):
        kwargs = {"watch": self.watcher} if to_bool(params.watch) else {}
        znodes = self._zk.get_children(params.path, **kwargs)
        print(" ".join(znodes))

    def complete_ls(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @interruptible
    @ensure_params(Required("command"), Required("path"), Optional("debug"), Optional("sleep"))
    @check_path_exists
    def do_watch(self, params):
        """
        Recursively watch for all changes under a path.
        examples:
        watch start /foo/bar [debug]
        watch stop /foo/bar
        watch stats /foo/bar [repeatN] [sleepN]
        """
        wm = get_watch_manager(self._zk)
        if params.command == "start":
            wm.add(params.path, params.debug.lower() == "true")
        elif params.command == "stop":
            wm.remove(params.path)
        elif params.command == "stats":
            repeat = 1
            sleep = 1
            try:
                repeat = int(params.debug)
                sleep = int(params.sleep)
            except ValueError: pass
            if repeat == 0:
                while True:
                    wm.stats(params.path)
                    time.sleep(sleep)
            else:
                for i in xrange(0, repeat):
                    wm.stats(params.path)
                    time.sleep(sleep)
        else:
            print("watch <start|stop> <path> [verbose]")

    @ensure_params(Required("src"), Required("dst"),
                   BooleanOptional("recursive"), BooleanOptional("overwrite"),
                   BooleanOptional("async"), BooleanOptional("verbose"))
    def do_cp(self, params):
        """
        copy from/to local/remote or remote/remote paths.
        example:
        cp file://<path> zk://[user:passwd@]host/<path> <recursive> <overwrite> <async> <verbose>
        """
        try:
            copy(params.src,
                 params.dst,
                 params.recursive,
                 params.overwrite,
                 params.async,
                 params.verbose)
        except CopyError as ex:
            print(str(ex))

    @connected
    @interruptible
    @ensure_params(Optional("path"), IntegerOptional("max_depth"))
    @check_path_exists
    def do_tree(self, params):
        """
        print the tree under a given path (optionally only up to a given max depth).
        examples:
        tree
        .
        ├── zookeeper
        │   ├── config
        │   ├── quota

        tree 1
        .
        ├── zookeeper
        ├── foo
        ├── bar
        """
        print(".")
        self._zk.tree(params.path,
                      params.max_depth,
                      lambda c,l: print(u"%s├── %s" % (u"│   " * l, c)))

    def complete_tree(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Optional("path"))
    @check_path_exists
    def do_du(self, params):
        print(pretty_bytes(self._zk.du(params.path)))

    @connected
    @ensure_params(Optional("path"), Required("match"))
    @check_path_exists
    def do_find(self, params):
        """
        find znodes whose path matches a given text.
        example:
        find / foo
        /foo2
        /fooish/wayland
        /fooish/xorg
        /copy/foo
        """
        self._zk.find(params.path,
                      params.match,
                      True,
                      0,
                      lambda p: print(p))

    def complete_find(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"), Required("match"))
    @check_path_exists
    def do_ifind(self, params):
        """
        find znodes whose path matches a given text (regardless of the latter's case).
        example:
        ifind / fOO
        /foo2
        /FOOish/wayland
        /fooish/xorg
        /copy/Foo
        """
        self._zk.find(params.path,
                      params.match,
                      True,
                      re.IGNORECASE,
                      lambda p: print(p))

    def complete_ifind(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"), Required("content"), BooleanOptional("show_matches"))
    @check_path_exists
    def do_grep(self, params):
        """
        find znodes whose value matches a given text.
        example:
        grep / unbound true
        /passwd: unbound:x:992:991:Unbound DNS resolver:/etc/unbound:/sbin/nologin
        /copy/passwd: unbound:x:992:991:Unbound DNS resolver:/etc/unbound:/sbin/nologin
        """
        self._.zk.grep(params.path,
                       params.content,
                       params.show_matches,
                       flags=0,
                       callback=lambda p: print(p))

    def complete_grep(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"), Required("content"), BooleanOptional("show_matches"))
    @check_path_exists
    def do_igrep(self, params):
        """
        find znodes whose value matches a given text (case-insensite).
        example:
        igrep / UNBound true
        /passwd: unbound:x:992:991:Unbound DNS resolver:/etc/unbound:/sbin/nologin
        /copy/passwd: unbound:x:992:991:Unbound DNS resolver:/etc/unbound:/sbin/nologin
        """
        self._zk.grep(params.path,
                      params.content,
                      params.show_matches,
                      flags=re.IGNORECASE,
                      callback=lambda p: print(p))

    def complete_igrep(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"))
    @check_path_exists
    def do_cd(self, params):
        self.update_curdir(params.path)

    def complete_cd(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"), Optional("watch"))
    @check_path_exists
    def do_get(self, params):
        """
        gets the value for a given znode. a watch can be set.

        example:
        get /foo
        bar

        # sets a watch
        get /foo true

        # trigger the watch
        set /foo 'notbar'
        WatchedEvent(type='CHANGED', state='CONNECTED', path=u'/foo')
        """
        kwargs = {"watch": self.watcher} if to_bool(params.watch) else {}
        value, stat = self._zk.get(params.path, **kwargs)
        print(value.decode(encoding="utf-8"))

    def complete_get(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"), Optional("watch"))
    @check_path_exists
    def do_exists(self, params):
        """
        checks if path exists and returns the stat for the znode. a watch can be set.

        example:
        exists /foo
        ZnodeStat(czxid=101, mzxid=102, ctime=1382820644375, mtime=1382820693801, version=1, cversion=0, aversion=0, ephemeralOwner=0, dataLength=6, numChildren=0, pzxid=101)

        # sets a watch
        exists /foo true

        # trigger the watch
        rm /foo
        WatchedEvent(type='DELETED', state='CONNECTED', path=u'/foo')
        """
        kwargs = {"watch": self.watcher} if to_bool(params.watch) else {}
        stat = self._zk.exists(params.path, **kwargs)
        print(stat)

    def complete_exists(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    def watcher(self, watched_event):
        print((str(watched_event)))

    @connected
    @ensure_params(Required("path"),
                   Required("value"),
                   BooleanOptional("ephemeral"),
                   BooleanOptional("sequence"),
                   BooleanOptional("recursive"))
    @check_path_absent
    def do_create(self, params):
        """
        creates a znode in a given path. it can also be ephemeral and/or sequential. it can also be created recursively.

        example:
        create /foo 'bar'

        # create an ephemeral znode
        create /foo1 '' true

        # create an ephemeral|sequential znode
        create /foo1 '' true true

        # recursively create a path
        create /very/long/path/here '' false false true

        # check the new subtree
        tree
        .
        ├── zookeeper
        │   ├── config
        │   ├── quota
        ├── very
        │   ├── long
        │   │   ├── path
        │   │   │   ├── here
        """
        self._zk.create(params.path,
                        str.encode(params.value),
                        acl=None,
                        ephemeral=params.ephemeral,
                        sequence=params.sequence,
                        makepath=params.recursive)

    def complete_create(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"), Required("value"))
    @check_path_exists
    def do_set(self, params):
        """
        sets the value for a znode.

        example:
        set /foo 'bar'
        """
        self._zk.set(params.path, str.encode(params.value))

    def complete_set(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"))
    @check_path_exists
    def do_rm(self, params):
        try:
            self._zk.delete(params.path)
        except NotEmptyError:
            print("%s is not empty." % (params.path))

    def complete_rm(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params()
    def do_session_info(self, params):
        """
        shows information about the current session (session id, timeout, etc.)

        example:
        state=CONNECTED
        xid=4
        last_zxid=11
        timeout=10000
        server=('127.0.0.1', 2181)
        """
        print(
"""state=%s
xid=%d
last_zxid=%d
timeout=%d
server=%s""" % (self._zk.state,
                self._zk._connection._xid,
                self._zk.last_zxid,
                self._zk._session_timeout,
                self._zk._connection._socket.getpeername()))

    @ensure_params(Optional("host"))
    def do_mntr(self, params):
        host = params.host if params.host != "" else None
        try:
            print(self._zk.mntr(host))
        except AugumentedClient.CmdFailed as ex:
            print(ex)

    @ensure_params(Optional("host"))
    def do_cons(self, params):
        host = params.host if params.host != "" else None
        try:
            print(self._zk.cons(host))
        except AugumentedClient.CmdFailed as ex:
            print(ex)

    @ensure_params(Optional("host"))
    def do_dump(self, params):
        host = params.host if params.host != "" else None
        try:
            print(self._zk.dump(host))
        except AugumentedClient.CmdFailed as ex:
            print(ex)

    @connected
    @ensure_params(Required("path"))
    @check_path_exists
    def do_rmr(self, params):
        """
        recursively deletes a path.

        example:
        rmr /foo
        """
        self._zk.delete(params.path, recursive=True)

    def complete_rmr(self, cmd_param_text, full_cmd, start_idx, end_idx):
        return self._complete_path(cmd_param_text, full_cmd)

    @connected
    @ensure_params(Required("path"))
    @check_path_exists
    def do_sync(self, params):
        self._zk.sync(params.path)

    @ensure_params(Required("hosts"))
    def do_connect(self, params):
        """
        connects to a host from a list of hosts given.

        example:
        connect host1:2181,host2:2181
        """

        # TODO: we should offer autocomplete based on prev hosts.
        self._connect(params.hosts.split(","))

    @connected
    def do_disconnect(self, args):
        """
        disconnects from the currently connected host.
        """
        self._disconnect()
        self.update_curdir("/")

    @connected
    def do_pwd(self, args):
        print("%s" % (self.curdir))

    def do_EOF(self, *args):
        self._exit(True)

    def do_quit(self, *args):
        self._exit(False)

    def do_exit(self, *args):
        self._exit(False)

    def _disconnect(self):
        if self._zk: return

        try: self._zk.stop()
        except Exception: pass

    def _connect(self, hosts):
        self._disconnect()
        self._zk = AugumentedClient(",".join(hosts),
                                    read_only=self._read_only)
        try:
            self._zk.start(timeout=self._connect_timeout)
            self.connected = True
        except Exception as ex: print("Failed to connect: %s" % (ex))

        self.update_curdir("/")

    @property
    def state(self):
        return "(%s) " % (self._zk.state if self._zk else "DISCONNECTED")

    def _complete_path(self, cmd_param_text, full_cmd):
        pieces = shlex.split(full_cmd)
        cmd_param = pieces[1] if len(pieces) > 1 else cmd_param_text
        offs = len(cmd_param) - len(cmd_param_text)
        path = cmd_param[:-1] if cmd_param.endswith("/") else cmd_param

        if re.match("^\s*$", path):
            return self._zk.get_children(self.curdir)

        if self._zk.exists(path):
            children = self._zk.get_children(self.abspath(path))
            opts = list(map(lambda z: "%s/%s" % (path, z), children))
        elif "/" not in path:
            znodes = self._zk.get_children(self.curdir)
            opts = list(filter(lambda z: z.startswith(path), znodes))
        else:
            parent = os.path.dirname(path)
            child = os.path.basename(path)
            matching = list(filter(lambda z: z.startswith(child), self._zk.get_children(parent)))
            opts = list(map(lambda z: "%s/%s" % (parent, z), matching))

        return list(map(lambda x: x[offs:], opts))
