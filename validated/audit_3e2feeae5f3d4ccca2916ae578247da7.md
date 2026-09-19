Based on the analysis, `tools/build_defs/repo/git_worker.bzl` sets `protocol.file.allow=always` for `git submodule update`, which explicitly re-enables the `file://` transport that Git disabled by default (starting with Git 2.38.1, in response to CVE-2022-39253) precisely because a malicious repository's `.gitmodules` can point a submodule at an arbitrary local path via a `file://` URL. This is a legitimate finding to report.### Title
Local-file submodule protocol re-enabled during `git submodule update` allows attacker-controlled `.gitmodules` to read/exfiltrate arbitrary host files into the build - (File: tools/build_defs/repo/git_worker.bzl)

### Summary
`git_repository`/`new_git_repository` call `update_submodules`, which runs `git submodule update` with `-c protocol.file.allow=always` unconditionally set on the command line for both the recursive and non-recursive cases. [1](#0-0) 

### Finding Description
Git 2.38.1+ ships with `protocol.file.allow=user` by default specifically to close a bug class where a repository's `.gitmodules` file can declare a submodule whose URL uses the `file://` transport (or a bare local path), pointing at an arbitrary location on the machine performing the clone/update. Without this protection, cloning an untrusted repository with `init_submodules`/`recursive_init_submodules` enabled can be tricked into copying files from anywhere on the local filesystem into the submodule checkout directory (this is the class of bug tracked upstream as a Git CVE for local-file submodule confused-deputy reads, sometimes summarized as `protocol.file` submodule exfiltration).

Bazel's `git_worker.bzl` explicitly overrides this protection by passing `-c protocol.file.allow=always` to every `git submodule update` invocation, citing the need to support local-path recursive submodule fetches (referencing bazelbuild/bazel#17040): [1](#0-0) 

The attacker's foothold matches the required threat model exactly: an unprivileged outside party can publish a `.gitmodules` file on a branch/tag/commit that a victim's `git_repository` rule fetches (e.g. a dependency hosted on a public forge, or any untrusted branch a CI job builds). The `.gitmodules` file is untrusted content coming from the fetched repository, not something written by the trusted root-repo Starlark author — the root `git_repository` rule only specifies `remote`/`tag`/`branch`/`commit`, none of which constrain what submodules the target repo declares. Once `protocol.file.allow=always` is set, `git submodule update --init [--recursive] --checkout --force` will happily clone a submodule URL such as `file:///etc` or `file:///home/user/.ssh` (or `../../../../some/sensitive/dir` interpreted as a local path) into the repository's submodule directory tree inside the Bazel-managed external repo, which subsequently becomes readable input to the build (and to actions, tests, and outputs derived from it).

### Impact Explanation
This breaks the "untrusted content stays data" invariant: content controlled by an untrusted repository (the `.gitmodules` file) is used to direct Git to fetch data from the *victim's local filesystem*, not from the network, and that data is materialized inside the Bazel workspace where it can be incorporated into build outputs, uploaded as test data, or leaked through a `genrule`/`BUILD` file that globs the submodule directory. This is a local-file read/exfiltration primitive triggered purely by supplying malicious repository content, without any code execution on the victim's machine, credential access, or MITM assumptions — squarely within the bug class described by CVE-2017-8386 (attacker-supplied content bypasses/abuses a security-relevant restriction that specific command-line handling was supposed to enforce).

### Likelihood Explanation
Any `git_repository`/`new_git_repository` usage with `init_submodules = True` or `recursive_init_submodules = True` against a remote whose content isn't otherwise vetted (a third-party dependency repo, a fork, or a CI job building an untrusted PR branch) is affected by default, since the override is applied unconditionally in the current code and there is no attribute to opt out of it. No warning is surfaced to the user that submodule protocol protections have been disabled.

### Recommendation
Do not unconditionally set `protocol.file.allow=always`. Instead:
- Restrict the override to cases where it is actually required (e.g., only apply it when the parent `remote` itself is a local `file://`/local path, matching the specific scenario in bazelbuild/bazel#17040), and
- Otherwise, retain Git's default `protocol.file.allow=user` (or explicitly set `protocol.file.allow=user`) so that submodule URLs pointing at arbitrary local paths from an untrusted repository are rejected.
- Alternatively, validate/sanitize submodule URLs (reject `file://` and bare local paths) before running `git submodule update` unless the user has explicitly opted in via a dedicated attribute.

### Proof of Concept
A `src/test/shell/bazel/starlark_git_repository_test.sh`-style integration test can demonstrate this:
1. Create a "victim-local" sensitive file, e.g. `$TEST_TMPDIR/secret/flag.txt` containing a canary string.
2. Create an attacker-controlled git repo (`outer-planets`-style, as already exercised by `test_git_repository_submodules`/`test_git_repository_submodules_with_recursive_init_modules`) whose `.gitmodules` declares a submodule with `url = file://$TEST_TMPDIR/secret` instead of a normal relative repo path. [2](#0-1) 
3. Configure a `git_repository` rule with `init_submodules = 1` (or `recursive_init_submodules = 1`) pointing at this repo, matching the existing test pattern. [3](#0-2) 
4. Build a target that reads the checked-out submodule directory and assert that `flag.txt`'s canary content is present in the Bazel output tree — demonstrating that content from outside the intended repository (a local path chosen by the untrusted `.gitmodules`) was copied into the build because of the `-c protocol.file.allow=always` override in `update_submodules`. [1](#0-0) 

Note: I could not find any code path that gates this override on the submodule URL type (e.g., only applying it for genuinely local `remote`), so as written the override applies to every `git_repository`/`new_git_repository` invocation with submodules enabled, regardless of whether the top-level `remote` is local or a remote network host.

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

**File:** src/test/shell/bazel/starlark_git_repository_test.sh (L456-470)
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
