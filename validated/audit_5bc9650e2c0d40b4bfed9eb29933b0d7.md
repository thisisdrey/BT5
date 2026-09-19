### Title
`git_repository`/`new_git_repository` submodule fetching unconditionally re-enables Git's `file://` transport (`protocol.file.allow=always`), allowing an attacker-controlled repository to pull arbitrary local paths into the build - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`update_submodules()` in `tools/build_defs/repo/git_worker.bzl` runs `git -c protocol.file.allow=always submodule update --init ...` for *every* `git_repository`/`new_git_repository` invocation that sets `init_submodules`/`recursive_init_submodules`, regardless of where the submodule URLs come from. [1](#0-0) 

### Finding Description
`git_repo()` drives the whole clone-and-checkout flow: `init`, `add_origin`, `fetch`, `reset`, `clean`, then (optionally) `update_submodules`. [2](#0-1) 

The submodule URLs used by `git submodule update --init` are *not* supplied by the trusted root `MODULE.bazel`/`BUILD` file — they come entirely from the `.gitmodules` file (and `.git/config`) that is part of the checked-out content of the *remote* repository being fetched, i.e. attacker-controlled data once that repository (or any of its nested submodules) is not fully trusted by the victim.

Upstream Git deliberately defaults `protocol.file.allow` to `user` (disallowed for submodule recursion) specifically because a hostile repository's `.gitmodules` could point a submodule at a `file://` path on the local machine, causing `git submodule update` to silently "clone" arbitrary local directories into the checkout (CVE-2022-39253 class issue). Bazel's git worker hard-codes `-c protocol.file.allow=always` for both the recursive and non-recursive submodule-update code paths, re-opening exactly the protocol Git's own default is meant to close: [1](#0-0) 

There is no validation anywhere in `git_worker.bzl` (or `git.bzl`) of submodule URLs before this call — `_git_maybe_shallow` simply forwards the flag and executes `git`: [3](#0-2) 

Because the flag is applied unconditionally, an attacker who controls the content served at the `remote` a victim points `git_repository`/`new_git_repository` at (or any transitively-included submodule inside that history) can add a `.gitmodules` entry such as:
```
[submodule "x"]
    path = x
    url = file:///home/victim/.ssh
```
When the victim's build fetches this repository with `init_submodules`/`recursive_init_submodules = True`, Bazel's `update_submodules()` will happily clone that local path into the external repository directory — reading local filesystem content the attacker has no right to and materializing it inside the workspace/output_base, i.e. crossing the containment boundary that the repository-fetch machinery is supposed to enforce (only content from the declared, checksummed `remote`/`archive` should end up in the repo). This does not require MITM, a malicious peer, or machine access — the whole attack is delivered purely as bytes inside a git repository the victim's `MODULE.bazel`/`WORKSPACE` already declares as a dependency (or a nested submodule of it).

### Impact Explanation
This breaks the containment invariant that only the fetched remote's own tracked content should land inside the external repository directory. An attacker-supplied `.gitmodules` can cause Bazel to read arbitrary local paths reachable by the user running Bazel (other checked-out repositories, credential-bearing git configs, CI workspace directories, etc.) and copy that content into the build's external repo tree, where it can subsequently be exposed through generated BUILD/`exports_files` targets, or where Git's local-clone hardlink optimization can create links that alias files outside the intended checkout. This is a read/containment-boundary violation triggered purely by untrusted, attacker-published repository content.

### Likelihood Explanation
Any project using `git_repository`/`new_git_repository` with `init_submodules` or `recursive_init_submodules` (a documented, commonly used attribute) against a dependency that is not fully trusted, or that pulls in transitive submodules the victim doesn't audit, is exposed the moment that dependency's history is compromised or replaced. No special privileges, MITM, or credential access are needed — only control over the git content a build already fetches.

### Recommendation
Do not pass `protocol.file.allow=always` unconditionally. Only relax `protocol.file.allow`/`protocol.ext.allow` when explicitly required (e.g. only for the specific known Git 2.38.1 regression referenced in bazelbuild/bazel#17040, and only after validating that no submodule URL uses `file://`/`ext::`), or better, validate/reject `file://`/`ext::` submodule URLs from `.gitmodules` before invoking `git submodule update`, preserving Git's upstream `protocol.file.allow=user` default otherwise.

### Proof of Concept
Extend `src/test/shell/bazel/starlark_git_repository_test.sh` (which already exercises `test_git_repository_submodules_with_recursive_init_modules`, see lines 456-480) with a malicious submodule repo whose `.gitmodules` sets `url = file://$TEST_TMPDIR/secret_dir` instead of a normal path, then assert that after `bazel fetch @outer_planets`, files from `$TEST_TMPDIR/secret_dir` (a directory the "attacker" repo has no right to) appear inside `bazel info output_base`'s external repo directory for the submodule — demonstrating that content from an arbitrary local path was pulled in via the attacker-controlled `.gitmodules`, using the same repo/test scaffolding already present at: [4](#0-3)

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L136-150)
```text
def _update(ctx, git_repo):
    ctx.delete(git_repo.directory)

    init(ctx, git_repo)
    add_origin(ctx, git_repo, ctx.attr.remote)
    fetch(ctx, git_repo)
    reset(ctx, git_repo)
    clean(ctx, git_repo)

    if git_repo.recursive_init_submodules:
        ctx.report_progress("Updating submodules recursively")
        update_submodules(ctx, git_repo, recursive = True)
    elif git_repo.init_submodules:
        ctx.report_progress("Updating submodules")
        update_submodules(ctx, git_repo)
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

**File:** tools/build_defs/repo/git_worker.bzl (L232-239)
```text
def _git_maybe_shallow(ctx, git_repo, command, *args):
    start = [command]
    args_list = list(args)
    if git_repo.shallow:
        st = _execute(ctx, git_repo, start + [git_repo.shallow] + args_list)
        if st.return_code == 0:
            return st
    return _execute(ctx, git_repo, start + args_list)
```

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L456-480)
```shellscript
function test_git_repository_submodules_with_recursive_init_modules() {
  local outer_planets_repo_dir=$TEST_TMPDIR/repos/outer-planets

  # Create a workspace that clones the outer_planets repository.
  cat >> MODULE.bazel <<EOF
git_repository = use_repo_rule('@bazel_tools//tools/build_defs/repo:git.bzl', 'git_repository')
git_repository(
    name = "outer_planets",
    remote = "$outer_planets_repo_dir",
    tag = "1-submodule",
    recursive_init_submodules = 1,
    build_file = "//:outer_planets.BUILD",
)
EOF

  cat > BUILD <<EOF
exports_files(['outer_planets.BUILD'])
EOF
  cat > outer_planets.BUILD <<EOF
filegroup(
    name = "neptune",
    srcs = ["neptune/info"],
    visibility = ["//visibility:public"],
)

```
