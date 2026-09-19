### Title
Shell command injection via unquoted patch file path in `patch()` when applying registry/remote patches with a custom `patch_tool` - (File: tools/build_defs/repo/utils.bzl)

### Summary
`tools/build_defs/repo/utils.bzl`'s `patch()` helper builds a shell command line by string-formatting the patch tool invocation and passes it to `bash -c`, but only `patch_args` is shell-quoted — `patch_tool` and the patch file path are interpolated unquoted.

### Finding Description
When a repository rule uses `patches`/`remote_patches` together with a non-default `patch_tool` (or `patch_args` other than plain `-p<N>`, which disables the native-patch fast path), `patch()` constructs: [1](#0-0) 

```
command = "{patchtool} {patch_args} < {patchfile}".format(
    patchtool = patch_tool,
    patchfile = ctx.path(patchfile),
    patch_args = " ".join(["'%s'" % arg for arg in patch_args]),
)
st = ctx.execute([bash_exe, "-c", command], working_directory = patch_directory or "")
```

Only `patch_args` elements are wrapped in single quotes; `patchfile` (the resolved path of the patch file) is embedded into the command string with no quoting or escaping at all. For remote patches, the local filename is derived directly from attacker-influenced input: [2](#0-1) 

```
def _download_patch(ctx, patch_url, integrity, auth = None):
    name = patch_url.split("/")[-1]
    patch_path = ctx.path(_REMOTE_PATCH_DIR).get_child(name)
    download_info = ctx.download(
        patch_url, patch_path, canonical_id = ctx.attr.canonical_id,
        auth = get_auth(ctx, [patch_url]) if auth == None else auth,
        integrity = integrity,
    )
    return patch_path, download_info
```

`name` is simply the last `/`-delimited segment of `patch_url`. `ctx.download`'s `integrity` parameter validates the *bytes* of the downloaded file, but it does nothing to validate or sanitize the *URL string itself* — so a URL segment containing shell metacharacters (e.g. backticks, `$( )`, `;`, spaces) survives unmodified into `patch_path`'s basename and, subsequently, unquoted into the `bash -c` command string built by `patch()`.

`remote_patches` (a URL→integrity map) and `patch_tool`/`patch_args` are attributes that can be populated for a module fetched through Bzlmod (e.g. `http_archive`'s `remote_patches`, used by the registry-patch mechanism, along with `patch_tool`/`patch_args` which can also be supplied by module/registry-controlled overrides). A hostile/compromised registry or mirror that a victim's build resolves against can therefore choose a `remote_patches` URL whose final path segment contains shell metacharacters (the pinned sha256/integrity only binds the patch file's *content*, not the *URL text*), causing arbitrary shell command execution in the fetching repository's working directory during `patch()`.

### Impact Explanation
This is a build-time command injection reachable purely from attacker-served registry/mirror data (an unprivileged, remote party that only needs to serve content the victim's build fetches from). It breaks the invariant that "untrusted content stays data" — the URL text (never covered by a checksum) is used to synthesize an executable shell command. If exploited it results in arbitrary command execution in the context of the Bazel client machine performing the fetch, i.e. well beyond writing files inside the intended repository directory.

### Likelihood Explanation
Exploitation requires: (1) a repository/module that sets a non-default `patch_tool` (or `patch_args` other than a bare `-p<N>`, disabling Bazel's native patch implementation) together with `remote_patches`, and (2) the attacker controlling (or MITM/compromising) the URL text served for that patch entry. This is a fairly narrow but concrete configuration (native patch application is the default and does not go through this shell path), so likelihood is moderate — it depends on projects that explicitly opt into external `patch_tool`/complex `patch_args` while consuming remote/registry patches.

### Recommendation
Quote/escape `patchfile` (and ideally `patch_tool`) before interpolating into the shell command string in `patch()`, e.g. build the argv list directly (`ctx.execute([patch_tool] + patch_args + ["<", ...])` is not directly expressible with shell redirection, so instead pass the patch content via stdin using `ctx.execute(..., ...)` with proper quoting such as `"'%s'" % str(patchfile).replace("'", "'\\''")`), or avoid constructing a shell string entirely by using `bash -c 'exec "$0" "$@" < "$1"'`-style argument passing. Additionally, sanitize/validate the derived filename in `_download_patch` (reject or percent-decode/strip shell-significant characters) rather than using the raw URL segment as a local file name embedded in later shell commands.

### Proof of Concept
Not independently verified with a runnable JUnit/shell-integration test within the available tooling; a proof-of-concept would need a `src/test/shell/bazel/external_patching_test.sh` (or `BuildIntegrationTestCase`) scenario that: (1) serves a `remote_patches` URL such as `http://127.0.0.1:$port/patch%3Btouch%20pwned` (or an unescaped `;`-containing path served by a test HTTP server) with a valid sha256/integrity for its content, (2) sets `patch_tool = "patch"` explicitly (or `patch_args = ["-p1", "--verbose"]`) to force the non-native code path in `patch()`, and (3) asserts that the injected command (`touch pwned`) executed. This construction is inferred from the code shown above and was not executed; it should be validated by a background engineer against a live Bazel build before being treated as confirmed. [3](#0-2)

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
