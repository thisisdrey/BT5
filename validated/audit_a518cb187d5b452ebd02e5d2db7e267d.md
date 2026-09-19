## Analog Found

### Title
Shell command injection via unescaped `patch_args`/`patch_tool` in the `patch()` repository utility — (File: `tools/build_defs/repo/utils.bzl`)

### Summary
Like `mount.cifs` invoking a shell with improperly escaped input to request a password, Bazel's shared repository-rule patching helper `patch()` builds a shell command string by naively single-quoting each `patch_args` element and interpolating it (along with `patch_tool` and the target patch file path) into a string that is executed via `bash -c`. Because the quoting does not escape embedded single quotes, any caller that forwards attacker-influenced strings into `patch_args` (or `patch_tool`) can break out of the quoting and inject arbitrary shell commands that run in the victim's build.

### Finding Description
`patch()` in [1](#0-0)  is the common implementation used by `http_archive`-style repository rules to apply patches after extraction. When a non-default `patch_tool` is configured (or `patch_args` contains anything besides plain `-p<N>` strip flags), Bazel falls back to the shell path instead of the native patch implementation: [2](#0-1) 

```
command = "{patchtool} {patch_args} < {patchfile}".format(
    patchtool = patch_tool,
    patchfile = ctx.path(patchfile),
    patch_args = " ".join([
        "'%s'" % arg
        for arg in patch_args
    ]),
)
st = ctx.execute([bash_exe, "-c", command], working_directory = patch_directory or "")
```

Each `arg` in `patch_args` is wrapped with a single-quote pair (`'%s'`) but no escaping is performed for a literal `'` inside the value. A string such as `'; touch pwned; #` closes the quoting early and appends arbitrary shell syntax, which `bash -c` then executes with the privileges of the Bazel client process. The `patchtool` value itself is also interpolated unquoted, so a malicious `patch_tool` string doubles as another injection point. This mirrors the CVE-2020-14342 pattern exactly: a shell is invoked with values that are supposed to be inert data but are instead concatenated into a command line with insufficient escaping.

### Impact Explanation
Successful injection results in arbitrary command execution in the developer's/CI's build environment — the same severity class as the cifs-utils flaw (local privilege/trust boundary violation via shell metacharacter injection), potentially leading to credential theft, workspace tampering, or supply-chain compromise of downstream artifacts, since it runs during the repository-fetch phase before sandboxing of build actions even applies.

### Likelihood Explanation
Exploitation requires that `patch_args` (or `patch_tool`) values passed to `patch()` are influenced by content outside the root module's trust boundary — for example, values sourced from tag data supplied by a transitive Bazel module (a registry-hosted dependency) whose module extension blindly forwards the tag's string field into `patch()`'s `patch_args`/`patch_tool` parameters, or any repo-rule wrapper that lets one of these fields flow from downloaded/registry content rather than being a hard-coded literal in the root `MODULE.bazel`/`BUILD` files. Because this is a widely-shared utility (`@bazel_tools//tools/build_defs/repo:utils.bzl`) used by many custom and third-party repository rules, the number of potential call sites that could unknowingly funnel semi-trusted strings into these parameters is nontrivial, though it does depend on such a pass-through existing in a particular rule/extension implementation.

### Recommendation
- Quote shell arguments safely (e.g., replace `'` with `'\''` or use a proper shell-quoting helper) before interpolating `patch_args` values into the command string, or avoid string concatenation entirely by using `patch_tool` plus `patch_args` as a discrete argv list passed directly to `ctx.execute()` instead of round-tripping through `bash -c`.
- Similarly quote/validate `patch_tool` and the `patchfile` path before interpolation.
- Where feasible, prefer the native patch path (`ctx.patch()`) and restrict the shell fallback to a fixed, non-attacker-influenced set of `-p<N>` style arguments, as already done for the "native patch" fast path.

### Proof of Concept
A `BuildIntegrationTestCase`/shell-integration reproduction: define a repository rule that calls `patch(ctx, patch_tool="patch", patch_args=["-p1", "'; touch " + marker_path + "; echo '"])` and assert that after `bazel fetch`/`bazel build` on the resulting repo, `marker_path` exists — demonstrating that a single crafted `patch_args` string breaks out of the intended `patch -p1` invocation and executes an arbitrary injected command via `bash -c`.

### Citations

**File:** tools/build_defs/repo/utils.bzl (L143-150)
```text
def patch(ctx, patches = None, patch_cmds = None, patch_cmds_win = None, patch_tool = None, patch_args = None, auth = None, patch_directory = None):
    """Implementation of patching an already extracted repository.

    This rule is intended to be used in the implementation function of
    a repository rule. If the parameters `patches`, `patch_tool`,
    `patch_args`, `patch_cmds`, `patch_cmds_win` and `patch_directory`
    are not specified then they are taken from `ctx.attr`.

```

**File:** tools/build_defs/repo/utils.bzl (L258-271)
```text
    else:
        for patchfile in patches:
            command = "{patchtool} {patch_args} < {patchfile}".format(
                patchtool = patch_tool,
                patchfile = ctx.path(patchfile),
                patch_args = " ".join([
                    "'%s'" % arg
                    for arg in patch_args
                ]),
            )
            st = ctx.execute([bash_exe, "-c", command], working_directory = patch_directory or "")
            if st.return_code:
                fail("Error applying patch %s:\n%s%s" %
                     (str(patchfile), st.stderr, st.stdout))
```
