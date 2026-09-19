## Finding: Bazel's `git_repository` submodule init disables Git's local‑clone protections (`protocol.file.allow=always`)

The bug class in **ALPINE‑CVE‑2019‑19604** is "checking out attacker-controlled repository content leads to execution/incorporation of attacker-chosen, untrusted actions during a submodule operation." Bazel's Starlark git tooling contains a directly analogous issue: it unconditionally overrides Git's submodule safety guard when performing `git submodule update --init`.

### Root cause [1](#0-0) 

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

This is called from `_update()` in the same file whenever `git_repository(init_submodules = True)` or `git_repository(recursive_init_submodules = True)` is used: [2](#0-1) 

### Why this matters
Modern Git ships `protocol.file.allow` defaulted to a restrictive value (as part of the fix line for the class of vulnerabilities culminating in CVE‑2022‑39253, "arbitrary file read via submodule using `file://`/local‑clone transport") precisely to stop an untrusted repository's `.gitmodules` from declaring a submodule whose `url` is a local filesystem path. Without that guard, `git submodule update --init` will happily perform a local clone of *any* path on the build machine that the invoking user can read, and materialize its contents inside the external repository directory that Bazel builds from.

Bazel's `git_worker.bzl` explicitly re-enables this behavior with `-c protocol.file.allow=always` for every submodule update, citing an unrelated compatibility issue (`bazelbuild/bazel#17040`) as justification, with no validation of the `.gitmodules` submodule URLs it is about to honor.

### Attack path
1. An attacker controls (or has push access to, e.g. via a malicious fork or an untrusted branch that CI builds) a Git repository referenced by a `git_repository(remote = ..., init_submodules = True)` or `recursive_init_submodules = True` rule — this matches the allowed "untrusted branch CI builds" attacker model.
2. That repository's `.gitmodules` declares a submodule whose `url` is an absolute local path on the victim build machine (e.g., a path under the user's home directory, another checked-out external repo under the Bazel output_base, or any world/user-readable directory containing secrets or source).
3. When Bazel invokes `update_submodules`, Git — with `protocol.file.allow=always` forced on — performs a local clone/hardlink of that arbitrary local path and materializes its file contents inside the external repo's checkout directory, i.e. **outside** the intended, integrity-checked repository content stream.
4. Those exfiltrated files are then visible to and can be consumed by subsequent build actions (e.g., copied into a `filegroup`, embedded into an output artifact, or, if the build later exports/publishes artifacts, exfiltrated off the machine) — a read outside the repository/checkout boundary that Bazel is trusted to enforce for external repositories.

### Why existing protections don't stop it
`git_repository`/`git_worker.bzl` has no checksum or lockfile pinning of submodule content (unlike `http_archive`'s `sha256`), and no validation of `.gitmodules` submodule URLs before invoking `git submodule update`; the only relevant safeguard (Git's own `protocol.file.allow` default) is explicitly disabled by the `-c protocol.file.allow=always` flag shown above.

### Recommendation
Do not blanket-enable `protocol.file.allow=always`. Restrict it to only cover the specific compatibility case from bazelbuild/bazel#17040 (cloning from the already-verified local temp clone directory Bazel itself created), or validate/allowlist submodule URLs (reject `file://`/local-path submodule URLs originating from `.gitmodules` of the fetched repository) before running `git submodule update --init`.

### Proof of concept sketch
A `src/test/shell/bazel/starlark_git_repository_test.sh`-style test can reuse the existing submodule test scaffolding (`test_git_repository_submodules`, `test_git_repository_submodules_with_recursive_init_modules`, at [3](#0-2)  and [4](#0-3) ) but replace the `pluto` submodule's `.gitmodules` URL with a `file://` path pointing at a sentinel file outside the outer-planets repo (e.g., a sibling directory containing a marker secret), set `init_submodules = 1`, run `bazel build`, and assert the sentinel content appears inside the fetched `@outer_planets` repository tree — demonstrating that content from outside the declared remote is being pulled into the build.

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

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L456-506)
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
