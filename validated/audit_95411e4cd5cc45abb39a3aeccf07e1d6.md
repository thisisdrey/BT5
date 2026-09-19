### Title
Bazel's `git_repository` submodule handling forces `protocol.file.allow=always`, defeating Git's local-file-submodule protection - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`update_submodules()` in `tools/build_defs/repo/git_worker.bzl` always injects `-c protocol.file.allow=always` before running `git submodule update --init [--recursive] --checkout --force`, unconditionally, for every `git_repository`/`new_git_repository` fetch that has `init_submodules`/`recursive_init_submodules` set. [1](#0-0) 

### Finding Description
Git added `protocol.file.allow` (default `user`, effectively disabling the `file://` transport for submodules cloned via `git submodule update`) specifically to stop a repository from silently pulling local paths on the machine running the clone — the same "improper submodule/URL handling" bug class as ALPINE-CVE-2019-1349, where crafted submodule metadata in a hostile repository lets `git submodule` operations reach content/paths the victim never intended to expose. Bazel's `git_worker.bzl` deliberately overrides this protection for *every* submodule update it performs, citing bazelbuild/bazel#17040 as the reason ("necessary... to allow the submodule command clone from a local directory"). [1](#0-0) 

Because the override is baked into `_git_maybe_shallow` invocation unconditionally, an attacker who controls the *content* of a git remote consumed by a `git_repository`/`new_git_repository` rule (a malicious/typosquatted public repo, a compromised upstream maintainer account pushing to a `branch`/`tag` a user did not pin to a `commit`, or a hostile mirror substituted for `remote`) can ship a `.gitmodules` file whose submodule `url` is `file:///absolute/local/path` (e.g. pointing at the victim's home directory, `.ssh`, `.netrc`, other checked-out repos, or CI secrets mounted on disk). Git's own guard that would normally refuse this is disabled by Bazel's forced `-c protocol.file.allow=always`, so `git submodule update --init` on the victim's machine happily "clones" that local path's contents into the external repository's checkout directory, where they become ordinary files consumable by subsequent `BUILD` targets, genrules, or `ctx.read`, effectively exfiltrating local filesystem contents into the build graph/output.

This mirrors the CVE-2019-1349 bug class: untrusted submodule metadata driving `git`'s submodule machinery into performing operations the victim did not authorize, because a safety check that should gate it is not honored.

### Impact Explanation
A hostile git remote (no code execution, no credentials, no access to the victim machine required beyond serving content the victim's `git_repository` rule fetches) can cause local files on the build machine to be read into the workspace and subsequently exposed through build outputs, genrule inputs, or committed artifacts — a concrete containment/confidentiality violation of "untrusted content stays data" and "reads stay inside the intended remote," achieved purely by data the attacker publishes.

### Likelihood Explanation
`init_submodules`/`recursive_init_submodules` are common, user-facing attributes of `git_repository`; the override is unconditional and undocumented to end users, so any consumer of a third-party `git_repository` dependency that isn't hash-pinned (or whose pinned commit itself already contains the malicious `.gitmodules`, e.g. supply-chain/typosquat scenarios) is affected with default flags on current Bazel.

### Recommendation
Do not force `protocol.file.allow=always` globally; scope it (e.g., only allow `file://` for submodules whose target resolves under the already-cloned parent repository's own working tree/remote, or require an explicit opt-in attribute on `git_repository`) so Git's default submodule protocol allowlist is preserved for untrusted remotes.

### Proof of Concept
Extend the existing submodule test in `src/test/shell/bazel/starlark_git_repository_test.sh` (which already builds an `outer-planets` repo with a `.gitmodules` submodule, see `test_git_repository_submodules`) by:
1. Creating a local "secret" directory outside any git remote directory, e.g. `$TEST_TMPDIR/secret/token.txt`.
2. Adding a `.gitmodules` entry `url = file://$TEST_TMPDIR/secret` (or turning that path into a bare git repo) to the `outer-planets` test fixture.
3. Declaring `git_repository(..., init_submodules = 1, ...)` as in `test_git_repository_submodules`.
4. Asserting that after `bazel build`, the checked-out submodule directory contains `token.txt` from the local path outside the intended remote — demonstrating that `-c protocol.file.allow=always` in `update_submodules()` let the local file be pulled in despite Git's default protection being designed to prevent exactly this. [2](#0-1)

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

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L404-454)
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

  cat > BUILD <<EOF
exports_files(['outer_planets.BUILD'])
EOF
  cat > outer_planets.BUILD <<EOF
filegroup(
    name = "neptune",
    srcs = ["neptune/info"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "pluto",
    srcs = ["pluto/info"],
    visibility = ["//visibility:public"],
)
EOF

  mkdir -p planets
  cat > planets/BUILD <<EOF
genrule(
    name = "planet-info",
    srcs = [
        "@outer_planets//:neptune",
        "@outer_planets//:pluto",
    ],
    outs = ["planet-info.txt"],
    cmd = "cat \$(SRCS) > \$@",
)
EOF

  bazel build //planets:planet-info >& $TEST_log \
    || echo "Expected build/run to succeed"
  cat bazel-bin/planets/planet-info.txt > $TEST_log
  expect_log "Neptune is a planet"
  expect_log "Pluto is a planet"
}
```
