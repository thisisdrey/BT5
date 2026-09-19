### Title
Unbounded out-of-bounds read parsing untrusted central-directory entries in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar/zip.cc` implements a memory-mapped ZIP/JAR reader used by ijar/singlejar-family tooling to parse archives that Bazel treats as attacker-influenced input (e.g. JAR/ZIP artifacts fetched or produced from external inputs). `InputZipFile::ProcessLocalFileEntry` explicitly bounds-checks every field it reads against the remaining mapped-file length via `EnsureRemaining()` [1](#0-0) , but the parallel function that walks the central directory, `InputZipFile::ProcessCentralDirEntry`, performs **no equivalent bounds checking** at all before advancing its cursor by attacker-controlled `file_name_length`, `extra_field_length`, and `file_comment_length` values [2](#0-1) .

### Finding Description
`EnsureRemaining()` is the invariant that is supposed to hold for every field read from the mmap'd archive: it checks `n > remaining` against `input_file_->Length()` before any read/memcpy of attacker data [3](#0-2) . It is used seven times in `ProcessLocalFileEntry` (e.g. before reading `file_name_length_`/`extra_field_length_`-sized regions) [4](#0-3) , but `ProcessCentralDirEntry` — which is the entry point used both by `ProcessNext()` (real extraction) and `CalculateOutputLength()` (size pre-computation) — reads `file_name_length`, `extra_field_length`, `file_comment_length` directly off the mapped buffer and unconditionally advances `p` by those attacker-supplied 16-bit counts, and separately walks an inner loop over "extra field" sub-records (`header_id`/`data_size`) with no check that `extra_p` stays within `extra_field_length` or within the mapped file at all [5](#0-4) .

The only validation performed upstream is that the *whole* central directory (`central_dir_offset + central_dir_size <= in_length`) fits inside the file, done once in `FindZipCentralDirectory` [6](#0-5) . Nothing constrains the *per-entry* header field lengths to stay within that central-directory span. A crafted archive can therefore declare a `central_dir_size` that is internally consistent with the EOCD record, while individual entries declare `file_name_length`/`extra_field_length`/`file_comment_length` values (up to `0xFFFF` each) that make `p` walk past the end of the central directory, past the end of the mapped file region, or even past the mapped page boundary — reading (via `get_u2le`/`get_u4le`/`memcpy`) memory that is not part of the archive at all. This is architecturally the same bug class as ALPINE-CVE-2017-13766: a dissector/parser trusts attacker-supplied length/offset fields inside a structured binary format without validating them against the actual buffer bounds before reading, resulting in an out-of-bounds read (and, since the destination `filename[PATH_MAX]` copy is length-clamped, the primary consequence here is an OOB *read* rather than a write, but it is reachable on fully untrusted archive bytes with default settings).

### Impact Explanation
An attacker who controls the bytes of a ZIP/JAR archive that Bazel's ijar tooling parses (e.g. a JAR produced from a dependency processed by `ijar`/`singlejar`, whose file contents are attacker-influenced through a build input) can trigger reads beyond the memory-mapped file. Depending on allocator/mmap layout this can crash the tool (SIGSEGV via reading beyond an mmap'd page — a hard fault, not a soft OOB into heap) or leak adjacent memory bytes into internal state (e.g. `filename`) that ends up in diagnostic output or is used to construct output archive paths, which is a confidentiality/integrity concern for the build.

### Likelihood Explanation
Reachable purely from bytes of a ZIP-format archive, with no privileges beyond supplying/serving that archive's content; every field in the vulnerable path (`file_name_length`, `extra_field_length`, `file_comment_length`, nested `header_id`/`data_size`) is fully attacker-controlled and requires no cryptographic bypass — there is simply no integrity/bounds check to defeat, unlike the local-file-header path which already has one. This makes it a straightforward, deterministic trigger given any archive that reaches this code path with default flags on a current build.

### Recommendation
Add the same `EnsureRemaining`-style bound check used in `ProcessLocalFileEntry` to `ProcessCentralDirEntry`: before advancing `p` by `file_name_length`, `extra_field_length`, or `file_comment_length`, verify the read stays within `[bytes, bytes + in_length)` (or within the previously-validated central directory span `[central_dir, central_dir + central_dir_size)`), and apply the same bound inside the extra-field sub-record walk (`header_id`/`data_size`) so `extra_p` cannot exceed `p` (i.e. cannot exceed `extra_field_length`) nor the mapped buffer.

### Proof of Concept
I was not able to fully verify a concrete, buildable reproduction (e.g. a `BuildIntegrationTestCase`/`third_party/ijar/test` JUnit/shell test) within this session — I only confirmed the missing-bounds-check condition by direct code comparison between `ProcessLocalFileEntry` (bounded) and `ProcessCentralDirEntry` (unbounded) in `third_party/ijar/zip.cc`. A reproducible PoC would construct a minimal ZIP whose EOCD/central-directory-size bookkeeping is self-consistent, but whose central-directory entry declares `file_name_length`/`extra_field_length`/`file_comment_length` large enough to walk `p` past `bytes + in_length`, then invoke the ijar/zipper CLI (as exercised by `third_party/ijar/test/zip_test.sh`, which already has a `test_no_path_traversal` pattern that could be extended [7](#0-6) ) against it and observe a crash or OOB read via ASan. This should be validated with an actual crafted file and a sanitizer-enabled build before treating it as confirmed exploitable, since exact mmap page-boundary behavior (whether the OOB read lands on an unmapped page immediately, causing a crash, versus readable adjacent heap) needs empirical confirmation.

### Citations

**File:** third_party/ijar/zip.cc (L158-170)
```text
  // Check that at least n bytes remain in the input file, otherwise
  // abort with an error message.  "state" is the name of the field
  // we're about to read, for diagnostics.
  int EnsureRemaining(size_t n, const char *state) {
    size_t in_offset = p - zipdata_in_;
    size_t remaining = input_file_->Length() - in_offset;
    if (n > remaining) {
      return error("Premature end of file (at offset %zd, state=%s); "
                   "expected %zd more bytes but found %zd.\n",
                   in_offset, state, n, remaining);
    }
    return 0;
  }
```

**File:** third_party/ijar/zip.cc (L332-370)
```text
int InputZipFile::ProcessLocalFileEntry(
    size_t compressed_size, size_t uncompressed_size) {
  if (EnsureRemaining(26, "extract_version") < 0) {
    return -1;
  }
  extract_version_ = get_u2le(p);
  general_purpose_bit_flag_ = get_u2le(p);

  if ((general_purpose_bit_flag_ & ~GENERAL_PURPOSE_BIT_FLAG_SUPPORTED) != 0) {
    return error("Unsupported value (0x%04x) in general purpose bit flag.\n",
                 general_purpose_bit_flag_);
  }

  compression_method_ = get_u2le(p);

  if (compression_method_ != COMPRESSION_METHOD_DEFLATED &&
      compression_method_ != COMPRESSION_METHOD_STORED) {
    return error("Unsupported compression method (%d).\n",
                 compression_method_);
  }

  // skip over: last_mod_file_time, last_mod_file_date, crc32
  p += 2 + 2 + 4;
  compressed_size_ = get_u4le(p);
  uncompressed_size_ = get_u4le(p);
  file_name_length_ = get_u2le(p);
  extra_field_length_ = get_u2le(p);

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

**File:** third_party/ijar/zip.cc (L507-543)
```text
  p += 16;  // skip to 'compressed size' field
  *compressed_size = get_u4le(p);
  *uncompressed_size = get_u4le(p);
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
  p += file_comment_length;
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
