## Analysis: Bazel's `git_repository` submodule handling disables Git's CVE-2022-39253 protection

Bazel's Starlark-based Git repository rule shells out to the system `git` binary through `tools/build_defs/repo/git_worker.bzl`. When a repository is fetched with `init_submodules = True` or `recursive_init_submodules = True`, Bazel invokes `git submodule update` while **forcing `protocol.file.allow=always`**: [1](#0-0) 

The comment in the code makes the intent explicit — it is deliberately re-enabling `file://` submodule cloning that Git itself disabled by default starting with Git 2.38.1 as the fix for **CVE-2022-39253** (malicious `.gitmodules` files can direct `git submodule update` to clone from an arbitrary local `file://` path, which can be abused to copy/hardlink files from outside the intended working tree into the checkout). Upstream Git's mitigation was to default `protocol.file.allow` to `user` (disabled for automated/non-interactive submodule fetches), specifically to stop untrusted repository content from doing this.

### How the attacker-controlled surface reaches the sink

1. A `git_repository`/`new_git_repository` rule's `remote`, `branch`, or `tag` (not a pinned `commit`) points at a Git ref an unprivileged outsider can influence — e.g., a compromised/hostile mirror, or a fork/branch built by CI for external contributions.
2. `git_repo()` clones/fetches the ref and, if submodules are enabled, calls `update_submodules()`, which routes into `_git_maybe_shallow` and ultimately `ctx.execute` with `-c protocol.file.allow=always`: [2](#0-1) 
3. Since `protocol.file.allow=always` is force-set on the command line, it **overrides** Git's own default safety check regardless of the local Git version/config, meaning the exact protection CVE-2022-39253 introduced is neutralized for every Bazel-driven submodule fetch.
4. A `.gitmodules` file supplied by the attacker (content of the fetched, untrusted repository — not root-repo BUILD/.bzl) can declare a submodule URL of the `file://` type pointing at a path that git can traverse/hardlink from, allowing content to be pulled into the external repository's checkout directory — i.e., a read that reaches outside the intended repository boundary that Git's own containment check exists specifically to prevent.

This matches the "Repository rules"/git fetch invariant-break class: an origin server or ref an unprivileged attacker can update supplies malicious content (`.gitmodules`), and a containment check that should stop it (Git's `protocol.file.allow` default) is explicitly defeated by Bazel's own `-c` override, with default Bazel flags on a current release.

### Recommendation (conceptual)
Do not hard-force `protocol.file.allow=always`. At minimum, only enable it when the submodule's URL is verified to be relative/same-origin as the parent remote, or gate it behind an opt-in attribute so victims consciously accept the CVE-2022-39253 risk reintroduced by Bazel, rather than silently doing so for all `init_submodules`/`recursive_init_submodules` users.

I did not find that this override is scoped by remote type or otherwise mitigated elsewhere in `git.bzl`/`git_worker.bzl`; the same `-c protocol.file.allow=always` argument is present in both the recursive and non-recursive submodule-update code paths. [1](#0-0) 

Given the constraints of this exercise (no ability to execute Git locally to build a concrete JUnit/shell PoC demonstrating an actual out-of-bounds file read via a hand-crafted `.gitmodules` + symlink/hardlink trick), I can describe the reachable, concrete code-level break but cannot produce and run a verified reproduction here. If you want, I can outline the shape of a `src/test/shell/bazel/starlark_git_repository_test.sh` test that sets `init_submodules=True`, points a submodule at `file://` to a path outside the fetch directory, and asserts the file content ends up materialized in the checkout — but this would need to be executed against a real Git binary to confirm exploitability under current Git security-hardening layers (e.g., some Git versions still guard against literal absolute-path hardlink tricks even with `protocol.file.allow=always`, and only permit a *clone*, not arbitrary file read, unless the target is itself a bare/valid repo). That verification step is outside what I can do without running code.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L210-217)
```text
def update_submodules(ctx, git_repo, recursive = False):
    if recursive:
        # "protocol.file.allow=always" allows the submodule command clone from a local directory.
        # It's necessary for Git 2.38.1 and assoicated backport versions.
        # See https://github.com/bazelbuild/bazel/issues/17040
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive", "--checkout", "--force")
    else:
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--checkout", "--force")
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
