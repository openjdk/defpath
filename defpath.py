# Copyright 2007, 2018, Oracle and/or its affiliates. All Rights Reserved.
# DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
#
# This code is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 only, as
# published by the Free Software Foundation.  Oracle designates this
# particular file as subject to the "Classpath" exception as provided
# by Oracle in the LICENSE file that accompanied this code.
#
# This code is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# version 2 for more details (a copy is included in the LICENSE file that
# accompanied this code).
#
# You should have received a copy of the GNU General Public License version
# 2 along with this work; if not, write to the Free Software Foundation,
# Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Please contact Oracle, 500 Oracle Parkway, Redwood Shores, CA 94065 USA
# or visit www.oracle.com if you need additional information or have any
# questions.

# Mercurial extension to examine and manipulate default path settings

# To enable in your ~/.hgrc:
#
#     [extensions]
#     defpath = /path/to/defpath.py
#
# There's one configuration setting:
#
#     [defpath]
#     username = foo    Sets username for ssh push URLs to "foo" (-u option)


import os, os.path, sys, getopt, socket, re
import ConfigParser, StringIO
import shutil
from urlparse import urlparse, urlunparse, urljoin
from urllib import urlopen
try:
    # Python 3.0 and higher
    import html.parser as HTMLParser
except ImportError:
    from HTMLParser import HTMLParser
    pass
from mercurial import cmdutil, commands, error, hg, util
try:
    # Mercurial 4.3 and higher
    from mercurial import registrar
except ImportError:
    registrar = {}
    pass

# Abort() was moved/copied from util to error in hg 1.3 and was removed from
# util in 4.6.
error_Abort = None
if hasattr(error, 'Abort'):
    error_Abort = error.Abort
else:
    error_Abort = util.Abort

# Config files

def cfg_dump(out, t, c):
    o = StringIO.StringIO()
    c.write(o)
    o.seek(0)
    out.write(t + " config:\n")
    for ln in o.readlines():
        ln = ln.strip()
        if len(ln) == 0:
            continue
        out.write("| " + ln + "\n")

def cfg_get(c, s, k):
    if not c.has_section(s) or not c.has_option(s, k):
        return None
    return c.get(s, k)

def cfg_put(c, s, k, v):
    if not c.has_section(s):
        c.add_section(s)
    c.set(s, k, v)

def load(dir):
    assert os.path.isdir(dir)
    assert os.path.isdir(dir + "/.hg")
    cfg = ConfigParser.RawConfigParser()
    hgrc = dir + "/.hg/hgrc"
    if os.path.isfile(hgrc):
        cfg.read(hgrc)
    return cfg

def store(cfg, dir):
    hgrc = dir + "/.hg/hgrc"
    hgrc_old = hgrc + ".old"
    if os.path.isfile(hgrc_old):
        os.remove(hgrc_old)
    if os.path.exists(hgrc):
        shutil.copy2(hgrc, hgrc_old)
    hf = open(hgrc, "w")
    try:
        cfg.write(hf)
    finally:
        hf.close()


# Repository URL resolution

TIMEOUT = 10

def get_repo_root(ui, url):
    u = urlparse(url, None, False)
    us = uscheme(u)
    if not us or us == "file":
        # Local repository
        return url

    class Scanner(HTMLParser):

        in_html = False
        in_head = False
        done = False
        path = None

        def handle_starttag(self, tag, attrs):
            if tag == "html":
                self.in_html = True
            elif self.in_html and tag == "head":
                self.in_head = True
            elif self.in_head and tag == "link":
                am = { }
                for k, v in attrs:
                    am[k] = v

                # This is fragile!
                if am.has_key("type") and am["type"] == "application/rss+xml":
                    if am.has_key("rel") and am["rel"] == "alternate":
                        if am.has_key("href"):
                            self.path = am["href"].replace("/rss-log", "")
                            self.done = True

        def handle_endtag(self, tag):
            if self.in_head and tag == "head":
                self.in_head = False
                self.done = True

    s = Scanner()
    f = None
    try:                # These trys can be merged in Python 2.5 
        try:
            oto = socket.getdefaulttimeout()
            socket.setdefaulttimeout(TIMEOUT)
            f = urlopen(url)
            while not s.done:
                ln = f.readline()
                if ln == "":
                    break
                s.feed(ln)
        except IOError, e:
            ui.debug("%s: %s\n" % (url, e))
    finally:
        if f:
            f.close()
        socket.setdefaulttimeout(oto)
    if not s.path:
        return None
    u = urlparse(url, "http", False)
    nurl = urlunparse(u[0:2] + (s.path,) + u[3:])
    return nurl

