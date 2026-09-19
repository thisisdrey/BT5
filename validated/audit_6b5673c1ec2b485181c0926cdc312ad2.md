# Git Submodule `protocol.file.allow=always` Override Enables Local-File-Read via Attacker-Controlled `.gitmodules` in `git_repository`/`git_override` - (File: tools/build_defs/repo/git_worker.bzl)

### Summary
`git_repository` (and the Bzlmod `git_override`/module-extension equivalent) fetches an externally hosted Git repository whose content is *not* integrity-checked — unlike `http_archive`, there is no `sha256`/`integrity` pinning for `git_repository` at all. When `init_submodules` or `recursive_init_submodules` is set (a normal, documented attribute), Bazel's `update_submodules()` in `git_worker.bzl` unconditionally re-enables Git's `file` transport for the submodule step with `-c protocol.file.allow=always`, overriding the safe default Git shipped specifically to close the local-clone attack surface that `.gitmodules` files can otherwise exploit.

### Finding Description
`git_repo()` drives the whole fetch/checkout sequence and, when submodules are requested, calls `update_submodules()`: [1](#0-0) [2](#0-1) 

Both the recursive and non-recursive branches force `protocol.file.allow=always` on every invocation of `git submodule update --init`. Modern Git defaults `protocol.file.allow` to `user` (i.e., disabled unless the operator has explicitly opted in), a hardening measure introduced to stop a fetched/cloned repository's own submodule definitions (`.gitmodules`) from silently triggering a local ("file") transport clone against arbitrary paths on the machine running Git. Bazel's Starlark repo rule reverses that protection for every `git_repository`/`git_override` fetch that enables submodules, citing the need to support relative-path local submodules (bazelbuild/bazel#17040), but the override is not scoped to "relative paths under the checkout" — it is a blanket `always`.

The `remote`/commit/tag pinned in a `MODULE.bazel` is trusted, but the **content that remote serves is not**: the `.gitmodules` file and the tree structure of the fetched repository come from whatever the Git server/branch/tag returns at fetch time, and `git_repository` provides no checksum to detect if that content differs from what the module author expected (this is explicitly called out as a known gap versus `http_archive`, see the rule's own docs recommending `http_archive` over `git_repository` for exactly this reason). A hostile or compromised origin (or a hostile branch on a CI-buildable fork/PR) can therefore ship a `.gitmodules` entry such as:

```
[submodule "leak"]
    path = leak
    url = file:///home/victim/.ssh
```

With `protocol.file.allow=always` forced by Bazel, `git submodule update --init` will happily perform a local clone of `/home/victim/.ssh` (or any other absolute path readable by the build user) into the `leak/` directory of the external repository, where its contents become part of the workspace and can be read by any target that depends on the submodule's files (e.g., via `filegroup`/`genrule`, mirroring the pattern already used by Bazel's own `test_git_repository_submodules` test). [3](#0-2) 

### Impact Explanation
This breaks the "untrusted content stays data" invariant for `git_repository`/`git_override`: attacker-controlled `.gitmodules` content is escalated into a filesystem-read primitive that reaches arbitrary paths outside the intended checkout/repository directory, because Bazel deliberately disables the very Git-side guard (`protocol.file.allow` defaulting to `user`) designed to prevent this class of local-clone abuse. Because Git's local-clone path can also hardlink/copy objects from the source location, sensitive local files (credentials, tokens, prior build artifacts) can end up embedded in the external repository's object store and subsequently surfaced through the build graph.

### Likelihood Explanation
`init_submodules`/`recursive_init_submodules` are ordinary, commonly used attributes of `git_repository`; any project that vendors a third-party dependency via `git_repository(..., init_submodules = True)` (or `recursive_init_submodules = True`) is exposed if that dependency's Git history/branch is ever compromised or mirrored through an untrusted host, with zero additional opt-in beyond using a normal, documented feature. No credentials, victim-machine access, or trusted BUILD/.bzl tampering is required — only control over the content served at the pinned `remote`.

### Recommendation
Do not force `protocol.file.allow=always` unconditionally. Either drop the override and accept the (rare) breakage for genuinely local relative submodules, or resolve/verify that any submodule URL that will use the `file` transport is confined to a path physically inside the just-cloned checkout directory before enabling `protocol.file.allow` for that invocation, so remote-published `.gitmodules` content cannot redirect the local clone to arbitrary filesystem locations.

### Proof of Concept
1. Host (or compromise) a Git repository `outer` referenced by `git_repository(name="outer", remote=<attacker-controlled-or-compromised-url>, recursive_init_submodules = True, ...)`.
2. In `outer`, commit a `.gitmodules` entry: `url = file:///etc` (or any path readable by the CI/build user, e.g. a previous checkout containing secrets) with `path = leaked`.
3. Have the victim's `MODULE.bazel`/`WORKSPACE` reference `outer` with `recursive_init_submodules = True` (mirrors `test_git_repository_submodules_with_recursive_init_modules` in `src/test/shell/bazel/starlark_git_repository_test.sh`).
4. Run `bazel build @outer//...`; observe that `update_submodules()` invokes `git -c protocol.file.allow=always submodule update --init --recursive --checkout --force`, successfully cloning `/etc` (or the targeted path) into `external/+git_repository+outer/leaked`, whose contents are now readable by any target depending on `@outer//leaked/...`.
5. A `BuildIntegrationTestCase`/shell test analogous to `test_git_repository_submodules_with_recursive_init_modules` can assert that files from an out-of-tree path chosen by the "attacker" repository's `.gitmodules` appear inside the fetched workspace, proving the read-outside-repository bypass.

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

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L404-417)
```shellscript
function test_git_repository_submodules() {
  local outer_planets_repo_dir=$TEST_TMPDIR/repos/outer-planets

  # Create a workspace that clones the outer_planets repository.
  cat >> MODULE.bazel <<EOF
git_repository = use_repo_rule('@bazel_tools//tools/build_defs/repo:git.bzl', 'git_repository')
git_repository(
    name = "outer_planets",
    remote = "$outer_planets_repo_dir",
    tag = "1-submodule",
    init_submodules = 1,
    build_file = "//:outer_planets.BUILD",
)
EOF
```
