### Title
Incomplete path-containment check allows zip archive to write outside extraction root - ([File: third_party/ijar/zip_main.cc])

### Summary
`UnzipProcessor::Process` in the ijar `zipper` tool checks that each extracted zip entry stays inside `output_root_` using a plain string-prefix comparison instead of a proper path-boundary check. Because the comparison does not verify that the character following the shared prefix is a path separator (or end of string), an attacker-controlled zip entry can escape into a sibling directory that merely shares the output root as a string prefix.

### Finding Description
When extracting an archive with `zipper x`, each entry's target path is computed by `concat_path(path, output_root_, output_file_name)` and then validated in `UnzipProcessor::Process`: [1](#0-0) 

The containment check is:
```
std::string normalized_root = normalize_path(output_root_);
std::string normalized = normalize_path(path);
if (normalized.compare(0, normalized_root.size(), normalized_root) != 0) {
  ... abort();
}
``` [2](#0-1) 

This only verifies that `normalized` begins with the literal bytes of `normalized_root`; it never checks that the next character after that prefix is `/` (or that `normalized == normalized_root`). Consequently, if the true output root is e.g. `/workspace/out`, an entry whose resolved path normalizes to `/workspace/out_evil/payload` also satisfies the check, because `"/workspace/out_evil/payload"` starts with the string `"/workspace/out"`. `normalize_path` (via `blaze_util::Path::AsPrintablePath`) only performs `.`/`..` normalization and does not prevent this sibling-directory collision — it does not re-root or bound the path in any way.

This is the exact bug class described by CVE-2024-6287: "incorrect calculation" of a containment/overlap check that "neglects to consider a few cases," letting an attacker who controls the untrusted archive escape the intended write boundary and overwrite/create files outside the designated extraction directory.

`zipper` is invoked by numerous Bazel-provided build rules and internal tooling (e.g., Android/Java packaging, `pkg_zip`-style rules, and other genrule/tool wrappers that call `zipper x` to unpack a zip into a directory tree computed from a target's declared output). Because the destination directory name is often derived from a target name or similar build-controlled string (e.g. `bazel-out/.../foo` vs. an attacker crafting an entry that escapes into `bazel-out/.../foo_backdoor` or a sibling `bazel-out/.../foo/../foo2`), an unprivileged party who can supply the *contents* of a zip file consumed by such a build step (a checked-in or downloaded archive, or a build-time generated zip whose contents come from an external/untrusted source) can place files outside the intended extraction subtree.

### Impact Explanation
A successful bypass lets attacker-supplied zip content be materialized at a path outside the intended output/extraction directory, in the containing build/output tree. This can overwrite files belonging to another target's output directory or plant unexpected files elsewhere under the execroot, which is a containment violation matching the "write outside the repository/exec root/output base" acceptance criterion. Depending on how `zipper`'s output feeds back into the build (e.g. later actions reading from the parent directory, or if the extraction root is close to other sensitive directories), this can result in tainted or unexpected files affecting other build steps.

### Likelihood Explanation
Exploitation requires an attacker to control the byte content of a zip file that is fed to `zipper x -d <output_root> ...` and for there to exist a "sibling" path that shares the output root string as a prefix without a separator boundary (e.g., due to naming patterns like `foo` vs `foo2`, `foo_backup`, etc., which are common in generated output trees, or by crafting a zip entry name containing `../foo_sibling/x`). No credentials, MITM, or privileged access are needed — only the ability to supply the archive's entries. However, actually finding/controlling a naturally-occurring sibling name that matches the target's specific build layout adds some precondition dependent on the concrete tree structure, which is somewhat likely to exist in real build trees (parallel target output directories under a common `bazel-out/<cfg>/bin/<pkg>` prefix).

### Recommendation
Fix the containment check to require an exact boundary match, e.g.:
```cpp
if (normalized != normalized_root &&
    (normalized.size() <= normalized_root.size() ||
     normalized.compare(0, normalized_root.size(), normalized_root) != 0 ||
     normalized[normalized_root.size()] != '/')) {
  ... abort();
}
```
Alternatively, ensure `normalized_root` always ends with a trailing separator before doing the prefix comparison, and compare against `normalized_root + "/"`.

### Proof of Concept
Not confirmed with a runnable, in-repo automated test in this session (no `src/test/*` coverage for `zip_main.cc`'s `UnzipProcessor` containment check was found via search, and only read-only, no-execution tools were available). Conceptually:
1. Create `output_root = "/tmp/out"`.
2. Craft a zip entry whose name, after concatenation and normalization, resolves to `/tmp/out_evil/payload.txt` (e.g. entry name `../out_evil/payload.txt` relative to a root of `/tmp/out`, or simply run `zipper x`ing into a directory `/tmp/out` while the archive entry path is engineered so `concat_path` + `normalize_path` yields `/tmp/out_evil/...`).
3. Run `zipper x archive.zip -d /tmp/out`.
4. Observe that `/tmp/out_evil/payload.txt` is written even though `/tmp/out_evil` is not under `/tmp/out`, because `"/tmp/out_evil".compare(0, strlen("/tmp/out"), "/tmp/out") == 0`.

A `src/test/shell/bazel` script that sets up two sibling directories differing only by a suffix, builds a crafted zip, and invokes the `zipper` binary directly (or via a rule that calls it) would provide a reproducible regression test, following the pattern of `src/test/shell/bazel/check_external_files_test.sh`'s `test_symlink_outside_still_checked` ( [3](#0-2) ), but this was not executed/validated against a live build in this session.

### Citations

**File:** third_party/ijar/zip_main.cc (L129-148)
```text
  if (extract_) {
    char path[PATH_MAX];
    if (!concat_path(path, sizeof(path), output_root_, output_file_name)) {
      abort();
    }
    std::string normalized_root = normalize_path(output_root_);
    std::string normalized = normalize_path(path);
    if (normalized.compare(0, normalized_root.size(), normalized_root) != 0) {
      fprintf(stderr,
              "paths in the zip may not end up outside of the output "
              "directory: %s vs "
              "%s\n",
              path, normalized.c_str());
      abort();
    }
    if (!make_dirs(path, perm) ||
        (!isdir && !write_file(path, perm, data, size))) {
      abort();
    }
  }
```

**File:** src/test/shell/bazel/check_external_files_test.sh (L234-253)
```shellscript
test_symlink_outside_still_checked() {
  mkdir main
  cd main
  setup_module_dot_bazel
  add_rules_shell "MODULE.bazel"
  cat > BUILD <<EOF
load("@rules_shell//shell:sh_test.bzl", "sh_test")
sh_test(name = "symlink", srcs = ["symlink.sh"])
EOF

  mkdir ../foo
  echo 'exit 0' > ../foo/foo.sh
  chmod u+x ../foo/foo.sh
  ln -s ../foo/foo.sh symlink.sh

  bazel test \
    --noexperimental_check_external_repository_files \
    --noexperimental_check_output_files \
    --watchfs \
    :symlink >& "$TEST_log" || fail "Expected build to succeed"
```
