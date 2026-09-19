### Title
`git_repository`/`new_git_repository` submodule handling force-enables `protocol.file.allow=always`, letting attacker-controlled `.gitmodules` pull arbitrary local host paths into the build - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`git_worker.bzl`'s `update_submodules()` always invokes `git submodule update --init [--recursive] --checkout --force` with the flag `-c protocol.file.allow=always`, unconditionally re-enabling Git's `file://` transport for submodule fetches, for every `git_repository`/`new_git_repository` invocation that sets `init_submodules = True`.

### Finding Description
`git_repo()` clones the attacker-reachable `remote` and checks out the pinned `commit`/`tag`/`branch` [1](#0-0) . If `init_submodules` (or `recursive_init_submodules`) is set, Bazel then runs:

```
git -c protocol.file.allow=always submodule update --init [--recursive] --checkout --force
``` [2](#0-1) 

The `.gitmodules` file that drives this command is *content of the fetched repository itself*, i.e. fully attacker-controlled: an outsider who controls the target of `remote` (a public fork, an untrusted branch that CI builds, or a dependency the victim vendors via `git_repository`) chooses the exact tree checked out at the pinned `commit`, including its `.gitmodules`. Pinning `commit` does not help here, because the attacker is the author of that very commit and can put anything they want into `.gitmodules`, including a submodule `url` using the `file://` scheme that targets an arbitrary path on the build host's filesystem.

Upstream Git changed the default of `protocol.file.allow` from `always` to `user` specifically to stop this class of attack (GHSA-vw2c-27hg-numc / CVE-2022-39253): with the safe default, an automated/recursive submodule update refuses to dereference `file://` URLs, so a malicious repository cannot make Git pull in and check out an unrelated local directory of the host as if it were a legitimate remote. Bazel's `git_worker.bzl` explicitly overrides this safe default back to `always` for every submodule update (comment cites bazelbuild/bazel#17040, a local-testing convenience issue), removing the protection Git added for exactly this untrusted-content scenario.

This mirrors the analog bug class in the report: a low-level knob (`git.checkoutbundle=true` in BuildKit / `protocol.file.allow=always` in Bazel) is force-enabled to support a legitimate use case, and that knob is what lets attacker-supplied Git content (a bundle / a `.gitmodules` entry) steer a subsequent Git invocation into unintended host-level behavior.

### Impact Explanation
When triggered, `git submodule update --init` treats an arbitrary local filesystem path chosen by the untrusted repository as a Git remote and clones/copies its content into the externally-fetched repository under the workspace's external-repo directory (outside the intended repository content boundary). This is a read of host filesystem content that was never part of the declared `remote`/`commit`, i.e. content escapes the intended repository/checkout boundary and lands inside the build tree, where it becomes reachable by subsequent build actions defined in that same (attacker-controlled) repository — enabling disclosure of local files/directories at attacker-guessable paths on the build machine.

### Likelihood Explanation
Requires only that a victim configure `git_repository`/`new_git_repository` with `init_submodules = True` (a common, legitimate option) pointing at a `remote` an attacker can influence (a fork, a PR branch built by CI, or any dependency source not fully trusted at the content level). No MITM, no access to the victim's credentials, output base, or trusted root-repo Starlark is required — the attacker only needs to publish Git content.

### Recommendation
Do not unconditionally force `protocol.file.allow=always`. Restrict it to the narrow local-testing scenario that bazelbuild/bazel#17040 addresses (e.g., only when the top-level `remote` itself is a local `file://` path), or otherwise validate/restrict submodule URLs (reject `file://`/local paths) before running `git submodule update --init` against content fetched from a non-local, untrusted `remote`.

### Proof of Concept
1. Create a "victim-local" sensitive directory on the build host that is itself a git repository (or reuse any pre-existing local git checkout reachable by absolute path), e.g. `/tmp/sensitive-repo`.
2. Create a malicious repository `evil.git` with a `.gitmodules` entry:
```
[submodule "leak"]
    path = leak
    url = file:///tmp/sensitive-repo
```
and commit it.
3. In a Bazel workspace, add:
```
git_repository(
    name = "evil",
    remote = "<url-to-evil.git>",
    commit = "<pinned-commit>",
    init_submodules = True,
)
```
4. Run `bazel build @evil//...` and observe (as in `src/test/shell/bazel/starlark_git_repository_test.sh`'s existing style of git-repository integration tests) that Bazel's invocation of `git -c protocol.file.allow=always submodule update --init --checkout --force` [3](#0-2)  successfully clones `/tmp/sensitive-repo` into `external/+git_repository+evil/leak`, despite the victim never referencing that local path anywhere in their own build configuration — demonstrating that the pinned `commit` did not stop attacker-controlled `.gitmodules` content from directing Git to read from an arbitrary local filesystem path.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L41-90)
```text
def git_repo(ctx, directory):
    """ Fetches data from git repository and checks out file tree.

    Called by git_repository rule.

    Args:
        ctx: Context of the calling rules, for reading the attributes.
        Please refer to the git_repository rule for the description.
        directory: Directory where to check out the file tree.
    Returns:
        The struct with the following fields:
        commit: Actual HEAD commit of the checked out data.
        shallow_since: Actual date and time of the HEAD commit of the checked out data.
    """
    if ctx.attr.shallow_since:
        if ctx.attr.tag:
            fail("shallow_since not allowed if a tag is specified; --depth=1 will be used for tags")
        if ctx.attr.branch:
            fail("shallow_since not allowed if a branch is specified; --depth=1 will be used for branches")

    # Use shallow-since if given
    if ctx.attr.shallow_since:
        shallow = "--shallow-since=%s" % ctx.attr.shallow_since
    else:
        shallow = "--depth=1"

    reset_ref = ""
    fetch_ref = ""
    if ctx.attr.commit:
        reset_ref = ctx.attr.commit
        fetch_ref = ctx.attr.commit
    elif ctx.attr.tag:
        reset_ref = "tags/" + ctx.attr.tag
        fetch_ref = "tags/" + ctx.attr.tag + ":tags/" + ctx.attr.tag
    elif ctx.attr.branch:
        reset_ref = "origin/" + ctx.attr.branch
        fetch_ref = ctx.attr.branch + ":origin/" + ctx.attr.branch
    else:
        reset_ref = "origin/HEAD"
        fetch_ref = "HEAD:refs/remotes/origin/HEAD"

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
