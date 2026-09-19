### Title
`git_repository` submodule fetch forces `protocol.file.allow=always`, allowing a hostile `.gitmodules` to pull arbitrary local filesystem content into the build - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`git_worker.bzl`'s `update_submodules()` unconditionally passes `-c protocol.file.allow=always` to `git submodule update --init`, overriding Git's own default protocol restriction for local-file submodule URLs. This is the same bug class as CVE-2015-7545 (unsanitized remote-helper/protocol handling driven by attacker-controlled `.gitmodules`), just with the `file://` transport instead of `git-remote-ext`.

### Finding Description
When `git_repository`/`new_git_repository` is invoked with `init_submodules = True` or `recursive_init_submodules = True`, Bazel clones the repo and then calls `update_submodules`: [1](#0-0) 

Both branches hard-code `-c protocol.file.allow=always` before running `submodule update --init`. This flag is applied globally to the git invocation and is not scoped to the already-known, previously-fetched remote — it applies to whatever submodule URLs appear in the untrusted repository's `.gitmodules` file, which is attacker-controlled content fetched from the `remote` (or from a hostile mirror/branch that CI builds). Git's own upstream default (since the fix that motivated CVE-2015-7545 and later `protocol.*.allow` hardening) is to *not* allow `file://` submodule clones automatically (`protocol.file.allow=user`), precisely to stop a malicious repo's `.gitmodules` from directing the client to read from arbitrary local paths on the machine performing the clone. Bazel's `-c protocol.file.allow=always` override, added to work around bazelbuild/bazel#17040 for legitimate use cases, removes that protection entirely for every `git_repository` call using submodules, with no allow-list, path confinement, or `GIT_ALLOW_PROTOCOL` restriction of any kind.

Nothing else in the code path validates or scopes the submodule URL: `add_origin`/`fetch`/`reset`/`update_submodules` execute against whatever the fetched `.gitmodules` declares, so a submodule entry like `url = file:///etc/passwd` or `url = /home/ci-runner/.ssh` will be happily cloned by Git into the external repository's checkout directory, because the confinement check that upstream Git added is deliberately disabled by Bazel's global config override.

### Impact Explanation
An attacker who controls (or can push to / open a PR against, if CI auto-builds branches) any Git repository consumed via `git_repository(... init_submodules = True ...)` can plant a `.gitmodules` file with a `file://` (or bare local path) submodule URL pointing at sensitive host paths (SSH keys, cloud credential files, other checked-out source trees, `/proc/self/environ`, etc.). Bazel's forced `protocol.file.allow=always` causes Git to clone that local content directly into the external repository's directory tree inside Bazel's output base, where it becomes readable by the consuming `BUILD` file (e.g. via `glob()`, `filegroup`, or a `genrule`) and can be exfiltrated through normal build outputs, or simply leaked into build logs/artifacts. This is a read of content outside the intended repository/exec-root boundary, driven entirely by attacker-published data, matching the containment-violation class asked for.

### Likelihood Explanation
`init_submodules`/`recursive_init_submodules` are documented, commonly used attributes of `git_repository`; any consumer of a third-party dependency hosted on Git with submodules (or any CI job that builds an external/forked branch) is exposed. No additional attacker capability beyond publishing repository content (the `.gitmodules` file) is required — this matches the "hostile origin server/repo" threat model in the prompt.

### Recommendation
Do not blanket-enable `protocol.file.allow=always` for submodule updates. Instead:
- Restrict submodule fetches to safe, expected protocols only (e.g., set `protocol.file.allow=user` or explicitly enumerate `GIT_ALLOW_PROTOCOL=https:http:git:ssh` before invoking `submodule update`), or
- Validate/canonicalize submodule URLs from `.gitmodules` before fetching, rejecting `file://`/bare local paths that resolve outside the already-cloned repository's own directory, restoring Git's upstream protection instead of overriding it globally.

### Proof of Concept
1. Attacker creates (or gets merged into a branch that CI auto-builds) a Git repo `evil-outer` containing a `.gitmodules`:
   ```
   [submodule "leak"]
       path = leak
       url = file:///etc/passwd
   ```
2. Victim's `MODULE.bazel`/`WORKSPACE` declares:
   ```python
   git_repository(
       name = "evil_outer",
       remote = "https://example.com/evil-outer.git",
       branch = "main",
       init_submodules = True,
       build_file_content = "exports_files(glob([\"leak/**\"]))",
   )
   ```
3. During `bazel build`, `git_repo()` → `update_submodules()` runs `git -c protocol.file.allow=always submodule update --init --checkout --force`, which clones `/etc/passwd` (or any other local path) directly into `.../external/+git_repository+evil_outer/leak/`.
4. A `genrule` reading `@evil_outer//:leak` now has the contents of the victim's `/etc/passwd` (or an SSH key, etc.) copied into the sandbox/output, demonstrating exfiltration of local filesystem content driven purely by attacker-controlled `.gitmodules`.

A `src/test/shell/bazel/starlark_git_repository_test.sh`-style shell test (mirroring `test_git_repository_submodules_with_recursive_init_modules`, at [2](#0-1) ) can be adapted: create an "outer" repo whose submodule entry uses a `file://` URL to a directory outside the git repo tree (e.g., `$TEST_TMPDIR/secret`), run `bazel build` with `init_submodules = 1`, and assert the secret file's content ends up materialized under the external repository directory.

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
