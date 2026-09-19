## Title
Arbitrary File Read via Git Submodule `file://` Protocol Re-enablement in `git_repository`/`new_git_repository` — (File: `tools/build_defs/repo/git_worker.bzl`)

## Summary
The Starlark-based `git_repository`/`new_git_repository` rules unconditionally pass `-c protocol.file.allow=always` to every `git submodule update` invocation, regardless of whether submodules are updated recursively or not. This deliberately re-enables the `file://` submodule protocol that upstream Git disabled by default starting in Git 2.38.1 specifically to close CVE-2022-39253 (arbitrary file read/write via malicious `.gitmodules` entries). Because the submodule URLs come from `.gitmodules`, a file inside the *fetched, attacker-controlled* Git repository, an attacker who controls the content of a repo/branch that a victim's Bazel build fetches (e.g., a compromised or malicious fork/branch referenced by `remote`) can smuggle a `file://` (or local-path) submodule URL that points at arbitrary files on the machine running the build, causing Git to "clone" (copy) those files into the workspace. The copied contents then become reachable by ordinary Bazel targets (e.g., `genrule`/`filegroup`), enabling exfiltration of secrets or any file readable by the Bazel process — directly analogous to ConsoleMe's flag-injection-driven arbitrary file read.

## Finding Description
`update_submodules` in `tools/build_defs/repo/git_worker.bzl` runs: [1](#0-0) 

Both the recursive and non-recursive code paths pass `-c protocol.file.allow=always` before `submodule update --init ... --checkout --force`. This flag directly disables the Git-side guardrail that treats submodule URLs starting with `file://` (or resolving to local paths) as untrusted by default. That guardrail exists precisely because `.gitmodules` — the file that declares submodule URLs — is tracked content of the (possibly untrusted) repository being cloned, not something the invoking user authored.

`init_submodules`/`recursive_init_submodules` are ordinary, commonly-used attributes of `git_repository`: [2](#0-1) 

and `_update` calls `update_submodules` whenever either is set: [3](#0-2) 

The attacker's control surface is exactly the class of input the rules describe as in-scope: "content a victim's build consumes" from a remote the build fetches (a malicious/compromised fork, or an untrusted branch that CI builds). The `remote` attribute is trusted, but the *tree contents fetched from that remote* — including `.gitmodules` — are not; nothing in `git_worker.bzl` validates or restricts the submodule URLs found there before Git processes them with `protocol.file.allow=always`.

## Impact Explanation
With `protocol.file.allow=always` forced on, a `.gitmodules` entry such as:
```
[submodule "leak"]
    path = leak
    url = file:///etc/passwd
```
(or a relative `file://` path escaping to arbitrary filesystem locations reachable by the build user, e.g. CI secrets, SSH keys, cloud credential files) causes `git submodule update --init` to copy that file's contents into the checked-out repository tree under the external repository directory. From there it is fully accessible to any Bazel target depending on the git repository (via `filegroup`, `genrule`, etc.), allowing the file's contents to be embedded in build outputs or otherwise exfiltrated — a concrete read of files outside the intended repository/source boundary, driven entirely by attacker-published repository content.

## Likelihood Explanation
`init_submodules`/`recursive_init_submodules = True` is a routine, documented configuration for any dependency that itself uses Git submodules — Bazel's own test suite exercises this exact code path (`test_git_repository_submodules_with_recursive_init_modules`): [4](#0-3) 

Any project depending on a third-party repository that uses submodules is exposed the moment that upstream repository (or an attacker who can push a branch/tag/commit CI or a developer later builds, e.g. via `commit`/`tag`/`branch` pinned to an untrusted ref, a compromised maintainer account, or a malicious fork used as a mirror) inserts a crafted `.gitmodules`. No special privileges beyond the ability to influence the fetched tree content are required, and the checksum/commit-pinning that normally provides integrity for `git_repository` (pinning to a specific `commit`) does not protect against this because the malicious content can simply be part of the very commit that is pinned.

## Recommendation
Do not blanket-enable `protocol.file.allow=always` for arbitrary submodule fetches. At minimum:
- Restrict the override to only the specific, narrow case documented in bazelbuild/bazel#17040 (recursive submodules cloned from a known-local, already-verified directory), rather than applying it to every `git submodule update` call.
- Before enabling `file://` submodule support, parse `.gitmodules` and reject/require confirmation for submodule URLs using `file://` or resolving outside the repository being cloned.
- Alternatively, drop the override and instead pass `--reference`/local-clone optimizations through mechanisms that don't require disabling Git's submodule URL protocol allowlist.

## Proof of Concept
Extending the existing shell test harness pattern in `src/test/shell/bazel/starlark_git_repository_test.sh`:
1. Create an "attacker" git repo `outer_planets` with a `.gitmodules` file containing:
   ```
   [submodule "leak"]
       path = leak
       url = file:///etc/hostname
   ```
   (any file readable by the build user; `/etc/hostname` used here for a portable, non-destructive PoC).
2. Reference it from a victim `MODULE.bazel`:
   ```
   git_repository(
       name = "outer_planets",
       remote = "<attacker_repo_dir>",
       tag = "1-submodule",
       recursive_init_submodules = 1,
   )
   ```
3. Add a `genrule`/`filegroup` depending on `@outer_planets//leak:...` and run `bazel build`.
4. Observe that `git -c protocol.file.allow=always submodule update --init --recursive --checkout --force` succeeds and copies `/etc/hostname`'s contents into the `leak` subdirectory of the external repository, where the Bazel target can read and expose it — whereas with default Git submodule protections (`protocol.file.allow=user`), this operation is refused with `fatal: transport 'file' not allowed`.

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

**File:** tools/build_defs/repo/git.bzl (L154-161)
```text
    "init_submodules": attr.bool(
        default = False,
        doc = "Whether to clone submodules in the repository.",
    ),
    "recursive_init_submodules": attr.bool(
        default = False,
        doc = "Whether to clone submodules recursively in the repository.",
    ),
```

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L456-469)
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
```
