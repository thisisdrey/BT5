## Analysis

The CVE-2021-30081 class is "unsanitized externally-controlled string is handed to an interpreter that treats crafted syntax as executable instructions instead of inert data." The closest reachable analog in this codebase is in the **`git_repository`** repo rule's fetch path, not in extraction/patching or checksum-verified archive fetching.

### The relevant code path

`git_repository`'s `remote` attribute is a plain, unchecked string that is forwarded verbatim into `git` invocations: [1](#0-0) 

That value is passed straight to `git remote add origin <remote>` with no scheme/protocol validation: [2](#0-1) 

The command itself is executed via a raw `execute()` call, again with no allow-listing of URL schemes and no `protocol.*.allow` hardening (the only hardening present, `protocol.file.allow=always`, is applied to *submodule* updates only, not to the top-level `remote`): [3](#0-2) [4](#0-3) 

Git supports a `ext::<command>` remote-URL scheme that spawns an arbitrary shell command as a "remote helper" whenever git dials that remote (on `remote add` + `fetch`). Because Bazel never restricts the `remote` string to `http(s)`/`git`/`ssh` schemes (no `protocol.ext.allow=never`, no `GIT_ALLOW_PROTOCOL` restriction) before invoking `git`, a `remote` value of `ext::sh -c '<command>'` results in command execution on the machine running Bazel's fetch.

Contrast this with `http_archive`, where the fetched content is bound to a declared `integrity`/sha256 hash before it is trusted: [5](#0-4) 

For `git_repository`-typed Bzlmod module sources, by contrast, there is **no integrity/checksum binding at all** on `remote`, `commit`, `tag`, or `branch` — these are simply forwarded from `source.json` (an index-registry-served file) directly into `git_repo` execution: [6](#0-5) 

Bazel already has a regression test defending the `commit`/`tag`/`branch` fields against git *option* injection (using `--` separators before refs where needed) — see `test_git_repository_invalid_commit`: [7](#0-6) 

However, there is no equivalent guard on the `remote` value's *scheme*, so the `ext::` transport is not blocked.

### Why this is a plausible, non-mocked analog

- **Attacker model fits**: a hostile/compromised index registry (or a `git_repository` invocation whose `remote` is derived from an untrusted, non-BCR registry's `source.json`) can set `remote` to `ext::sh -c '<payload>'`.
- **Invariant that should hold and doesn't**: "untrusted content stays data." Here, an attacker-controlled string reaches `git`'s URL parser, which interprets embedded shell syntax as executable code — structurally identical to the injection pattern in the reported SQL-injection CVE (unsanitized attacker string parsed by an interpreter as commands, not values).
- **No existing mitigation stops it**: unlike `http_archive`'s `integrity` checksum, there's no checksum/pinning on `git_repository` fields, and no protocol allow-listing is applied before invoking `git remote add`/`fetch`.

### Suggested reproduction (shell integration test, mirroring the existing pattern)

```bash
function test_git_repository_ext_remote_command_execution() {
  local sentinel=$TEST_TMPDIR/sentinel_ext
  cat >> MODULE.bazel <<EOF
git_repository = use_repo_rule('@bazel_tools//tools/build_defs/repo:git.bzl', 'git_repository')
git_repository(
    name = "evil_repo",
    remote = "ext::sh -c 'touch $sentinel'",
    commit = "0000000000000000000000000000000000000000",
)
EOF
  bazel fetch @evil_repo >& $TEST_log
  if [ -e "$sentinel" ]; then
    fail "Arbitrary command executed via git ext:: remote transport!"
  fi
}
```

This would be added alongside `test_git_repository_invalid_commit` in `src/test/shell/bazel/starlark_git_repository_test.sh`.

---

I found a plausible analog but want to flag uncertainty explicitly: whether `ext::` (and similar `fd::`/local-helper schemes) is enabled by default in the git version Bazel's CI/release bundles, and whether any allowlist exists elsewhere (e.g., in `HttpDownloader`/`UrlRewriter` config that could indirectly gate this), is not fully verifiable from the indexed files alone — I did not find any `protocol.*.allow` or `GIT_ALLOW_PROTOCOL` configuration anywhere in the repo aside from the submodule-specific `protocol.file.allow=always`. If you want a definitive determination (e.g., confirming default git behavior across supported bundled/test git versions, or checking whether `RegistryFunction`/`ModuleFileFunction` impose any extra scheme restriction on `remote` before it reaches `git_repository`), that would require deeper tracing than the index surfaces, and I'd recommend a Devin session with full repo/tool access to confirm end-to-end and write the JUnit/shell proof.

### Citations

**File:** tools/build_defs/repo/git.bzl (L119-123)
```text
_common_attrs = {
    "remote": attr.string(
        mandatory = True,
        doc = "The URI of the remote Git repository",
    ),
```

**File:** tools/build_defs/repo/git_worker.bzl (L158-160)
```text
def add_origin(ctx, git_repo, remote):
    _git(ctx, git_repo, "remote", "add", "origin", remote)

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

**File:** tools/build_defs/repo/git_worker.bzl (L311-319)
```text
def _execute(ctx, git_repo, args):
    # "core.fsmonitor=false" disables git from spawning a file system monitor which can cause hangs when cloning a lot.
    # See https://github.com/bazelbuild/bazel/issues/21438
    start = ["git", "-c", "core.fsmonitor=false"]
    return ctx.execute(
        start + args,
        environment = ctx.os.environ | _GIT_LOCAL_ENV_VARS,
        working_directory = str(git_repo.directory),
    )
```

**File:** docs/external/registry.mdx (L79-89)
```text
*   If `type` is `archive` (the default), this module version is backed by an
    [`http_archive`](/rules/lib/repo/http#http_archive) repo rule; it's fetched
    by downloading an archive from a given URL and extracting its contents. It
    supports the following fields:
    *   `url`: A string, the URL of the source archive
    *   `mirror_urls`: A list of string, the mirror URLs of the source archive.
        The URLs are tried in order after `url` as backups.
    *   `integrity`: A string, the [Subresource
        Integrity][subresource-integrity] checksum of the archive
    *   `strip_prefix`: A string, the directory prefix to strip when extracting
        the source archive
```

**File:** docs/external/registry.mdx (L105-118)
```text
*   If `type` is `git_repository`, this module version is backed by a
    [`git_repository`](/rules/lib/repo/git#git_repository) repo rule; it's
    fetched by cloning a Git repository.
    *   The following fields are supported, and are directly forwarded to the
        underlying `git_repository` repo rule: `remote`, `commit`,
        `shallow_since`, `tag`, `init_submodules`, `verbose`, and
        `strip_prefix`, `patch_strip`.
    *   `patches`: A JSON object containing patch files to apply to the
        cloned repository. The patch files are located under the
        `/modules/$MODULE/$VERSION/patches` directory. The keys are the
        patch file names, and the values are the integrity checksum of
        the patch files. The patches are applied in the order they appear in
        `patches`.
*   If `type` is `local_path`, this module version is backed by a
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
