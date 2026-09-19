This is the exact analog: `http_archive`'s main archive requires an integrity/sha256 check that binds the downloaded bytes to a pinned digest, but `remote_file_integrity` (secondary artifacts overlaid onto the repo, e.g. `REPO.bazel`/`BUILD.bazel` files fetched from `remote_file_urls`) is allowed to default to an empty string, which silently disables verification for those specific files while the archive itself remains checked — mirroring the Sherlock finding where the primary currency is checked but secondary currencies are not.

### Title
Secondary artifacts fetched via `remote_file_urls`/`remote_file_integrity` bypass integrity verification while the primary archive is checked - ([File: tools/build_defs/repo/utils.bzl])

### Summary
`http_archive`'s primary source archive is fetched with a mandatory-by-convention `integrity`/`sha256` argument passed straight into `ctx.download_and_extract`, and Bazel enforces the digest match before the archive is trusted [1](#0-0) . But the same rule also supports overlaying additional files fetched from attacker-influencable `remote_file_urls`, and the integrity check for each of those files is looked up from `remote_file_integrity` with a default of the empty string when no entry is provided [2](#0-1) .

### Finding Description
`download_remote_files()` in `tools/build_defs/repo/utils.bzl` iterates `ctx.attr.remote_file_urls` and calls `ctx.download()` for each path with `integrity = ctx.attr.remote_file_integrity.get(path, "")` [2](#0-1) . If the caller (a module extension, a BCR `source.json`-derived `http_archive` invocation, or a hand-written rule) does not populate `remote_file_integrity` for a given path — or explicitly sets it to `""` — the download proceeds with no expected checksum at all, exactly analogous to how the empty-string default was demonstrated to be accepted in `test_overlay_remote_file_with_empty_integrity` [3](#0-2) .

These overlaid files are not decorative: they are symlinked/written on top of the extracted, already-integrity-verified archive contents and can include `BUILD.bazel`/`REPO.bazel` files that define build semantics for the repository [4](#0-3) . Because `remote_file_integrity` is per-path and optional, a registry/extension author (or a hostile server controlling one of the `remote_file_urls` mirrors) can supply a path with a missing or blank integrity entry while every other artifact (the main archive, and separately-pinned `remote_patches`) is fully checksum-pinned — the invariant "all fetched bytes are bound to a pinned digest" holds for the primary artifact but is silently unenforced for this secondary artifact class, mirroring the Notional bug where `assetToken`/`underlyingToken` checks applied to the primary currency but not to `secondaryBorrowCurrencies`.

### Impact Explanation
An attacker who controls (or MITMs/compromises) one of the URLs in `remote_file_urls` for a path lacking a corresponding non-empty `remote_file_integrity` entry can substitute arbitrary content — including a `BUILD.bazel` file — that lands unauthenticated inside an otherwise checksum-verified repository. Since these overlay files are written after archive extraction and patching [5](#0-4) , this can alter build definitions/build outputs of the repository despite the archive's own hash being correct, without any error or warning distinct from the normal reproducibility-tracking mechanism (`_update_http_archive_integrity_attrs`, which only overrides the attr for future runs, it does not fail the current run) [6](#0-5) .

### Likelihood Explanation
This requires that a rule author (extension writer, macro, or BCR module) populate `remote_file_urls` but omit (or leave empty) the matching `remote_file_integrity` entry for at least one path — a plausible oversight given the API allows partial/empty maps by default and Bazel does not fail hard the first time; it only prints a note about the "canonical reproducible form" for `bazel mod tidy`/attrs-for-reproducibility purposes [7](#0-6) .

### Recommendation
Require a non-empty `remote_file_integrity` entry for every path present in `remote_file_urls` in `download_remote_files()`, failing the build (similar to how `remote_module_file_integrity` is already enforced as mandatory when `remote_module_file_urls` is set) [8](#0-7) , instead of silently defaulting to `""`.

### Proof of Concept
The existing shell test `test_overlay_remote_file_with_empty_integrity` demonstrates the exact bypass path: it configures `http_archive` with `remote_file_integrity = {"REPO.bazel": "<real hash>", "BUILD.bazel": ""}` and shows the build succeeds and only warns about reproducibility, then shows the overlay content silently changes across a `bazel clean --expunge` without any integrity failure, because `BUILD.bazel`'s integrity was empty [9](#0-8) . A malicious dependency-URL host serving different bytes for that same path with the recorded empty integrity would be undetected while the main archive's own `integrity` continued to be enforced.

### Citations

**File:** tools/build_defs/repo/http.bzl (L180-194)
```text
def _update_http_archive_integrity_attrs(ctx, attrs, integrity):
    integrity_override = {}

    # We don't need to override the integrity attribute if sha256 is already specified.
    # remote_module_file_integrity is for internal use by Bazel only and always
    # set correctly.
    if not ctx.attr.sha256 and not ctx.attr.integrity:
        integrity_override["integrity"] = integrity.archive
    if ctx.attr.remote_file_integrity != integrity.remote_files:
        integrity_override["remote_file_integrity"] = integrity.remote_files
    if ctx.attr.remote_patches != integrity.remote_patches:
        integrity_override["remote_patches"] = integrity.remote_patches
    if not integrity_override:
        return ctx.repo_metadata(reproducible = True)
    return ctx.repo_metadata(attrs_for_reproducibility = update_attrs(ctx.attr, attrs.keys(), integrity_override))
```

**File:** tools/build_defs/repo/http.bzl (L204-215)
```text
    source_urls = _get_source_urls(ctx)
    download_info = ctx.download_and_extract(
        source_urls,
        ctx.attr.add_prefix,
        ctx.attr.sha256,
        ctx.attr.type,
        ctx.attr.strip_prefix,
        strip_components = ctx.attr.strip_components,
        canonical_id = ctx.attr.canonical_id or get_default_canonical_id(ctx, source_urls),
        auth = get_auth(ctx, source_urls),
        integrity = ctx.attr.integrity,
    )
```

**File:** tools/build_defs/repo/http.bzl (L216-220)
```text
    workspace_and_buildfile(ctx)

    remote_files_info = download_remote_files(ctx)
    remote_patches_info = patch(ctx)
    symlink_files(ctx)
```

**File:** tools/build_defs/repo/http.bzl (L426-432)
```text
    "files": attr.string_keyed_label_dict(
        doc = """A map of relative paths (key) to a file label (value) that overlaid on the repo as
a symlink. This is useful when you want to add REPO.bazel or BUILD.bazel files atop an existing
repository. Files are symlinked after remote files are downloaded and patches (`remote_patches`,
`patches`) are applied. Existing files will be overwritten.
""",
    ),
```

**File:** tools/build_defs/repo/utils.bzl (L102-114)
```text
    pending = {
        path: ctx.download(
            remote_file_urls,
            path,
            canonical_id = ctx.attr.canonical_id,
            auth = get_auth(ctx, remote_file_urls) if auth == None else auth,
            integrity = ctx.attr.remote_file_integrity.get(path, ""),
            block = False,
            # Overlaid files may be shell scripts.
            executable = True,
        )
        for path, remote_file_urls in ctx.attr.remote_file_urls.items()
    }
```

**File:** tools/build_defs/repo/utils.bzl (L238-248)
```text
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
```

**File:** src/test/shell/bazel/external_remote_file_test.sh (L247-300)
```shellscript
test_overlay_remote_file_with_empty_integrity() {
  EXTREPODIR=`pwd`
  EXTREPOURL="$(get_extrepourl ${EXTREPODIR})"

  archive_integrity="sha256-$(cat hello_world.zip | openssl dgst -sha256 -binary | openssl base64 -A)"

  # Generate the remote files to overlay
  cat > BUILD.bazel <<'EOF'
load("@rules_cc//cc:defs.bzl", "cc_binary")

cc_binary(
    name = "hello_world",
    srcs = ["hello_world.c"],
)
EOF
  touch REPO.bazel

  mkdir main
  cd main
  cat >> $(setup_module_dot_bazel) <<EOF
http_archive = use_repo_rule("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
http_archive(
  name="hello_world",
  strip_prefix="hello_world-0.1.2",
  urls=["${EXTREPOURL}/hello_world.zip"],
  integrity="${archive_integrity}",
  remote_file_urls={
    "REPO.bazel": ["${EXTREPOURL}/REPO.bazel"],
    "BUILD.bazel": ["${EXTREPOURL}/BUILD.bazel"],
  },
  remote_file_integrity={
    "REPO.bazel": "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=",
    "BUILD.bazel": "",
  },
)
EOF
  add_rules_cc "MODULE.bazel"

  bazel build @hello_world//:hello_world > "${TEST_log}" 2>&1
  expect_log "canonical reproducible form can be obtained by modifying arguments \
remote_file_integrity = {\"REPO\.bazel\": \"[^\"]*\", \"BUILD\.bazel\": \"[^\"]*\"}\$"

  # Check that repo is not marked as reproducible and cached

  bazel clean --expunge

  # Modify the remote files to overlay
  cat > ../BUILD.bazel <<'EOF'
load("@rules_cc//cc:defs.bzl", "cc_binary")

cc_binary(
    name = "goodbye_world",
    srcs = ["hello_world.c"],
)
```
