### Title
Path-traversal in ijar `zipper` extraction via sibling-directory prefix bypass in `UnzipProcessor::Process` - ([File: third_party/ijar/zip_main.cc])

### Summary
The `zipper` tool's extraction containment check in `third_party/ijar/zip_main.cc` verifies that an extracted entry stays inside the output root using a bare string-prefix comparison, without checking for a trailing path-separator boundary. This is the same bug class as CVE-2026-81716 (`PluginSandbox._is_safe_path` doing a bare prefix match instead of an exact/separator-bounded match), and it allows a malicious zip/jar archive to write files into a sibling directory whose name merely shares the output root as a string prefix.

### Finding Description
`UnzipProcessor::Process` computes the destination path for each zip entry and checks containment like this: [1](#0-0) 

```
std::string normalized_root = normalize_path(output_root_);
std::string normalized = normalize_path(path);
if (normalized.compare(0, normalized_root.size(), normalized_root) != 0) {
  ...
  abort();
}
```

`normalized.compare(0, normalized_root.size(), normalized_root)` only checks that `normalized` *starts with* the characters of `normalized_root` — it does not require the next character after the prefix to be a path separator (or that the two strings are equal). This is exactly the `PluginSandbox._is_safe_path`-style flaw described in the external report: "authorized file access using a bare string-prefix match ... breaking isolation when directories merely share a name prefix."

The path fed into this check is built by `concat_path(path, sizeof(path), output_root_, output_file_name)`: [2](#0-1) 

Because `concat_path` always inserts a `/` between `output_root_` and the (attacker-controlled) entry name when the entry name doesn't already start with `/`, the pre-normalization path is always `output_root_ + "/" + entry_name`. However, if `entry_name` contains `..` components (e.g. `../out_evilsibling/payload`), `normalize_path` (which uses `blaze_util::Path::AsPrintablePath`) resolves the `..` and can collapse the path to something like `/tmp/out_evilsibling/payload` when `output_root_` is `/tmp/out`. The prefix check `normalized.compare(0, strlen("/tmp/out"), "/tmp/out")` still succeeds because `/tmp/out_evilsibling/payload` textually starts with `/tmp/out`, even though the resolved path is a completely different, sibling directory outside the intended extraction root.

### Impact Explanation
An attacker who controls the contents of a `.zip`/`.jar` archive that a victim extracts with the `zipper` binary (or any consumer of `UnzipProcessor`, i.e. Bazel's own ijar/zip extraction tool used for jar/zip manipulation in the build) can use a crafted entry name containing `..` segments to write files into a directory that is a sibling of, but not contained within, the intended output root — bypassing the explicit containment check ("paths in the zip may not end up outside of the output directory"). This is a concrete write-outside-of-the-output-directory primitive driven purely by attacker-supplied archive content, matching the "write outside repository/exec root/output base" acceptance criterion for this class of bug.

### Likelihood Explanation
Medium. Exploitation requires the attacker to control an archive that is extracted through this specific `zipper`/`UnzipProcessor` code path with a predictable/guessable output-root naming pattern (so that a sibling directory name overlapping the root's prefix can be pre-arranged or is otherwise attacker-influenced). The `test_no_path_traversal` shell test in `third_party/ijar/test/zip_test.sh` only exercises the classic `../` full-escape case and does not cover the sibling-prefix bypass, so the existing test suite would not catch this variant.

### Recommendation
Change the containment check to require an exact match or a match followed by a path separator, mirroring the fix in the referenced CVE and the already-correct pattern used elsewhere in this codebase (`src/main/starlark/builtins_bzl/common/paths.bzl`'s `_starts_with`, which explicitly checks `norm_a[len(norm_b)] == "/"`):

```cpp
if (normalized.compare(0, normalized_root.size(), normalized_root) != 0 ||
    (normalized.size() > normalized_root.size() &&
     normalized[normalized_root.size()] != '/')) {
  ...
}
```

### Proof of Concept
Extend `third_party/ijar/test/zip_test.sh`'s `test_no_path_traversal` with a sibling-directory case:
1. Create an output directory `out` for extraction (`output_root_` = `.../out`).
2. Pre-create (or have the attacker's archive imply) a sibling marker path `out_evil/OWNED` reachable via an entry name such as `../out_evil/OWNED` inside a crafted zip.
3. Run `${ZIPPER} x crafted.jar -d out`.
4. Assert the extraction does **not** abort and that `out_evil/OWNED` (a path outside `out`) is created — demonstrating the prefix check failed to block extraction outside the output root. [3](#0-2)

### Citations

**File:** third_party/ijar/zip_main.cc (L86-103)
```text
bool concat_path(char *out, const size_t size, const char *path1,
                 const char *path2) {
  int len1 = strlen(path1);
  size_t l = len1;
  strncpy(out, path1, size - 1);
  out[size - 1] = 0;
  if (l < size - 1 && path1[len1] != '/' && path2[0] != '/') {
    out[l] = '/';
    l++;
    out[l] = 0;
  }
  if (l >= size - 1) {
    fprintf(stderr, "paths too long to concat: %s + %s", path1, path2);
    return false;
  }
  strncat(out, path2, size - 1 - l);
  return true;
}
```

**File:** third_party/ijar/zip_main.cc (L129-143)
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
```

**File:** third_party/ijar/test/zip_test.sh (L277-283)
```shellscript
function test_no_path_traversal() {
  local folder=$(mktemp -d ${TEST_TMPDIR}/output.XXXXXXXX)
  ! (cd $folder && $ZIPPER x $(dirname ${ZIPPER})/test/path_traversal_zip.jar)
  if [[ -e ${folder}/../ZIPPER_POC_OWNED ]]; then
    fail "Path traversal succeeded"
  fi
}
```
