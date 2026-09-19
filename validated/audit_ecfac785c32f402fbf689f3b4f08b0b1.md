### Title
`git_repository`/`new_git_repository` re-enables Git's disabled `file://` submodule protocol, reopening local-path submodule exfiltration (CVE-2022-39253-class) - (`tools/build_defs/repo/git_worker.bzl`)

### Summary
Bazel's built-in `git_repository` rule fetches submodules with `git -c protocol.file.allow=always submodule update --init [--recursive] --checkout --force`, unconditionally overriding the safety default (`protocol.file.allow=user`/`never`) that upstream Git shipped specifically to close the "malicious submodule URL" bug class that CVE‑2017‑1000117 belongs to (and that CVE‑2022‑39253 later hardened further for local/`file://` submodule URLs). An attacker who controls the content of a git dependency that a victim's build fetches (a `git_repository(..., commit=<pinned-sha>, init_submodules=True|recursive_init_submodules=True)`) can ship a `.gitmodules` entry with a `file://` or relative-path submodule URL. Because Bazel force-enables the `file` protocol for the submodule step, Git will happily perform a local clone/hardlink of an arbitrary path reachable by the build user, letting attacker-authored repository content pull files from outside the intended checkout into the build tree.

### Finding Description
`git_repo()` in `tools/build_defs/repo/git_worker.bzl` drives the whole clone/checkout sequence for the `git_repository`/`new_git_repository` rules: `init` → `add_origin` → `fetch` → `reset` → `clean` → (`update_submodules` if `init_submodules`/`recursive_init_submodules` is set). [1](#0-0) 

The submodule step is:
```
_git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive", "--checkout", "--force")
```
with the accompanying comment explaining that this override is required so submodules can be cloned "from a local directory": [2](#0-1) 

`protocol.file.allow=user`/`never` (the modern Git default for anything reached transitively, e.g. via submodule URLs) is precisely the mitigation Git introduced after the "arbitrary submodule URL causes unexpected code/side effects" bug class (of which CVE-2017-1000117 is the canonical example, later hardened for local paths by the `protocol.file.allow` restriction). Bazel's `-c protocol.file.allow=always` blanket override defeats that restriction for every `git_repository` invocation that turns on submodules, regardless of whether the remote is actually local.

The `remote`, `commit`, `init_submodules`, and `recursive_init_submodules` values come straight from rule attributes: [3](#0-2) . Bazel pins the fetched tree by `commit`, but it does not vet or sanitize the *content* of that tree, including `.gitmodules`. An attacker who authors/controls the git repository referenced by `remote` (a legitimate scenario for third-party git dependencies, forks, or CI branches that a victim's build consumes) fully controls `.gitmodules`, including submodule URLs. There is no code path in `git.bzl`/`git_worker.bzl` that inspects or restricts the scheme/target of submodule URLs before Bazel invokes `git submodule update --init` with the protection disabled.

### Impact Explanation
With `protocol.file.allow=always` forced, an attacker-controlled `.gitmodules` entry such as
```
[submodule "x"]
    path = x
    url = file:///home/ci/.ssh
```
or a relative path escaping the repository (`../../secret-repo/.git`) causes Git to perform a local-clone with its hardlink/`--local` optimizations against a path chosen by the attacker, not the intended remote. This can pull files/objects from elsewhere on the build host's filesystem into the fetched repository's working tree, where they become buildable/readable inputs — i.e., a read outside the intended repository/exec root, and a route to credential/data exfiltration if secrets or SSH keys are reachable on the build machine (common on CI runners). This corresponds to the "write or read outside the repository / exec root / output base" and "credential exfiltration" impact classes.

### Likelihood Explanation
Reachable with default flags on any Bazel build that uses the stock `git_repository`/`new_git_repository` repo rules (shipped in `@bazel_tools//tools/build_defs/repo:git.bzl`) with `init_submodules = True` or `recursive_init_submodules = True` — both documented, commonly-used attributes. [4](#0-3)  No opt-in flag disables the `protocol.file.allow=always` override; it is unconditional in `update_submodules`. [2](#0-1)  The only requirement is that the victim's build fetch a git dependency whose `.gitmodules` is attacker-influenced, which matches the allowed attacker model (hostile/compromised upstream repo, or an untrusted branch/fork a CI job builds).

### Recommendation
Do not globally force `protocol.file.allow=always` for submodule updates. Instead:
- Restrict the override to cases where the top-level `remote` itself is a local `file://`/filesystem path (the actual use case cited in bazelbuild/bazel#17040), and leave Git's default protections in place for all other remotes.
- Alternatively, resolve and canonicalize submodule URLs before invoking `git submodule update`, rejecting `file://`/relative paths that escape the checkout directory, mirroring the containment checks already used for `strip_prefix`/`add_prefix` in `git.bzl`. [5](#0-4) 

### Proof of Concept
1. Create attacker repository `evil.git` with a pinned commit containing:
   - `.gitmodules`:
     ```
     [submodule "leak"]
         path = leak
         url = file:///etc
     ```
   - a corresponding gitlink entry for `leak`.
2. Victim `MODULE.bazel`:
   ```python
   git_repository = use_repo_rule("@bazel_tools//tools/build_defs/repo:git.bzl", "git_repository")
   git_repository(
       name = "dep",
       remote = "https://example.com/evil.git",
       commit = "<pinned-sha>",
       init_submodules = True,
   )
   ```
3. `bazel build @dep//...` triggers `git_repo()` → `update_submodules()` in `tools/build_defs/repo/git_worker.bzl`, which runs `git -c protocol.file.allow=always submodule update --init --checkout --force`; because `protocol.file.allow` is forced to `always`, Git performs the local clone of `file:///etc` (or an attacker-chosen relative path) into `external/+git_repository+dep/leak`, whereas the same command with Git's own hardened default (`protocol.file.allow=user`) would refuse the operation with `fatal: transport 'file' not allowed`.
4. This can be encoded as a `src/test/shell/bazel/starlark_git_repository_test.sh` case asserting that a submodule URL of `file://<path outside the checkout>` is rejected rather than fetched, alongside the existing refetch/strip_prefix tests in that file. [6](#0-5)

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

**File:** tools/build_defs/repo/git.bzl (L43-63)
```text
    checkout_path = _checkout_path(ctx)
    git_ = git_repo(ctx, str(checkout_path))

    if ctx.attr.strip_prefix:
        strip_prefix_path = checkout_path.get_child(ctx.attr.strip_prefix)
        if not strip_prefix_path.exists:
            fail("strip_prefix at {} does not exist in repo".format(ctx.attr.strip_prefix))
        if not strip_prefix_path.is_dir:
            fail("strip_prefix at {} is not a directory".format(ctx.attr.strip_prefix))

        strip_prefix_path = strip_prefix_path.realpath
        checkout_realpath = checkout_path.realpath
        strip_prefix_realpath = str(strip_prefix_path).lower()
        git_metadata_path = str(checkout_realpath.get_child(".git")).lower()
        if (strip_prefix_realpath == git_metadata_path or
            strip_prefix_realpath.startswith(git_metadata_path + "/")):
            fail("strip_prefix at {} refers to Git metadata".format(ctx.attr.strip_prefix))
        if strip_prefix_path != checkout_realpath:
            if not str(strip_prefix_path).startswith(str(checkout_realpath) + "/"):
                fail("strip_prefix at {} escaped the checkout directory".format(ctx.attr.strip_prefix))

```

**File:** tools/build_defs/repo/git.bzl (L119-161)
```text
_common_attrs = {
    "remote": attr.string(
        mandatory = True,
        doc = "The URI of the remote Git repository",
    ),
    "commit": attr.string(
        default = "",
        doc =
            "specific commit to be checked out." +
            " Precisely one of branch, tag, or commit must be specified.",
    ),
    "shallow_since": attr.string(
        default = "",
        doc =
            "an optional date, not after the specified commit; the argument " +
            "is not allowed if a tag or branch is specified (which can " +
            "always be cloned with --depth=1). Setting such a date close to " +
            "the specified commit may allow for a shallow clone of the " +
            "repository even if the server does not support shallow fetches " +
            "of arbitrary commits. Due to bugs in git's --shallow-since " +
            "implementation, using this attribute is not recommended as it " +
            "may result in fetch failures.",
    ),
    "tag": attr.string(
        default = "",
        doc =
            "tag in the remote repository to checked out." +
            " Precisely one of branch, tag, or commit must be specified.",
    ),
    "branch": attr.string(
        default = "",
        doc =
            "branch in the remote repository to checked out." +
            " Precisely one of branch, tag, or commit must be specified.",
    ),
    "init_submodules": attr.bool(
        default = False,
        doc = "Whether to clone submodules in the repository.",
    ),
    "recursive_init_submodules": attr.bool(
        default = False,
        doc = "Whether to clone submodules recursively in the repository.",
    ),
```

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L511-527)
```shellscript
  local repo_dir=$TEST_TMPDIR/repos/refetch

  rm MODULE.bazel
  cat >> MODULE.bazel <<EOF
git_repository = use_repo_rule('@bazel_tools//tools/build_defs/repo:git.bzl', 'git_repository')
git_repository(name='g', remote='$repo_dir', commit='22095302abaf776886879efa5129aa4d44c53017', verbose=True)
EOF

  # Use batch to force server restarts.
  bazel --batch build @g//:g >& $TEST_log || fail "Build failed"
  expect_log "Cloning"
  assert_contains "GIT 1" bazel-genfiles/external/+git_repository+g/go

  # Without changing anything, restart the server, which should not cause the checkout to be re-cloned.
  bazel --batch build @g//:g >& $TEST_log || fail "Build failed"
  expect_not_log "Cloning"
  assert_contains "GIT 1" bazel-genfiles/external/+git_repository+g/go
```