def probe_repo(ui, url):
    rr = get_repo_root(ui, url)
    return rr and rr == url

# Workarounds for lack of URL attributes in Python < 2.5
def uscheme(url): return url[0]
def unetloc(url): return url[1]
def upath(url): return url[2]
def uhostname(url):
    return re.sub(":.*$", "", re.sub("^.*@", "", unetloc(url)))

def find_repo(ui, url, secondary):
    rr = get_repo_root(ui, url)
    if probe_repo(ui, url):
        return url
    if not secondary:
        raise error_Abort("%s: Repository not found" % url)
    u = urlparse(url, "http", False)
    url2 = urljoin(secondary, upath(u))
    if probe_repo(ui, url2):
        return url2
    raise error_Abort("%s: Repository not found\n       %s: Repository not found either"
                     % (url, url2))
    return None

def new_push_url(ui, url, gated, user):
    u = urlparse(url, None, False)
    us = uscheme(u)
    if not us or us == "file":
        # Local repository
        return url
    h = unetloc(u)
    if not user:
        user = ui.config("defpath", "username")
    if user:
        h = user + "@" + uhostname(u)
    p = upath(u)
    if gated:
        ps = p.split("/")[1:]
        g = "-gate"
        if ps[1].isupper():
            g = "-GATE"
        p = "/" + ps[0] + "/" + ps[1] + g
        if len(ps) > 2:
            p += "/" + "/".join(ps[2:])
    nu = ("ssh",) + (h, p, None, None, None)
    return urlunparse(nu)


# Display and updating

def show(ui, repo, c):
    ui.write("%s: \n" % repo)
    ui.write("         default = %s\n" % cfg_get(c, "paths", "default"))
    ui.write("    default-push = %s\n" % cfg_get(c, "paths", "default-push"))

# List of updates to apply once all updates have been computed
todo = [ ]

def go(ui, repo, root, peer, peer_push, gated, user, dry_run, secondary,
       default, **opts):
    ui.debug("go: repo=%s, root=%s, peer=%s, peer_push=%s, gated=%s, user=%s, dry_run=%s, secondary=%s\n"
             % (repo, root, peer, peer_push, gated, user, dry_run, secondary))
    verbose = ui.verbose or dry_run
    if root:
        assert repo.startswith(root)
        subtree = repo[len(root):]
    else:
        subtree = ""
    c = load(repo)
    if ui.debugflag:
        cfg_dump(ui, repo + " old", c)
    pull = cfg_get(c, "paths", "default")
    push = cfg_get(c, "paths", "default-push")
    if default:
        if peer or peer_push:
            raise error_Abort("Peers cannot be specified together with -d flag")
        peer = pull
    elif peer:
        peer = peer + subtree
    if peer:
        peer = peer.rstrip("/")
    if not peer and not peer_push:
        show(ui, repo, c)
        return
    new_pull = find_repo(ui, peer, secondary)
    if not new_pull:
        return
    new_push = None
    if peer_push:
        new_push = peer_push.rstrip("/")
    else:
        new_push = new_push_url(ui, new_pull, gated, user)
    if pull == new_pull and push == new_push:
        if verbose:
            show(ui, repo, c)
            ui.write("(no change)\n")
        return
    cfg_put(c, "paths", "default", new_pull)
    cfg_put(c, "paths", "default-push", new_push)
    if verbose:
        show(ui, repo, c)
    if ui.debugflag:
        cfg_dump(ui, repo + " new", c)
    if not dry_run:
        todo.append(lambda: store(c, repo))

def finish():
    for f in todo:
        f()

def walk_forest(repo):
    for dir, subdirs, files in os.walk(repo.root):
        if ".hg" in subdirs:
            subdirs.remove(".hg")
            yield dir

def walk_self(repo):
    return [repo.root]


# Hg extension

def defpath(ui, repo, peer, peer_push, walker, opts):
    ui.debug("defpath repo=%s peer=%s push=%s walker=%s opts=%s\n"
             % (repo.path, peer, peer_push, walker, opts))
    root = repo.root
    try:
        for d in walker(repo):
            go(ui, d, root=root, peer=peer, peer_push=peer_push, **opts)
        finish()
    except error_Abort, x:
        ui.write("abort: %s\n" % x)
        ui.write("No hgrc files updated\n")
        return -1

