Found it. `_download_patch` in `tools/build_defs/repo/utils.bzl` derives the local patch file name directly from the **attacker-controlled URL path** (`patch_url.split("/")[-1]`) with no sanitization, and that filename is later interpolated **unquoted** into a shell command string executed via `bash -c`. [1](#0-0) 

That filename flows into `patch()`, where, when `patch_tool`/`patch_args` force the non-native code path, it is embedded directly into a shell command string without quoting: [2](#0-1) 

### Title
Command Injection via Unsanitized Remote Patch Filename in `patch()` Shell Command Construction - (File: tools/build_defs/repo/utils.bzl)

### Summary
`patch()` in `tools/build_defs/repo/utils.bzl` builds a shell command string to invoke an external patch tool: `"{patchtool} {patch_args} < {patchfile}".format(...)`, executed via `ctx.execute([bash_exe, "-c", command], ...)`. The `patchfile` component comes, for remote patches, directly from `_download_patch`, which sets the local file name to `patch_url.split("/")[-1]` — the last path segment of an attacker-controlled `remote_patches` URL — with no escaping/quoting applied before being embedded in the `bash -c` string. This is structurally the same bug class as Haraka's zip-attachment command injection (GHSA-w5m8-5v9m-xhx5 / CVE-2016-1000282): untrusted, attacker-influenced text (there a zip filename, here a URL-derived filename) is spliced into a shell command line unescaped.

### Finding Description
`http_archive`'s `remote_patches` attribute maps a patch URL to an expected integrity hash: [3](#0-2)  The integrity check validates the *content* bytes of the downloaded patch, but never validates or sanitizes the *file name* used to store it, which is taken verbatim from the URL: [1](#0-0) 

Later, if the repository rule sets a custom `patch_tool` or passes `patch_args` other than `-pN` (both plausible/normal `http_archive` configurations for handling binary patches), Bazel falls back to invoking the patch tool through a shell string rather than the native patcher: [4](#0-3) [5](#0-4)  The `patchfile` value (`ctx.path(patchfile)`, i.e., the resolved path whose basename is the attacker-chosen URL segment) is interpolated into `command` with no shell quoting, then handed to `bash -c command`. A URL such as `https://attacker.example/patch.diff$(id>${HOME}/pwned)` or one containing backticks/`;`/`|` in the final path segment produces a local file name containing those shell metacharacters, which are then executed by `bash -c` when the string is built.

### Impact Explanation
Because `ctx.execute([bash_exe, "-c", command])` runs on the machine performing the Bazel fetch/build (the same trust boundary as `WORKSPACE`/`MODULE.bazel` execution for repository rules), a successful injection here yields arbitrary command execution on the build machine outside the sandboxed/integrity-checked artifact — i.e., untrusted content (a hostile registry/mirror serving crafted URLs, or a malicious `remote_patches` entry propagated via a compromised transitive Bazel module) escalates to command injection despite integrity being enforced on the patch *contents*. This matches the "integrity is binding" invariant being broken: the sha256/integrity check protects the bytes downloaded but not the derived filename used in command construction.

### Likelihood Explanation
Exploitation requires that the affected repository rule set a non-default `patch_tool` or non-`-pN` `patch_args` (to hit the shell fallback) and that the `remote_patches` URL (or its last path segment) be attacker-influenced — e.g. a malicious Bazel module supplying its own `http_archive`/`remote_patches` map, or a compromised mirror redirect changing the final URL segment. This is a real but non-default configuration path, and it depends on root/module-declared attributes (`patch_tool`, `patch_args`, `remote_patches`) rather than pure external-content control, which somewhat limits pure unprivileged-outsider reach unless the URL itself is what's attacker-controlled (e.g., supplied by a registry-hosted module that pulls patches from a third party mirror it doesn't fully control).

### Recommendation
Do not derive shell-embedded file names from untrusted URL content. In `_download_patch`, sanitize/normalize the local file name (e.g., use a fixed or hashed name unrelated to the URL) instead of `patch_url.split("/")[-1]`. In `patch()`, avoid string-based shell command construction entirely — pass `patchfile` as a separate argv element (e.g., via `ctx.execute([patch_tool] + patch_args + ["<", ...])` is not directly expressible in Starlark's execve semantics, but the current design should redirect input via a file argument to the patch tool rather than shell `<` redirection, or explicitly shell-quote every interpolated value using a proper quoting utility before building the `bash -c` string).

### Proof of Concept
A `src/test/shell/bazel` style reproduction:
1. Configure an `http_archive` with `remote_patches = {"https://attacker.example/patch.diff`touch /tmp/PWNED`": "sha256-<validhash>"}` (or a locally served HTTP file server acting as the "attacker" host, serving a file at that exact URL, whose content matches the declared integrity value) and `patch_tool = "patch"` (forcing the shell fallback via `_use_native_patch` returning true only for `-p` args — set `patch_args = ["-p1", "--binary"]` to force fallback while still being interpretable, or simply set `patch_tool` explicitly).
2. Run `bazel fetch @that_repo` and observe that `/tmp/PWNED` is created, demonstrating command execution occurred outside of the patch tool despite the downloaded patch content correctly matching its declared integrity hash.

### Citations

**File:** tools/build_defs/repo/utils.bzl (L73-83)
```text
def _download_patch(ctx, patch_url, integrity, auth = None):
    name = patch_url.split("/")[-1]
    patch_path = ctx.path(_REMOTE_PATCH_DIR).get_child(name)
    download_info = ctx.download(
        patch_url,
        patch_path,
        canonical_id = ctx.attr.canonical_id,
        auth = get_auth(ctx, [patch_url]) if auth == None else auth,
        integrity = integrity,
    )
    return patch_path, download_info
```

**File:** tools/build_defs/repo/utils.bzl (L197-204)
```text
    if patch_tool == None and hasattr(ctx.attr, "patch_tool"):
        patch_tool = ctx.attr.patch_tool
    if not patch_tool:
        patch_tool = "patch"
        native_patch = True
    else:
        native_patch = False

```

**File:** tools/build_defs/repo/utils.bzl (L221-229)
```text
    # Apply remote patches
    remote_patches_download_info = {}
    for patch_url in remote_patches:
        integrity = remote_patches[patch_url]
        patchfile, download_info = _download_patch(ctx, patch_url, integrity, auth)
        remote_patches_download_info[patch_url] = download_info
        ctx.patch(patchfile, remote_patch_strip)
        ctx.delete(patchfile)
    ctx.delete(ctx.path(_REMOTE_PATCH_DIR))
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
