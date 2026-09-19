This confirms it: `FindZipCentralDirectory` validates that `cd.central_dir_offset + cd.central_dir_size <= in_length` [1](#0-0) , so the central directory region itself is bounds-checked against the mapped file length. However, that check only bounds the *total* central directory size — it does not validate the *per-entry* variable-length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) read from each central directory record header inside `ProcessCentralDirEntry`.

### Title
Out-of-bounds read in ijar/singlejar's `InputZipFile::ProcessCentralDirEntry` from unchecked attacker-controlled length fields - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from a crafted central directory record and advances/copies from the input pointer `p` by those attacker-controlled amounts without ever checking that `p + length` stays inside the mmap'd input buffer, unlike its sibling `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before every variable-length read [2](#0-1) .

### Finding Description
`ProcessCentralDirEntry` reads the fixed 46-byte central-directory-record header fields and then does: [3](#0-2) 

The `memcpy` into the fixed-size `filename[PATH_MAX]` buffer is capped by `filename_size - 1`, so it cannot smash the destination buffer, but the *source* pointer `p` is advanced and read from without any check that `file_name_length`, `extra_field_length`, or `file_comment_length` (each an attacker-controlled `u2`, up to 65535) actually fit within the remaining mmap'd input region. The subsequent extra-field walk loop is equally unchecked: [4](#0-3) 

Bounds for the *whole* central directory blob are validated once, at `FindZipCentralDirectory` time: `cd.central_dir_offset + cd.central_dir_size > in_length` is rejected [1](#0-0) . That check constrains where the central directory as a whole sits relative to the file, but it does nothing to stop a single record's length fields from claiming more bytes than actually remain before the mapped region ends (e.g., a truncated/malformed final entry, or lengths that push `p` past `bytes + in_length`). Because `MappedInputFile` is a page-aligned `mmap()` of the archive (see `third_party/ijar/mapped_file_unix.cc`), reads past the logical end of file but within the same page silently return stale page data (an information disclosure into `filename`/extra-field parsing), while reads that cross into an unmapped page cause a `SIGSEGV` crash — the same "attacker-supplied length fields drive raw pointer arithmetic/memcpy without a per-field bounds check" pattern as CVE-2022-47089's `gf_vvc_read_sps_bs_internal`, which read structured length-prefixed fields from an untrusted bitstream without validating against the buffer end.

This code is exercised by `third_party/ijar/zip_main.cc`'s `extract()`/`UnzipProcessor`, and more importantly by singlejar/ijar's own use of `ZipExtractor` to build interface jars during normal Bazel builds — this is invoked on `.jar` inputs, including jars fetched from external, potentially hostile sources via rules like `http_jar`/`http_archive`(jar-type)/Maven-resolved dependencies, i.e., "bytes at a dependency URL... that a victim's build consumes."

### Impact Explanation
An attacker who controls a jar file that a victim's build processes with ijar/singlejar (e.g., a malicious dependency artifact) can craft a central directory entry with a `file_name_length`/`extra_field_length`/`file_comment_length` that exceeds the actual remaining mapped bytes. This triggers an out-of-bounds heap/mmap read, either crashing the Bazel build process (denial of the specific build invocation, not the broader sandbox) or leaking adjacent memory-page bytes into the recorded `filename`, which flows into `processor->Accept`/`Process` and ultimately into the output artifact name — a concrete memory-safety violation (C(H)/I(L) territory), not merely a crash.

### Likelihood Explanation
Likelihood is moderate: it requires the victim's build to process a jar (via ijar/singlejar) whose bytes are attacker-influenced (e.g., a compromised or malicious upstream artifact server, since a declared `sha256`/lockfile hash only pins bytes to whatever the attacker chose to publish — it does not validate structural correctness of those bytes). No special build configuration or flags are needed since ijar interface-jar generation runs by default for Java targets.

### Recommendation
Add an `EnsureRemaining`-style bounds check in `ProcessCentralDirEntry` before reading `file_name_length`, `extra_field_length`, and `file_comment_length` bytes from `p`, mirroring the checks already present in `ProcessLocalFileEntry`; likewise bound the per-record extra-field walk (`extra_p`/`data_size`) against `bytes + in_length`.

### Proof of Concept
A reproducible test would be a `BuildIntegrationTestCase`/shell test (in the style of `third_party/ijar/test/zip_test.sh`) that constructs a minimal valid ZIP EOCD/central-directory-size envelope but places, as the final central directory record, a header whose `file_name_length` field claims a value large enough that `p + file_name_length` exceeds the mmap'd file (e.g., truncate the file immediately after the record's fixed header bytes while setting `file_name_length = 0xFFFF`), then runs the `ijar`/`unzip_bootstrap` (`zip_main.cc extract`) binary against it and observes a crash/ASan heap-buffer-overflow report instead of a clean "corrupted file" error, analogous to `test_no_path_traversal` in that file [5](#0-4) .

### Citations

**File:** third_party/ijar/zip.cc (L360-370)
```text
  if (EnsureRemaining(file_name_length_, "file_name") < 0) {
    return -1;
  }
  file_name_ = p;
  p += file_name_length_;

  if (EnsureRemaining(extra_field_length_, "extra_field") < 0) {
    return -1;
  }
  extra_field_ = p;
  p += extra_field_length_;
```

**File:** third_party/ijar/zip.cc (L510-525)
```text
  u2 file_name_length = get_u2le(p);
  u2 extra_field_length = get_u2le(p);
  u2 file_comment_length = get_u2le(p);
  p += 4;  // skip to external file attributes field
  *attr = get_u4le(p);
  *offset = get_u4le(p);
  {
    size_t len = (file_name_length < filename_size)
      ? file_name_length
      : (filename_size - 1);
    memcpy(reinterpret_cast<void*>(filename), p, len);
    filename[len] = 0;
  }
  p += file_name_length;
  const u1 *extra_p = p;
  p += extra_field_length;
```

**File:** third_party/ijar/zip.cc (L526-542)
```text
  while (extra_p != p) {
    const u2 header_id = get_u2le(extra_p);
    const u2 data_size = get_u2le(extra_p);
    const u1 *extra = extra_p;
    extra_p += data_size;
    if (header_id == ZIP64_EXTRA_FIELD_TAG) {
      if (*uncompressed_size == U4_MAX) {
        *uncompressed_size = get_u8le(extra);
      }
      if (*compressed_size == U4_MAX) {
        *compressed_size = get_u8le(extra);
      }
      if (*offset == U4_MAX) {
        *offset = get_u8le(extra);
      }
    }
  }
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
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