# From Mercurial 1.9, the preferred way to define commands is using the @command
# decorator. If this isn't available, fallback on a simple local implementation
# that just adds the data to the cmdtable.
cmdtable = {}
if hasattr(registrar, 'command'):
    command = registrar.command(cmdtable)
elif hasattr(cmdutil, 'command'):
    command = cmdutil.command(cmdtable)
else:
    def command(name, options, synopsis):
        def decorator(func):
            cmdtable[name] = func, list(options), synopsis
            return func
        return decorator

opts = [("d", "default", False, "use current default path to compute push path"),
        ("g", "gated", False, "create gated push URL"),
        ("u", "user", "", "username for push URL"),
        ("s", "secondary", "", "secondary peer repository base URL")
        ] + commands.dryrunopts

help = "[-d] [-g] [-u NAME] [-s SECONDARY] [PEER [PEER-PUSH]]"

@command("defpath", opts, "hg defpath " + help)
def cmd_defpath(ui, repo, peer=None, peer_push=None, **opts):
    """examine and manipulate default path settings

    When invoked without arguments and without the -d option, the defpath
    command displays the default and default-push paths of the current
    repository, or the specified repository if the -R option is used.

    If the PEER argument is given then the repository's default path is
    set to PEER if it can be verified that PEER names a valid repository.

    If the PEER-PUSH argument is given then the repository's default push
    path is set to PEER-PUSH.  The PEER-PUSH URL is not validated, since
    most often it will be an SSH URL.

    If the PEER argument is given without also specifying PEER-PUSH then
    a push URL is computed as follows: If the PEER URL has the scheme
    "http" then it will be replaced with "ssh", and possibly include an
    explicit username if the -u option is used; otherwise, the push URL
    will be the same as the PEER URL.

    The -g option causes the push URL to add the suffix"-gate" (or
    "-GATE", as appropriate) to the name of the target repository.

    The -d option takes the repository's current default path as if it
    had been specified as the PEER argument.  It is most often useful
    immediately after a clone operation.

    The -s option may be used to specify a secondary peer base URL that
    contains repositories not available on the primary peer server.  If
    an HTTP peer URL cannot be validated then its scheme and authority
    components will be replaced by those of the secondary peer URL, and
    if the resulting URL can be validated then it will be used as the
    peer.

    The -u option may be used to specify a username other than that of
    the current process.  It is only useful when your ssh login name
    on the peer push server differs from your local login name.
    """
    return defpath(ui, repo, peer, peer_push, walk_self, opts)

@command("fdefpath", list(opts), "hg fdefpath " + help)
def cmd_fdefpath(ui, repo, peer=None, peer_push=None, **opts):
    """examine and manipulate default path settings for a forest

    The forest equivalent of the defpath command.

    When invoked without arguments and without the -d option, the defpath
    command displays the default and default-push paths of each repository
    in the current forest, or in the specified forest if the -R option is
    used.

    If the PEER argument is given then the default path of each repository
    in the forest is set to PEER, or to the appropriate child of PEER, if
    it can be verified that PEER (or the appropriate child) names a valid
    repository.

    If the PEER-PUSH argument is given then the default push path of each
    repository in the forest is set to PEER-PUSH, or to the appropriate
    child of PEER-PUSH.  The PEER-PUSH URL is not validated, since most
    often it will be an SSH URL.

    If the PEER argument is given without also specifying PEER-PUSH then
    a push URL is computed as follows: If the PEER URL has the scheme
    "http" then it will be replaced with "ssh", and possibly include an
    explicit username if the -u option is used; otherwise, the push URL
    will be the same as the PEER URL.

    The -g option causes the push URL to add the suffix"-gate" (or
    "-GATE", as appropriate) to the name of the target forest.

    The -d option takes each repository's current default path as if it
    had been specified as the PEER argument.  It is most often useful
    immediately after an fclone operation.

    The -s option may be used to specify a secondary peer base URL that
    contains repositories not available on the primary peer server.  If
    an HTTP peer URL cannot be validated then its scheme and authority
    components will be replaced by those of the secondary peer URL, and
    if the resulting URL can be validated then it will be used as the
    peer.

    The -u option may be used to specify a username other than that of
    the current process.  It is only useful when your ssh login name
    on the peer push server differs from your local login name.

    """
    return defpath(ui, repo, peer, peer_push, walk_forest, opts)
