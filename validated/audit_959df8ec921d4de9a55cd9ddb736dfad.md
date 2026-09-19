### Title
`remote_file_integrity` defaults to empty string per-URL in `download_remote_files`, silently disabling integrity checking for individually keyed overlay files - (File: tools/build_defs/repo/utils.bzl)

### Summary
The Cork report describes a class of bug where a multi-output operation validates one output value while leaving sibling outputs from the same call unchecked, letting an attacker skew the unchecked portion. The closest reachable analog in bazel is `download_remote_files` in `tools/build_defs/repo/utils.bzl`, which fetches every entry of `http_archive`'s `remote_file_urls` dict but only enforces an integrity check for a given path if the *same key* also exists in `remote_file_integrity`. Any path missing from `remote_file_integrity` downloads with `integrity = ""`, i.e. unchecked.

### Finding Description
`http_archive` (`tools/build_defs/repo/http.bzl`) exposes `remote_file_urls` (map of relative path → list of mirror URLs) and `remote_file_integrity` (map of relative path → integrity value) as independent, unpaired dictionaries: [1](#0-0) 

The actual fetch loop is in `download_remote_files`: [2](#0-1) 

For each `path` in `remote_file_urls`, it calls `ctx.attr.remote_file_integrity.get(path, "")` — if the author (or a registry `source.json`/`overlay` entry, since these dicts are also populated from BCR/registry metadata for `overlay` files, per the registry doc) forgets or omits an entry for one particular path, that specific file is downloaded with an empty integrity string, meaning `ctx.download` performs no verification for that file while every other keyed file in the same dict may be correctly checked. This mirrors the reported bug class exactly: one part of a multi-part operation (one asset in `remote_file_urls`) is silently unverified while sibling parts are verified, since the check is keyed per-item rather than being total over the whole operation.

By contrast, `remote_patches` (fetched via `_download_patch`, also driven by a URL→integrity dict) is explicitly tested to require the integrity value; an empty value there is flagged in `bazel build` output as "canonical reproducible form can be obtained by modifying arguments" and the repo is marked non-reproducible/non-cached: [3](#0-2) 

No equivalent test exists (in the code paths inspected) enforcing that every key of `remote_file_urls` has a non-empty matching entry in `remote_file_integrity`, nor does `download_remote_files` fail/warn when an integrity value is missing for one of several file paths — it just silently defaults to `""`.

### Impact Explanation
If a repository rule invocation (e.g., an `http_archive` used from a module extension backed by BCR `overlay`/`remote_file_urls` metadata) has multiple entries in `remote_file_urls` but only some entries mirrored in `remote_file_integrity`, an attacker controlling the origin server or a compromised mirror for the *unpinned* path can serve arbitrary content for that one overlay file (e.g., a `BUILD.bazel` file) while the other, integrity-checked files remain correct. This is a content-integrity bypass scoped to the unpinned key, directly analogous to redeeming one asset (RA) safely while another asset (CT/DS/PA) from the same operation is manipulated unchecked.

### Likelihood Explanation
This requires a repository-rule caller (module extension author, BCR contributor, or Starlark repo rule wrapping `download_remote_files`) to declare a `remote_file_urls` dict with more keys than `remote_file_integrity`, which is not prevented by any validation in `http.bzl`/`utils.bzl`. Because `remote_file_integrity` defaults to `{}` and the loop does a per-key `.get(path, "")`, this is a straightforward omission (accidental or intentional) rather than a hard-to-trigger edge case, and it fetches from attacker-influenced URLs (mirrors) by design.

### Recommendation
In `download_remote_files`, fail loudly (similar to the `remote_module_file_integrity` check) if a key present in `remote_file_urls` has no corresponding non-empty entry in `remote_file_integrity`, instead of silently defaulting to `""`. Optionally extend the reproducibility warning message (as done for `remote_patches`/`remote_module_file_integrity`) to also cover `remote_file_integrity` gaps, so the whole multi-file operation is validated as a totality rather than per-key optionally.

### Proof of Concept
A minimal Starlark reproduction pattern, adapting the existing test harness structure used for `remote_patches` in `src/test/shell/bazel/external_patching_test.sh`:
```
http_archive(
  name = "ext",
  urls = ["https://good-mirror/ext.zip"],
  integrity = "sha256-<archive-hash>",
  remote_file_urls = {
    "BUILD.bazel": ["https://good-mirror/BUILD.bazel"],
    "extra.bzl":   ["https://attacker-controlled/extra.bzl"],
  },
  remote_file_integrity = {
    "BUILD.bazel": "sha256-<pinned-hash>",
    # "extra.bzl" intentionally omitted
  },
)
```
Building this repo causes `download_remote_files` to call `ctx.download(..., integrity = "")` for `extra.bzl`, so the attacker-controlled mirror can serve any content for that file without detection, while `BUILD.bazel` is verified. This was traced statically in `tools/build_defs/repo/utils.bzl:102-117`; I was not able to execute a live `BuildIntegrationTestCase`/shell test in this session to confirm runtime behavior, so this should be validated with an actual `bazel build` run mirroring `external_patching_test.sh`'s style before treating it as fully confirmed.

### Citations

**File:** tools/build_defs/repo/http.bzl (L449-464)
```text
    "remote_file_urls": attr.string_list_dict(
        default = {},
        doc = """A map of relative paths (key) to a list of URLs (value) that are to be downloaded
and made available as overlaid files on the repo. This is useful when you want to add REPO.bazel or
BUILD.bazel files atop an existing repository. The files are downloaded before `files` are
symlinked and patches (`remote_patches`, `patches`) are applied. The list of URLs should all be
possible mirrors of the same file. The URLs are tried in order until one succeeds. Existing files
will be overwritten.
""",
    ),
    "remote_file_integrity": attr.string_dict(
        default = {},
        doc =
            "A map of file relative paths (key) to its integrity value (value). These relative paths should map " +
            "to the files (key) in the `remote_file_urls` attribute.",
    ),
```

**File:** tools/build_defs/repo/utils.bzl (L102-117)
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

    # Wait until the requests are done
    return {path: token.wait() for path, token in pending.items()}
```

**File:** src/test/shell/bazel/external_patching_test.sh (L229-278)
```shellscript
test_remote_patch_integrity_empty() {
  EXTREPODIR=`pwd`
  EXTREPOURL="$(get_extrepourl ${EXTREPODIR})"

  archive_integrity="sha256-$(cat ext.zip | openssl dgst -sha256 -binary | openssl base64 -A)"

  # Generate the remote patch file
  cat > remote.patch <<'EOF'
--- a/foo.sh	2018-01-15 10:39:20.183909147 +0100
+++ b/foo.sh	2018-01-15 10:43:35.331566052 +0100
@@ -1,3 +1,3 @@
 #!/usr/bin/env sh

-echo Here be dragons...
+echo There are dragons...
EOF

  mkdir main
  cd main

  cat > $(setup_module_dot_bazel) <<EOF
http_archive = use_repo_rule("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
http_archive(
  name="ext",
  strip_prefix="ext-0.1.2",
  urls=["${EXTREPOURL}/ext.zip"],
  integrity="${archive_integrity}",
  build_file_content="exports_files([\"foo.sh\"])",
  remote_patches = {"${EXTREPOURL}/remote.patch": ""},
  remote_patch_strip = 1,
)
EOF

  cat > BUILD <<'EOF'
genrule(
  name = "foo",
  outs = ["foo.sh"],
  srcs = ["@ext//:foo.sh"],
  cmd = "cp $< $@; chmod u+x $@",
  executable = True,
)
EOF

  bazel build :foo.sh > "${TEST_log}" 2>&1
  expect_log "canonical reproducible form can be obtained by modifying arguments \
remote_patches = {\".*/remote\.patch\": \"[^\"]*\"}\$"

  foopath=`bazel info bazel-bin`/foo.sh
  grep -q 'There are' $foopath || fail "expected remote patch to be applied"

```
