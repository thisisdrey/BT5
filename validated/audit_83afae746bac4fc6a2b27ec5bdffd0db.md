### Title
Argument/transport injection into `git` via unsanitized `remote` URL in `git_repository` repo rule - ([File: tools/build_defs/repo/git_worker.bzl])

### Summary
`git_repository`'s worker (`tools/build_defs/repo/git_worker.bzl`) forwards the `remote` attribute verbatim into `git` invocations without validating its scheme or leading characters. The `remote` value can originate from a Bazel registry's `source.json` (`type: "git_repository"`), which is attacker-controlled content in the sense that any registry (including third-party/mirror `--registry` entries or a compromised/malicious registry index) can set arbitrary `remote` values that get "directly forwarded to the underlying `git_repository` repo rule" as documented in `docs/external/registry.mdx:105-111`.

### Finding Description
`git_repo()` in `tools/build_defs/repo/git_worker.bzl:41-106` builds up a `_GitRepoInfo` with `remote = str(ctx.attr.remote)` (line 87) taken straight from the rule attribute with no format/scheme check. It is then used in `add_origin`: [1](#0-0) 
which calls `_git(ctx, git_repo, "remote", "add", "origin", remote)`. `_git`/`_execute` build the process argv as `["git", "-c", "core.fsmonitor=false", "remote", "add", "origin", <remote>]` and hand it to `ctx.execute()`: [2](#0-1) [3](#0-2) 

Because the argument list is passed to `git` as-is (no `--` separator before the URL in `add_origin`, unlike `fetch()` which does add `--` before the ref), a value beginning with `-` could be interpreted as an option rather than a positional URL argument to `git remote add`.

More importantly, `git` supports non-HTTP "transport helpers" in remote URLs, including `ext::<command>`, which cause `git` to spawn an arbitrary local command as part of fetch/clone. Nothing in `git_worker.bzl` or `git.bzl` restricts the `remote` value to `http(s)://`/`git://`/`ssh://` schemes, sets `GIT_ALLOW_PROTOCOL`, or configures `protocol.ext.allow=never` before invoking `git`. The registry mechanism (`docs/external/registry.mdx:105-117`) explicitly documents that a module's `source.json` of `type: "git_repository"` can set `remote` and have it "directly forwarded" to this rule — meaning any registry serving the module (a hostile/compromised mirror registry, or a malicious BCR-like third-party registry configured via `--registry`) fully controls this string.

This mirrors the MariaDB Xcurl bug class: an externally-supplied URL/string is interpolated into a native command line invocation without sanitization, allowing execution of unintended operations (argument injection / command execution via `ext::` transport) instead of a plain fetch.

### Impact Explanation
If a hostile registry (or attacker who can influence resolved registry content that a victim's Bazel build consumes, e.g. a compromised mirror or malicious third-party `--registry` entry) publishes a module version whose `source.json` sets `"type": "git_repository"` and `"remote": "ext::sh -c ... "`, then when Bazel resolves and fetches that module dependency, `git` will execute the attacker-chosen command as part of the "clone"/"fetch"/"remote add" operation on the victim's machine — full remote code execution during dependency resolution, with no dependency on the victim's credentials, output base or trusted root BUILD/MODULE.bazel files.

### Likelihood Explanation
This requires the victim to depend (transitively) on a module resolved from a registry that is not the trusted default (or a compromise of a registry the victim already trusts) and for `git_repository`-typed `source.json` entries to be used (which the registry format explicitly supports and BCR-style registries can define). There is no scheme allowlist, `GIT_ALLOW_PROTOCOL` restriction, or `--` separator protecting `add_origin`, so the path from "attacker-controlled `remote` string in registry JSON" to "argv passed to `git`" is direct and unmitigated in the reviewed code.

### Recommendation
- Validate the `remote` attribute in `git.bzl`/`git_worker.bzl` to require an allow-listed scheme (e.g., `https://`, `http://`, `git://`, `ssh://`, or a local file path explicitly), rejecting `ext::`, `fd::`, or values starting with `-`.
- Set `GIT_ALLOW_PROTOCOL` (or pass `-c protocol.ext.allow=never -c protocol.allow=never` plus an explicit allow list for needed protocols) when invoking `git` from `_execute()`.
- Insert an explicit `--` separator before the `remote` argument in `add_origin()` (as already done for refs in `fetch()`) to prevent option-style values from being parsed as flags.
- Apply the same scheme validation before forwarding `remote` values sourced from registry `source.json` entries during module resolution.

### Proof of Concept
1. Stand up (or point `--registry=` at) a registry serving a module version whose `source.json` is:
```json
{
  "type": "git_repository",
  "remote": "ext::sh -c 'touch /tmp/pwned'",
  "commit": "0000000000000000000000000000000000000000"
}
```
2. Add a `bazel_dep` on that module in a victim `MODULE.bazel` and run `bazel build //...`.
3. During module fetch, `tools/build_defs/repo/git_worker.bzl`'s `add_origin`/`fetch` invoke `git ... remote add origin ext::sh -c 'touch /tmp/pwned'` and subsequent `git fetch origin ...`, causing `git`'s `ext` transport helper to execute the attacker's command (`touch /tmp/pwned`) on the victim's machine — demonstrable via a `BuildIntegrationTestCase`/`src/test/shell/bazel` test that serves such a registry and asserts the marker file is created after `bazel build`.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L158-159)
```text
def add_origin(ctx, git_repo, remote):
    _git(ctx, git_repo, "remote", "add", "origin", remote)
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
