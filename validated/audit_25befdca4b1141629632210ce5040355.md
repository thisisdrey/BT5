### Title
Command/Argument Injection via Attacker-Controlled `remote` URL in `git_repository` (git transport helpers) - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`git_repository`/`new_git_repository` accept a `remote` attribute that is passed, unsanitized, to the system `git` binary as the value of `git remote add origin <remote>` and is later dereferenced by `git fetch origin`. Because Bazel neither validates the `remote` string nor restricts Git's transport helpers (no `GIT_ALLOW_PROTOCOL`/`protocol.*.allow` hardening, no rejection of leading `-` or `ext::`/`fd::`-style URLs), an attacker who controls the `remote` value reaching this code path can make the invoked `git` binary execute an arbitrary shell command or interpret the value as command-line options rather than a URL. This is the same bug class as CVE-2022-21187 (libvcs/vcspull passing an unsanitized URL into `hg clone`, permitting argument injection), applied to Bazel's own `git` invocation.

### Finding Description
`git_repo()` in [1](#0-0)  stores the raw `ctx.attr.remote` string in `git_repo.remote` without any validation. `_update()` then calls: [2](#0-1) 

`add_origin()` passes this untouched string directly as an argv element to `git remote add origin <remote>`: [3](#0-2) 

and `fetch()`/`_git_maybe_shallow()` subsequently invoke `git fetch origin ...`, which causes native `git` to dereference the stored `origin` URL and open whatever transport it names: [4](#0-3) [5](#0-4) 

Git itself natively supports the `ext::<command>` remote-helper transport, under which `git fetch`/`git remote add` + `git fetch` will execute `<command>` via the shell as part of establishing the "connection" — this is a documented Git feature, not a Bazel bug in Git, but Bazel does nothing to disallow it (no `-c protocol.ext.allow=never`, no `GIT_ALLOW_PROTOCOL` allowlist) before shelling out. Additionally, because `remote` is a bare, unvalidated string, a value beginning with `-` (e.g. `--upload-pack=...`) can be interpreted by `git remote add`/`git fetch` as an option rather than a positional URL, enabling argument injection analogous to the Mercurial `hg clone <url>` issue in the referenced advisory.

The `remote` attribute is required and typed as a plain string with no scheme/character restriction: [6](#0-5) 

`git_repository` is a normal repo rule invoked wherever a `MODULE.bazel` (including a dependency module's module extension, resolved from a registry) declares it; the `remote` value can originate from data an attacker fully controls — e.g., a module version an attacker publishes to a registry, or values threaded through an extension's tag class that get forwarded verbatim into `git_repository(remote = ...)`. No sha256/lockfile/integrity check applies to the `remote` string itself (only `commit`/`shallow_since` feed the lockfile's reproducibility metadata); there is no containment or transport allowlist that would stop this value from reaching the raw `git` invocation.

### Impact Explanation
If reachable with an attacker-supplied `remote` value, this results in arbitrary command execution in the context of the Bazel build (same trust boundary as the fetching phase, running with the invoking user's privileges, capable of reading/writing anywhere that user can, and exfiltrating credentials such as `.netrc`/SSH keys). This satisfies the "credential exfiltration to attacker-controlled host" / "arbitrary command execution" bar for the analog class.

### Likelihood Explanation
Requires the attacker's malicious `remote` string to be threaded into a `git_repository`/`new_git_repository` invocation that a victim's build actually executes — e.g. via a module the attacker publishes to a Bazel registry that is consumed (directly or transitively) by the victim's `MODULE.bazel`, or via any extension that forwards attacker-influenced data into the `remote` attribute without validation. This is a realistic supply-chain vector consistent with the "content a victim's build consumes" attacker model (no need for root-repo trust or local machine access), making likelihood moderate-to-high in registry/module-extension-heavy dependency graphs.

### Recommendation
- Validate `ctx.attr.remote` in `git.bzl`/`git_worker.bzl`: reject values starting with `-`, and restrict to an allowlist of safe schemes (`https://`, `http://`, `ssh://`, `git://`, local file paths), explicitly rejecting `ext::`, `fd::`, and other Git remote-helper pseudo-URLs.
- Pass `-c protocol.ext.allow=never -c protocol.fd.allow=never` (or set `GIT_ALLOW_PROTOCOL` to an explicit allowlist) on every `git` invocation in `_execute()` so that even if a bad URL slips through, Git itself refuses the dangerous transport helpers.
- Ensure argv-separator conventions (`--`) are used consistently before `remote` wherever it is passed to `git` subcommands, to eliminate any option/argument-injection interpretation.

### Proof of Concept
A `src/test/shell/bazel/starlark_git_repository_test.sh`-style test can demonstrate the issue:
```
git_repository(
    name = "pwn",
    remote = "ext::sh -c 'touch /tmp/pwned'",
    branch = "main",
)
```
Running `bazel build @pwn//:all` invokes `git_worker.bzl`'s `add_origin`/`fetch`, causing the system `git` binary to execute `sh -c 'touch /tmp/pwned'` via the `ext::` transport when establishing the "remote" connection, producing `/tmp/pwned` — demonstrating arbitrary command execution driven purely by the value of the `remote` string, with no containment or protocol restriction in [7](#0-6)  to prevent it.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L82-90)
```text
    git_repo = _GitRepoInfo(
        directory = ctx.path(directory),
        shallow = shallow,
        reset_ref = reset_ref,
        fetch_ref = fetch_ref,
        remote = str(ctx.attr.remote),
        init_submodules = ctx.attr.init_submodules,
        recursive_init_submodules = ctx.attr.recursive_init_submodules,
    )
```

**File:** tools/build_defs/repo/git_worker.bzl (L136-143)
```text
def _update(ctx, git_repo):
    ctx.delete(git_repo.directory)

    init(ctx, git_repo)
    add_origin(ctx, git_repo, ctx.attr.remote)
    fetch(ctx, git_repo)
    reset(ctx, git_repo)
    clean(ctx, git_repo)
```

**File:** tools/build_defs/repo/git_worker.bzl (L152-202)
```text
def init(ctx, git_repo):
    cl = ["git", "init", str(git_repo.directory)]
    st = ctx.execute(cl, environment = ctx.os.environ | _GIT_LOCAL_ENV_VARS)
    if st.return_code != 0:
        _error(ctx.name, cl, st.stderr)

def add_origin(ctx, git_repo, remote):
    _git(ctx, git_repo, "remote", "add", "origin", remote)

def fetch(ctx, git_repo):
    args = ["fetch", "origin"]

    sparse_checkout_patterns_or_file = \
        getattr(ctx.attr, "sparse_checkout_patterns", None) or \
        getattr(ctx.attr, "sparse_checkout_file", None)
    if sparse_checkout_patterns_or_file:
        if _git_sparse_checkout_config(ctx, git_repo):
            # Use filter to disable downloading file contents until we set the `sparse-checkout` patterns.
            args.append("--filter=blob:none")
        else:
            print("WARNING: Sparse checkout is not supported. Doing a full checkout.")
            sparse_checkout_patterns_or_file = None

    args.extend(["--", git_repo.fetch_ref])
    st = _git_maybe_shallow(ctx, git_repo, *args)

    if sparse_checkout_patterns_or_file:
        _git_sparse_checkout(ctx, git_repo, sparse_checkout_patterns_or_file)

    if st.return_code == 0:
        return
    if ctx.attr.commit:
        # Perhaps uploadpack.allowReachableSHA1InWant or similar is not enabled on the server;
        # fall back to fetching all branches, tags, and history.
        # The semantics of --tags flag of git-fetch have changed in Git 1.9, from 1.9 it means
        # "everything that is already specified and all tags"; before 1.9, it used to mean
        # "ignore what is specified and fetch all tags".
        # The arguments below work correctly for both before 1.9 and after 1.9,
        # as we directly specify the list of references to fetch.
        _report_progress(ctx, git_repo, shallow_failed = True)
        _git(
            ctx,
            git_repo,
            "fetch",
            "origin",
            "--",
            "refs/heads/*:refs/remotes/origin/*",
            "refs/tags/*:refs/tags/*",
        )
    else:
        _error(ctx.name, ["git"] + args, st.stderr)
```

**File:** tools/build_defs/repo/git_worker.bzl (L311-319)
```text
def _execute(ctx, git_repo, args):
    # "core.fsmonitor=false" disables git from spawning a file system monitor which can cause hangs when cloning a lot.
    # See https://github.com/bazelbuild/bazel/issues/21438
    start = ["git", "-c", "core.fsmonitor=false"]
    return ctx.execute(
        start + args,
        environment = ctx.os.environ | _GIT_LOCAL_ENV_VARS,
        working_directory = str(git_repo.directory),
    )
```

**File:** tools/build_defs/repo/git.bzl (L119-123)
```text
_common_attrs = {
    "remote": attr.string(
        mandatory = True,
        doc = "The URI of the remote Git repository",
    ),
```
