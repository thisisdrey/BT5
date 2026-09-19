No occurrence of `GIT_PROTOCOL_FROM_USER`, `protocol.allow`, or `protocol.ext.allow` restrictions anywhere in the Bazel Java/Starlark sources I could find — confirming Bazel's `git_repository` machinery imposes no protocol allowlist on the `remote` value before invoking `git`.

### Title
Arbitrary Command Execution via Git "ext::" Transport in `git_repository`'s `remote` attribute - (File: tools/build_defs/repo/git_worker.bzl)

### Summary
`git_repository`'s helper `git_worker.bzl` passes the user/extension-supplied `remote` value directly as the URL registered for the `origin` remote and later fetched from, without any allowlisting of the git transport/protocol and without disabling Git's "user protocol" helpers (`ext::`, `fd::`) via `GIT_PROTOCOL_FROM_USER=0` or `-c protocol.ext.allow=never`.

### Finding Description
`add_origin` registers the raw `remote` string as the git origin URL: [1](#0-0) . `fetch`/`_git_maybe_shallow` subsequently invoke `git fetch origin ...` against that configured remote: [2](#0-1) . The actual subprocess invocation in `_execute` only sets `-c core.fsmonitor=false` and clears a list of local Git environment variables — it never sets `GIT_PROTOCOL_FROM_USER=0` nor `-c protocol.ext.allow=never`/`-c protocol.file.allow=...` to restrict which transport helpers Git is willing to invoke: [3](#0-2) .

Git natively supports a `remote-ext` transport, where a remote URL of the form `ext::<command> <args>` causes `git fetch`/`git clone` to execute `<command>` directly (spawning an arbitrary process) instead of using a normal network protocol. Because Bazel does not set `GIT_PROTOCOL_FROM_USER=0` (the exact mitigation Git itself introduced after CVE-2017-1000117 for automated/non-interactive consumers of untrusted remote URLs, e.g. submodules), Git treats the value as if a user typed it interactively and permits `ext::`/`fd::` helpers by default.

The `remote` value reaching this code is exactly the kind of "attacker-published content" the report class targets: it is the `remote` attribute of a `git_repository` invocation, which can originate from data a Bazel module extension or `use_repo_rule` macro constructs from an external, untrusted source (e.g., a hostile registry entry, a third-party `.bzl` macro that forwards a remote URL string it downloaded/read from an external file, or any pipeline where the URL string itself is treated as "just data" rather than as trusted Starlark). Wherever the string value of `remote` is attacker-influenced, no sanitization, scheme validation, or protocol restriction rejects `ext::...`.

### Impact Explanation
If a `remote` value of the form `ext::sh -c "<attacker command>"` (or any argument-injectable git-remote-helper invocation) reaches `git_repository`, Git will execute the embedded command with the privileges of the Bazel client during `bazel fetch`/`bazel build`, which is full arbitrary command execution on the machine performing the build — matching the "command injection leads to arbitrary command execution" impact of CVE-2021-23376/CWE-77.

### Likelihood Explanation
Likelihood depends on whether some component (registry entry, extension-generated repo attrs, or forwarding macro) treats an untrusted string as a `remote` value without validating its scheme. This is plausible but I could not fully confirm within this codebase whether Bazel's officially supported bzlmod/module-extension flows ever pass externally-sourced strings directly into `git_repository`'s `remote` attribute without going through trusted root-repo Starlark first (the standard `git_repository` invocation is typically written directly in a project's own `MODULE.bazel`/`.bzl`, which the "reject untrusted root-repo Starlark" exclusion would apply to). I could not locate any built-in Bazel module extension that programmatically constructs `git_repository(remote = ...)` calls from external registry/JSON data in this repository — this is a gap in my verification, and it should be checked before treating this as fully exploitable rather than requiring an already-hostile root module.

### Recommendation
- In `tools/build_defs/repo/git_worker.bzl`'s `_execute`, always pass `GIT_PROTOCOL_FROM_USER=0` in the environment (or `-c protocol.ext.allow=never -c protocol.fd.allow=never`) to prevent execution of arbitrary commands via `ext::`/`fd::` remote helpers.
- Validate/allowlist the scheme of `ctx.attr.remote` in `git.bzl`/`git_worker.bzl` (permit only `http(s)://`, `git://`, `ssh://`, `file://`) before it is ever passed to `git remote add`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` reproduction would define a `git_repository` (or a wrapper macro simulating an extension consuming external data) with:
```python
git_repository(
    name = "pwned",
    remote = "ext::sh -c \"touch /tmp/bazel_pwned\"",
    commit = "0000000000000000000000000000000000000000",
)
```
then run `bazel fetch @pwned//...` and observe `/tmp/bazel_pwned` is created, demonstrating that the `remote` value is passed unsanitized to `git`, which invokes the `ext::` transport helper and executes the embedded command — I was not able to execute this PoC directly since I only have read access to the codebase; it should be run by a Devin agent with shell/build access to confirm behavior against the current default Git configuration used by Bazel's test harness.

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L158-159)
```text
def add_origin(ctx, git_repo, remote):
    _git(ctx, git_repo, "remote", "add", "origin", remote)
```

**File:** tools/build_defs/repo/git_worker.bzl (L161-176)
```text
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
