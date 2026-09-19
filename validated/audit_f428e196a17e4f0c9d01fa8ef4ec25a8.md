### Title
Path-Traversal via Non-Boundary-Checked Prefix Comparison in ijar's zip extractor - (File: `third_party/ijar/zip_main.cc`)

### Summary
Bazel's `ijar`/`zipper` zip extraction code (`UnzipProcessor::Process` in `third_party/ijar/zip_main.cc`) validates that an extracted entry stays inside the target output directory using a raw string-prefix comparison (`std::string::compare`) instead of a boundary-aware containment check. This mirrors exactly the bug class in the LiteStar advisory (`commonpath`/prefix check without verifying a directory-separator boundary), which allows a crafted archive entry name to escape the intended output directory into a sibling directory whose name merely shares the same prefix.

### Finding Description
`UnzipProcessor::Process` builds the destination path from `output_root_` (fixed, trusted) and `output_file_name` (attacker-controlled, taken directly from the zip's central directory filename field): [1](#0-0) 

The containment check is:
```
std::string normalized_root = normalize_path(output_root_);
std::string normalized = normalize_path(path);
if (normalized.compare(0, normalized_root.size(), normalized_root) != 0) {
  ... abort();
}
```
This is the identical anti-pattern flagged in the LiteStar advisory: comparing only the leading N characters of the normalized path against the trusted root, without confirming that character N is a path separator (or that the path equals the root exactly). Because of this, a normalized path such as `/home/user/output_backup/evil` will be judged "inside" the root `/home/user/output`, since the check only compares the first `len("/home/user/output")` characters, and `"_backup/evil"` is simply ignored. An attacker who controls zip entry names (e.g., `../output_backup/evil`) can therefore produce a `normalize_path(path)` result that has `output_root_` as a literal string prefix while actually resolving to a sibling directory outside the intended extraction root — the same class of bypass as the `commonpath([str(directory), file_info["name"], joined_path])` flaw in the report. [2](#0-1) 

The existing regression test (`test_no_path_traversal`) only exercises a `../` traversal that escapes to a fixed sibling folder name (`ZIPPER_POC_OWNED`) and does not test the "sibling-directory-with-shared-prefix" bypass, so it would not catch this specific containment-check flaw: [3](#0-2) 

### Impact Explanation
This zip extractor is used by Bazel's own `zipper` tool. Where invoked against an attacker-supplied/untrusted zip archive with a crafted entry name, the prefix-only check can be bypassed to write files into a directory located outside — but adjacent to — the intended extraction root, provided an attacker can predict or control a sibling directory name at that location. This is a local file write outside the declared output directory, breaking the "containment holds" invariant for archive extraction, matching the report's "Path Traversal" bug class.

### Likelihood Explanation
Exploitation requires: (1) an attacker who can supply the content of a zip archive that `zipper`/`ijar`'s extractor processes, and (2) a scenario where the sibling directory that shares the root's string prefix already exists or can be created by the same operation stream. This is a narrower precondition than a full unrestricted traversal (unlike the CVE's simpler `../../../etc/shadow` case), which lowers likelihood somewhat, but the underlying validation logic is provably incorrect and directly analogous to the reported vulnerability class — it is a latent containment-check bug rather than a purely theoretical one.

### Recommendation
Replace the raw `compare(0, n, ...)` prefix check in `UnzipProcessor::Process` with a boundary-aware containment check: verify that `normalized == normalized_root` or that `normalized.compare(0, normalized_root.size(), normalized_root) == 0 && normalized[normalized_root.size()] == '/'` (handling trailing-slash normalization of `normalized_root` first). This mirrors how Starlette correctly checks `os.path.commonpath([full_path, directory]) == directory` (which internally reasons in path-segments, not raw strings) rather than a naive substring compare.

### Proof of Concept
A `BuildIntegrationTestCase`/shell test analogous to `third_party/ijar/test/zip_test.sh`'s existing `test_no_path_traversal` could be extended:
1. Create output directory `output_root/out`.
2. Also create a sibling directory `output_root/out_backup` (simulating a name an attacker can predict, e.g. via a previous benign extraction step or symlink).
3. Craft a zip archive with entry name `../out_backup/evil` (so that after `concat_path` + `normalize_path`, the resulting path is `output_root/out_backup/evil`).
4. Run `zipper x crafted.zip -d out`.
5. Observe that the containment check `normalized.compare(0, normalized_root.size(), normalized_root)` passes (since `"output_root/out_backup/evil"` has `"output_root/out"` as a literal prefix) and the file `output_root/out_backup/evil` is written, i.e., outside the intended `output_root/out` directory — confirming the boundary-check bypass.

### Citations

**File:** third_party/ijar/zip_main.cc (L105-128)
```text
void UnzipProcessor::Process(const char* filename, const u4 attr,
                             const u1* data, const size_t size) {
  mode_t perm = zipattr_to_perm(attr);
  bool isdir = zipattr_is_dir(attr);
  const char *output_file_name = filename;
  if (attr == 0) {
    // Fallback when the external attribute is not set.
    isdir = filename[strlen(filename)-1] == '/';
    perm = 0777;
  }

  if (flatten_) {
    if (isdir) {
      return;
    }
    const char *p = strrchr(filename, '/');
    if (p != NULL) {
      output_file_name = p + 1;
    }
  }

  if (verbose_) {
    printf("%c %o %s\n", isdir ? 'd' : 'f', perm, output_file_name);
  }
```

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
