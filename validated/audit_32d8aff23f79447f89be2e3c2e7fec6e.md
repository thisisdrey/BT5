### Title
`git_repository`/`new_git_repository` force-enable Git's disabled `protocol.file.allow`, letting an attacker-controlled `.gitmodules` read arbitrary files/repos from the build host into the workspace - ([File: tools/build_defs/repo/git_worker.bzl])

### Summary
`git_repository`'s submodule handling in `update_submodules` unconditionally passes `-c protocol.file.allow=always` to `git submodule update --init`, re-enabling a Git transport that upstream Git disabled by default (protocol.file.allow was hardened to `user` starting with Git 2.38.1) specifically because `file://` submodule URLs let a malicious repository read files/repos from anywhere on the local filesystem the invoking user can access.

### Finding Description
When `init_submodules` or `recursive_init_submodules` is set on a `git_repository`/`new_git_repository` rule, Bazel runs: [1](#0-0) 

Both branches explicitly pass `"-c", "protocol.file.allow=always"` before invoking `submodule update --init`. The inline comment even acknowledges the purpose: "`protocol.file.allow=always` allows the submodule command clone from a local directory. It's necessary for Git 2.38.1 and associated backport versions." [1](#0-0) 

That comment describes working around a Git *security hardening*, not a bug: Git 2.38.1 changed the default of `protocol.file.allow` from `always` to `user` precisely because a `.gitmodules` file inside a fetched repository (fully attacker-controlled content, since it lives in the tree of the remote repo being cloned) can declare a submodule URL such as `file:///home/victim/.ssh` or `file:///some/other/checked-out/repo` and have Git transparently clone that arbitrary local path during automated submodule init. Bazel's `git_worker.bzl` reverses this protection unconditionally for every fetch, restoring the pre-hardening, vulnerable behavior.

The remote content that drives this — the repository fetched via `ctx.attr.remote`/`ctx.attr.commit` — is exactly the "bytes served at a dependency URL" class of input this analysis is scoped to: a hostile git host/mirror serving a pinned commit whose tree contains a crafted `.gitmodules` file. Because Bazel performs a full `git submodule update --init [--recursive]` with `protocol.file.allow=always` set, `_git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", ...)` [1](#0-0)  will happily clone whatever local path the submodule URL names, staging its contents inside the external repository directory that later feeds `BUILD` targets, `strip_prefix`, and downstream actions.

No other invariant blocks this: the checksum/lockfile machinery for `git_repository` only pins the top-level commit hash (`ctx.attr.commit`), which cryptographically fixes the tree contents including `.gitmodules`, but does nothing to constrain what submodule *URLs* that tree may reference — commit pinning is about integrity of content, not about sandboxing what the content is allowed to instruct Git to do. `_checkout_path`/`strip_prefix` containment checks in `git.bzl` only guard the checkout directory of the *main* repo, not the arbitrary local filesystem paths a `file://` submodule can reach. [2](#0-1) 

### Impact Explanation
An attacker who controls (or compromises) the content of a git-hosted dependency that a victim fetches via `git_repository`/`new_git_repository` with `init_submodules = True` or `recursive_init_submodules = True` can smuggle a `.gitmodules` entry with a `file://` URL pointing to sensitive local paths on the build machine (SSH keys, other checked-out source trees, CI secrets baked into the filesystem, etc.). Because Bazel forces `protocol.file.allow=always`, Git will clone that local path into the fetched repository tree, which is then materialized into the external repository directory and can be referenced by `BUILD` files, embedded in build outputs, or otherwise exfiltrated — a read outside the intended repository/exec root boundary.

### Likelihood Explanation
Requires the victim to use `git_repository`/`new_git_repository` with submodule initialization enabled against a remote the attacker can influence (a third-party dependency repository, a compromised upstream, or a malicious mirror serving the pinned commit) — a common and realistic configuration for consuming git-based dependencies with submodules. No credentials, sandbox escape, or privileged access are needed by the attacker; the malicious content is delivered purely as ordinary repository bytes at the pinned commit.

### Recommendation
Do not force `protocol.file.allow=always` unconditionally. Instead:
- Default to Git's hardened behavior (`protocol.file.allow=user` or unset) and only relax it when the submodule URL is verified to stay within the already-cloned repository/checkout tree (e.g., relative submodule paths), rather than globally allowing arbitrary `file://` targets.
- If broader local-clone support is genuinely required for a specific compatibility scenario, gate it behind an explicit, off-by-default rule attribute so a repo owner must opt in, rather than applying it unconditionally to every `git_repository` submodule fetch driven by untrusted upstream content.

### Proof of Concept
A `src/test/shell/bazel/starlark_git_repository_test.sh`-style integration test can demonstrate this:
1. Create an "attacker" bare git repo `victim_target` outside the workspace/output-base tree (e.g., under `$TEST_TMPDIR/outside`), containing a marker file `secret.txt`.
2. Create the "dependency" repo that the victim will `git_repository`-fetch; add a `.gitmodules` entry with `url = file://$TEST_TMPDIR/outside/victim_target` and a submodule path `stolen`, then commit it.
3. In the test WORKSPACE/MODULE.bazel, declare:
```
git_repository(
    name = "dep",
    remote = "<url of dependency repo>",
    commit = "<pinned commit>",
    init_submodules = True,
)
```
4. Run `bazel build @dep//...` (or a target that reads `external/dep/stolen/secret.txt`) and assert the file `secret.txt` from the "outside" repo is present under the fetched repository's `stolen/` directory — proving content from outside the intended dependency source was pulled in via the forced `protocol.file.allow=always` in `update_submodules`/`_execute` in `tools/build_defs/repo/git_worker.bzl`. [1](#0-0)

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

**File:** tools/build_defs/repo/git.bzl (L89-107)
```text
def _checkout_path(ctx):
    """
    Returns the path where the git repository will be checked out.

    The path returned will be the repository directory. If `add_prefix` is set,
    the additional prefix subdirectory path is appended to the repository
    directory. If the directory escapes the "root" repository, eg. an uplevel
    reference '..', the method will fail.
    """
    root = ctx.path(".")
    if ctx.attr.add_prefix:
        add_prefix_root = root.get_child(ctx.attr.add_prefix)
        if add_prefix_root != root and not str(add_prefix_root).startswith(str(root) + "/"):
            fail(
                "add_prefix '%s' escaped the base directory of '%s': '%s'" %
                (ctx.attr.add_prefix, str(root), str(add_prefix_root)),
            )
        return add_prefix_root
    return root
```
