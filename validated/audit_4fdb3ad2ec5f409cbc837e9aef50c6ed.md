### Title
Unchecked exit status of `git submodule update` allows a failed/incomplete submodule checkout to be silently treated as success - (File: tools/build_defs/repo/git_worker.bzl)

### Summary
`git_worker.bzl`'s `update_submodules()` invokes `git submodule update --init [--recursive] --checkout --force` via `_git_maybe_shallow()` but never inspects the returned `exec_result.return_code`, unlike every other git invocation in the same file (`_git`, `init`, `_git_version`, `_git_sparse_checkout_config`), which explicitly check `st.return_code != 0` and call `_error()` to fail the build.

### Finding Description
`git_repository`/`new_git_repository` call `_update()`, which conditionally invokes submodule initialization: [1](#0-0) 

`update_submodules()` runs the actual submodule command but discards the `exec_result`: [2](#0-1) 

Contrast this with `_git()`, `init()`, and `_git_sparse_checkout_config()`, all of which check the return code and call `_error()` on failure: [3](#0-2) [4](#0-3) 

Because `git_repository` is a repository rule whose `remote` (and any `.gitmodules`-declared submodule URLs) is attacker-influenced content when a victim's build fetches from an untrusted/malicious git host, a hostile server can make the submodule fetch/checkout step fail (e.g., unreachable submodule commit, corrupted pack, protocol/permission errors, disconnected transfer) while the top-level clone/reset/clean steps succeed. Since `update_submodules()`'s result is never checked in `_update()`, and `_git_maybe_shallow()`'s result is discarded inside `update_submodules()` itself, the repository rule silently returns and reports success (`repo_metadata(reproducible = True)` in `git.bzl`) even though the submodule directories are left empty, partially checked out, or stale.

### Impact Explanation
A malicious/compromised git remote (fully within the unprivileged-attacker-serves-content threat model — no MITM, no local access) can cause the submodule stage to fail without the build failing. Downstream Bazel targets consuming files from within the submodule paths will silently build against missing/incomplete/stale unverified content rather than the intended commit, defeating the reproducibility/integrity guarantee that `git_repository` is supposed to provide (analogous to using an unverified/partial ERC20 transfer as if it fully succeeded).

### Likelihood Explanation
Any repository using `git_repository`/`new_git_repository` with `init_submodules`/`recursive_init_submodules` against a remote not fully trusted or controlled (mirrors, third-party forks, forks pinned by tag/branch rather than commit) is exposed. Triggering it only requires the attacker-controlled git server to fail (or partially fail) the submodule fetch/checkout, which is straightforward to arrange (e.g., serve a submodule pointing at a nonexistent/removed ref, or terminate the connection mid-transfer for that step).

### Recommendation
Capture the `exec_result` returned by `_git_maybe_shallow()` inside `update_submodules()` and check `st.return_code != 0`, calling `_error()` (matching the pattern used by `_git()`, `init()`, and `_git_sparse_checkout_config()`) so a failed or incomplete submodule checkout aborts the repository fetch instead of being silently accepted.

### Proof of Concept
1. Set up a local git remote whose `.gitmodules` references a submodule URL/commit that is reachable at clone time but made to fail specifically during `git submodule update --init` (e.g., point the submodule remote at a path that is deleted/unreachable only for the submodule fetch, or emulate a hostile server that resets the connection when the submodule pack is requested).
2. Define:
```
git_repository = use_repo_rule('@bazel_tools//tools/build_defs/repo:git.bzl', 'git_repository')
git_repository(
    name = "outer",
    remote = "<attacker-controlled or victim-fetched remote>",
    commit = "<pinned commit>",
    init_submodules = True,
)
```
3. Run `bazel build @outer//:all`. Expect: build succeeds and reports the repository as fetched, but the submodule directory under `external/+git_repository+outer/<submodule>` is empty/incomplete because `git submodule update` failed silently — no `_error()` was raised, unlike a failure in `git fetch`, `git reset`, or `git clean`, which would properly abort the build (as covered by existing `src/test/shell/bazel/starlark_git_repository_test.sh:test_git_repository_submodules`, which only exercises the success path and does not assert failure propagation for submodule update).

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L145-150)
```text
    if git_repo.recursive_init_submodules:
        ctx.report_progress("Updating submodules recursively")
        update_submodules(ctx, git_repo, recursive = True)
    elif git_repo.init_submodules:
        ctx.report_progress("Updating submodules")
        update_submodules(ctx, git_repo)
```

**File:** tools/build_defs/repo/git_worker.bzl (L152-156)
```text
def init(ctx, git_repo):
    cl = ["git", "init", str(git_repo.directory)]
    st = ctx.execute(cl, environment = ctx.os.environ | _GIT_LOCAL_ENV_VARS)
    if st.return_code != 0:
        _error(ctx.name, cl, st.stderr)
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

**File:** tools/build_defs/repo/git_worker.bzl (L225-230)
```text
def _git(ctx, git_repo, command, *args):
    start = [command]
    st = _execute(ctx, git_repo, start + list(args))
    if st.return_code != 0:
        _error(ctx.name, ["git"] + start + list(args), st.stderr)
    return st.stdout
```
