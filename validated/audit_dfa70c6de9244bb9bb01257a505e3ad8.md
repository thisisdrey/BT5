Based on my research into Bazel's patch-application code paths:

**No vulnerability found for this question.**

Rationale (not part of the required strict output, but for transparency): Bazel's `patch()` helper in [1](#0-0)  has two code paths for applying patches. When `patch_tool` is unset, Bazel uses its own native patch implementation via `ctx.patch()` [2](#0-1) , which — per Bazel's documentation — only supports the standard unified-diff format and explicitly does not support fuzz matching or other GNU patch extensions [3](#0-2) ; ed-script syntax (the `do_ed_script`/`!command` bug class from CVE-2018-20969) is not part of that format and is not parsed by Bazel's own implementation, so the bug class is unreachable there.

When a `patch_tool` is explicitly configured, Bazel instead shells out to that external binary via `bash -c` [4](#0-3) . Any ed-script vulnerability in that scenario would live in the third-party `patch` binary itself (the actual GNU patch CVE), which the rules explicitly exclude ("reject analogs that reduce to ... third-party dependency CVEs"). Additionally, selecting a vulnerable system `patch_tool` is a trusted build-author configuration choice made in root-repo Starlark/BUILD, not something an unprivileged attacker (a hostile URL/archive/registry host) can control while remote patch content is still integrity-pinned via `remote_patches` sha256/integrity checks in `_download_patch` [5](#0-4) , so this also falls under the excluded "running untrusted root-repo Starlark" / trusted-configuration category.

No standalone Bazel-native ed-script parser or equivalent `do_ed_script`-style logic exists in the indexed patch/extraction code (`PatchUtil`, `ZipDecompressor`, `TarFunction`, etc.) that would independently reproduce this bug class.

### Citations

**File:** tools/build_defs/repo/utils.bzl (L143-271)
```text
def patch(ctx, patches = None, patch_cmds = None, patch_cmds_win = None, patch_tool = None, patch_args = None, auth = None, patch_directory = None):
    """Implementation of patching an already extracted repository.

    This rule is intended to be used in the implementation function of
    a repository rule. If the parameters `patches`, `patch_tool`,
    `patch_args`, `patch_cmds`, `patch_cmds_win` and `patch_directory`
    are not specified then they are taken from `ctx.attr`.

    Args:
      ctx: The repository context of the repository rule calling this utility
        function.
      patches: The patch files to apply. List of strings, Labels, or paths.
      patch_cmds: Bash commands to run for patching, passed one at a
        time to bash -c. List of strings
      patch_cmds_win: Powershell commands to run for patching, passed
        one at a time to powershell /c. List of strings. If the
        boolean value of this parameter is false, patch_cmds will be
        used and this parameter will be ignored.
      patch_tool: Path of the patch tool to execute for applying
        patches. String.
      patch_args: Arguments to pass to the patch tool. List of strings.
      auth: An optional dict specifying authentication information for some of the URLs.
      patch_directory: Directory relative to the repository root in which to
        apply `patches` and run `patch_cmds`. Remote patches are always applied
        at the repository root. Defaults to the repository root.

    Returns:
        dict mapping remote patch URLs to a download info.
    """
    bash_exe = ctx.os.environ["BAZEL_SH"] if "BAZEL_SH" in ctx.os.environ else "bash"
    powershell_exe = ctx.os.environ["BAZEL_POWERSHELL"] if "BAZEL_POWERSHELL" in ctx.os.environ else "powershell.exe"

    if patches == None:
        patches = []
    if hasattr(ctx.attr, "patches") and ctx.attr.patches:
        patches += ctx.attr.patches

    remote_patches = {}
    remote_patch_strip = 0
    if hasattr(ctx.attr, "remote_patches") and ctx.attr.remote_patches:
        if hasattr(ctx.attr, "remote_patch_strip"):
            remote_patch_strip = ctx.attr.remote_patch_strip
        remote_patches = ctx.attr.remote_patches

    if patch_cmds == None and hasattr(ctx.attr, "patch_cmds"):
        patch_cmds = ctx.attr.patch_cmds
    if patch_cmds == None:
        patch_cmds = []

    if patch_cmds_win == None and hasattr(ctx.attr, "patch_cmds_win"):
        patch_cmds_win = ctx.attr.patch_cmds_win
    if patch_cmds_win == None:
        patch_cmds_win = []

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

    if patch_directory == None and hasattr(ctx.attr, "patch_directory"):
        patch_directory = ctx.attr.patch_directory

    if len(remote_patches) > 0 or len(patches) > 0 or len(patch_cmds) > 0:
        ctx.report_progress("Patching repository")

    # Apply remote patches
    remote_patches_download_info = {}
    for patch_url in remote_patches:
        integrity = remote_patches[patch_url]
        patchfile, download_info = _download_patch(ctx, patch_url, integrity, auth)
        remote_patches_download_info[patch_url] = download_info
        ctx.patch(patchfile, remote_patch_strip)
        ctx.delete(patchfile)
    ctx.delete(ctx.path(_REMOTE_PATCH_DIR))

    # Support for the remote_module_file_urls attribute, which is only meant for
    # internal use by Bazel when defining a Bazel module repository.
    # Download the module file after applying remote (i.e., registry) patches
    # since modules may decide to patch their packaged module and the patch may
    # not apply to the file checked in to the registry.
    # Download the module file before applying local patches since users should
    # still be able to modify it via a single_version_override.
    remote_module_file_urls = getattr(ctx.attr, "remote_module_file_urls", [])
    if remote_module_file_urls:
        if not ctx.attr.remote_module_file_integrity:
            fail("remote_module_file_integrity must be set when remote_module_file_urls is set")
        ctx.delete("MODULE.bazel")
        ctx.download(
            remote_module_file_urls,
            "MODULE.bazel",
            auth = get_auth(ctx, ctx.attr.remote_module_file_urls),
            integrity = ctx.attr.remote_module_file_integrity,
        )

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

**File:** docs/versions/7.7.1/rules/lib/builtins/repository_ctx.mdx (L199-202)
```text
None repository_ctx.patch(patch_file, strip=0, *, watch_patch='auto')
```

Apply a patch file to the root directory of external repository. The patch file should be a standard  [unified diff format](https://en.wikipedia.org/wiki/Diff#Unified_format) file. The Bazel-native patch implementation doesn't support fuzz match and binary patch like the patch command line tool.
```
