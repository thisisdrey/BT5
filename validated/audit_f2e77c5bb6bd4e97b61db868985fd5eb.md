## Finding

The bzlmod `http_archive`/`patch()` utility contains a shell command-injection flaw that is a structural analog of the CAI `find_file()` bug: attacker-controlled string arguments are interpolated into a shell command string with naive quoting and executed via `bash -c`.

### Title
Shell Command Injection via Unescaped `patch_args` in `patch()` Utility - (File: `tools/build_defs/repo/utils.bzl`)

### Summary
The Starlark `patch()` helper used by the built-in `http_archive` repository rule builds a shell command line by wrapping each `patch_args` element in single quotes and concatenating it with `patch_tool` and the patch file path, then runs it with `ctx.execute([bash_exe, "-c", command], ...)`. The quoting does not escape embedded single quotes, so any `patch_args` entry containing a `'` character breaks out of the intended argument and injects arbitrary shell commands.

### Finding Description
`patch()` decides whether to use Bazel's native (safe) patch implementation or to shell out, based solely on whether every `patch_args` entry starts with `"-p"`: [1](#0-0) 

If any argument fails that check (or `patch_tool` is explicitly set), Bazel falls back to building a shell command string: [2](#0-1) 

Each `patch_args` element is wrapped as `"'%s'" % arg`, which is not a safe shell-quoting operation — it does not escape single quotes inside `arg`. An argument such as `"'; touch /tmp/pwned #"` breaks out of its quotes and appends an arbitrary shell command, which is then executed by `ctx.execute([bash_exe, "-c", command], ...)`.

`patch_args`, `patch_tool`, and `patches` are ordinary, user-visible attributes of the `http_archive` repository rule: [3](#0-2) 

and `patch()` is invoked unconditionally from `_http_archive_impl`: [4](#0-3) 

Under Bzlmod, `http_archive` (or any repository rule built on `patch()`) can be declared directly in a dependency module's `MODULE.bazel` (via `use_repo_rule`) or invoked from a module extension with tag values supplied by that module — i.e., by a party who is not the root module owner. Bazel fetches and evaluates all such repositories transitively during a normal build, so a malicious module published to a registry (e.g., BCR) can ship an `http_archive(..., patches=[":trivial.patch"], patch_args=["-p1", "'; <cmd> #"])` declaration. When any consumer depends on that module — even transitively — this code path executes attacker-chosen shell commands on the invoking machine during `bazel build`/`bazel fetch`.

### Impact Explanation
This is remote code execution on the machine running Bazel, triggered merely by depending (even transitively) on a malicious module/repository. Unlike a checksum-verification bypass, this bug is in argument handling for a locally-referenced patch tool invocation — the integrity of the downloaded archive is irrelevant; the injection lives in the `patch_args` string list itself, which is not sanitized before being embedded in a shell command.

### Likelihood Explanation
Likelihood is high for any project consuming third-party Bazel modules or repositories that are not fully vetted: `patch_args`/`patch_tool` are ordinary, documented, and commonly-set attributes; the vulnerable code path is reachable any time `patch_args` contains an argument that isn't a plain `-pN` flag (a very ordinary occurrence e.g. `--fuzz=0`, `-d`, `-l`), and no `default flags` gate or containment mechanism in current Bazel prevents this — there is no escaping, allow-listing, or `execvp`-style argv-array execution.

### Recommendation
Replace the naive `"'%s'" % arg` quoting in `patch()` with proper POSIX shell quoting (escape embedded `'` as `'"'"'`) or, preferably, avoid constructing a shell command string altogether — invoke the patch tool as an argv array (`ctx.execute([patch_tool] + patch_args + ["<", ...])` is not directly expressible for a stdin redirect, so alternatively read/redirect input by other means to eliminate the `bash -c` string-building step). At minimum, reject `patch_args`/`patch_tool` values containing shell metacharacters, or apply the same rigor Bazel already uses for the native-patch path to more argument shapes.

### Proof of Concept
In an attacker-controlled (dependency) module's `MODULE.bazel`:
```python
http_archive = use_repo_rule("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

http_archive(
    name = "evil",
    url = "https://example.com/harmless.tar.gz",
    sha256 = "<valid sha256 of a legit tiny archive>",
    patches = [":trivial.patch"],   # any valid, harmless patch file
    patch_args = ["-p1", "'; touch /tmp/pwned; echo '"],
)
```
When a victim's build fetches `@evil` (directly or transitively), `patch()` in `tools/build_defs/repo/utils.bzl` builds:
```
patch '-p1' ''; touch /tmp/pwned; echo '' < <patchfile>
```
and runs it via `ctx.execute([bash_exe, "-c", command], ...)`, executing `touch /tmp/pwned` with the privileges of the Bazel build user. A `BuildIntegrationTestCase`/`src/test/shell/bazel` test analogous to `starlark_repository_test.sh` can drive an `http_archive` with such `patch_args` and assert the injected file is created. [5](#0-4) [6](#0-5)

### Citations

**File:** tools/build_defs/repo/utils.bzl (L66-71)
```text
def _use_native_patch(patch_args):
    """If patch_args only contains -p<NUM> options, we can use the native patch implementation."""
    for arg in patch_args:
        if not arg.startswith("-p"):
            return False
    return True
```

**File:** tools/build_defs/repo/utils.bzl (L250-271)
```text
    # Apply local patches
    if native_patch and _use_native_patch(patch_args):
        if patch_args:
            strip = int(patch_args[-1][2:])
        else:
            strip = 0
        for patchfile in patches:
            ctx.patch(patchfile, strip, directory = patch_directory or "")
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

**File:** tools/build_defs/repo/http.bzl (L216-220)
```text
    workspace_and_buildfile(ctx)

    remote_files_info = download_remote_files(ctx)
    remote_patches_info = patch(ctx)
    symlink_files(ctx)
```

**File:** tools/build_defs/repo/http.bzl (L486-505)
```text
    "patch_tool": attr.string(
        default = "",
        doc = "The patch(1) utility to use. If this is specified, Bazel will use the specified " +
              "patch tool instead of the Bazel-native patch implementation.",
    ),
    "patch_args": attr.string_list(
        default = [],
        doc =
            "The arguments given to the patch tool. Defaults to -p0 (see the `patch_strip` " +
            "attribute), however -p1 will usually be needed for patches generated by " +
            "git. If multiple -p arguments are specified, the last one will take effect." +
            "If arguments other than -p are specified, Bazel will fall back to use patch " +
            "command line tool instead of the Bazel-native patch implementation. When falling " +
            "back to patch command line tool and patch_tool attribute is not specified, " +
            "`patch` will be used. This only affects patch files in the `patches` attribute.",
    ),
    "patch_strip": attr.int(
        default = 0,
        doc = "When set to `N`, this is equivalent to inserting `-pN` to the beginning of `patch_args`.",
    ),
```
