### Title
Git submodule `file://` protocol force-enabled during `git_repository`/`new_git_repository` fetch re-enables CVE-2022-39253-class local file/repo disclosure - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`update_submodules()` in `tools/build_defs/repo/git_worker.bzl` unconditionally passes `-c protocol.file.allow=always` to `git submodule update`. This overrides Git's own default of `protocol.file.allow=user`, a protection Git added specifically to close CVE-2022-39253 (submodule `file://` local-clone confused-deputy leading to local file/history disclosure and, on vulnerable Git versions, code execution via the local-clone hardlink optimization). By forcing `always`, Bazel reintroduces exactly the class of bug Git's own default was designed to prevent, and does so for content (`.gitmodules`) that comes from the *fetched, attacker-controlled* repository rather than the trusted root repo.

### Finding Description
`git_repository`/`new_git_repository` (`tools/build_defs/repo/git.bzl`) clone an arbitrary `remote` and, when `init_submodules`/`recursive_init_submodules` is set, call `update_submodules`: [1](#0-0) 

```
def update_submodules(ctx, git_repo, recursive = False):
    if recursive:
        # "protocol.file.allow=always" allows the submodule command clone from a local directory.
        # It's necessary for Git 2.38.1 and assoicated backport versions.
        # See https://github.com/bazelbuild/bazel/issues/17040
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive", "--checkout", "--force")
    else:
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--checkout", "--force")
```

The submodule URLs read by `git submodule update` come from `.gitmodules`, which is a *file inside the fetched superproject* — content that the attacker who controls (or mirrors/MITMs, or is granted push access to) the `remote` repository fully controls. Neither `git_repository` nor `new_git_repository` verify a checksum/integrity value over the clone contents (unlike `http_archive`'s `sha256`/`integrity`); when `tag` or `branch` is used (not a pinned `commit`), the attacker's server fully determines what gets checked out, including `.gitmodules`. Even with a pinned `commit`, the submodule *url* field recorded in `.gitmodules` is still just repository data used verbatim by `git submodule` at fetch time — it is never checked against any Bazel-level integrity value.

By forcing `protocol.file.allow=always`, Bazel strips the safety net Git added in 2.38.1 to stop a malicious `.gitmodules` from pointing a submodule at a `file://` path on the machine performing the clone. With this protection disabled, an attacker-crafted `.gitmodules` entry such as:
```
[submodule "x"]
    path = x
    url = file:///home/ci/.ssh
```
or any other locally-readable git-repo path, causes Bazel's git-driven fetch to clone that local path as a "submodule," pulling its history/contents into the external repository directory under the output base — data that then becomes visible to the Bazel build (e.g., via `BUILD` targets, `genrule`, or CI artifact upload), i.e. a read that escapes the intended repository/exec-root boundary and can leak files/repos the victim's build machine can otherwise reach.

### Impact Explanation
An attacker who serves or compromises the `remote` a build depends on (a hostile mirror/fork, a compromised branch a CI job builds, or a MITM'd unauthenticated `git://`/`http://` remote) can smuggle a malicious `.gitmodules` file. When the victim's Bazel invocation uses `init_submodules = True` or `recursive_init_submodules = True` (a supported, non-default but common option for vendoring third-party C/C++ dependencies), Bazel's own `-c protocol.file.allow=always` override causes the submodule fetch to reach local filesystem paths that would otherwise be blocked by Git's built-in defense-in-depth default. This is a containment break: fetched/attacker-controlled content (`.gitmodules`) is allowed to direct reads outside the intended repository, onto the local filesystem of the build machine, with the result materialized inside the workspace where the build can further exfiltrate it.

### Likelihood Explanation
Reaching the vulnerable code path only requires: (1) a `git_repository`/`new_git_repository` usage with `init_submodules`/`recursive_init_submodules` set, and (2) the attacker controlling (or MITMing, for unauthenticated transports) the `remote`'s content — no access to the victim's machine, credentials, or trusted root `BUILD`/`.bzl` files is needed. This matches the scoped threat model (hostile origin server serving content a victim's build consumes) and default flags require no special configuration beyond the rule's own documented submodule attributes.

### Recommendation
Do not force `protocol.file.allow=always` for submodule fetches driven by untrusted `remote` content. At minimum, restore Git's default (`user`) or explicitly set `protocol.file.allow=user`/`deny` unless the user has opted in, and/or validate `.gitmodules` submodule URLs before invoking `git submodule update` to reject `file://`/local-path targets that were not explicitly authorized by the root repository's own `MODULE.bazel`/`WORKSPACE` configuration.

### Proof of Concept
1. Set up a malicious "remote" git repository containing a `.gitmodules` referencing `url = file:///path/to/sensitive/local/repo` and a corresponding submodule commit entry.
2. In a `src/test/shell/bazel/starlark_git_repository_test.sh`-style integration test, add:
```
git_repository(
    name = "evil",
    remote = "$malicious_repo_dir",
    tag = "1-build",
    recursive_init_submodules = True,
)
```
3. Run `bazel fetch @evil`, then assert that the checked-out `external/+git_repository+evil/<submodule_path>` directory contains contents cloned from the local sensitive repository (mirroring `test_git_repository_invalid_commit`'s sentinel-file pattern at [2](#0-1)  but targeting the submodule `file://` path instead of `commit`), confirming the local path's data was pulled in via the forced `protocol.file.allow=always` setting. [1](#0-0) [3](#0-2)

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

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L913-927)
```shellscript
function test_git_repository_invalid_commit() {
  local sentinel=$TEST_TMPDIR/sentinel_validation
  cat >> MODULE.bazel <<EOF
git_repository = use_repo_rule('@bazel_tools//tools/build_defs/repo:git.bzl', 'git_repository')
git_repository(
    name = "invalid_commit_repo",
    remote = "/",
    commit = "--upload-pack=touch $sentinel #",
)
EOF
  bazel fetch @invalid_commit_repo >& $TEST_log && fail "Fetch succeeded"
  if [ -e "$sentinel" ]; then
    fail "Sentinel file was created!"
  fi
}
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
