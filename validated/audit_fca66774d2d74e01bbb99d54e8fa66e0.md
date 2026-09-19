### Title
Missing bounds check in `InputZipFile::ProcessCentralDirEntry` allows out-of-bounds read past the mmap'd input jar - ([File: third_party/ijar/zip.cc])

### Summary
`ijar` (invoked e.g. via `java_common.run_ijar` / `stamp_jar`, and used to strip third-party `.jar` dependencies fetched with `http_jar`/`http_archive`) parses the ZIP central directory of an mmap'd input file using `InputZipFile::ProcessCentralDirEntry`. Unlike the local-file-header parser, this function never checks that attacker-controlled length fields keep the read cursor `p` within the bounds of the mapped file, so a crafted central directory entry can drive reads past the end of the mmap'd region.

### Finding Description
`InputZipFile::ProcessLocalFileEntry` protects every variable-length read with `EnsureRemaining()`, which compares the requested length against `input_file_->Length()`: [1](#0-0) [2](#0-1) 

By contrast, `InputZipFile::ProcessCentralDirEntry` — which reads the Central Directory Header (CDH) fields for every entry, including `file_name_length`, `extra_field_length`, `file_comment_length`, and the file name/extra-field bytes themselves — performs **no equivalent bounds check** against the mapped file length before advancing `p` and dereferencing it: [3](#0-2) 

Specifically:
- `file_name_length`, `extra_field_length`, and `file_comment_length` are attacker-controlled 16-bit values read directly from the CDH.
- `memcpy(filename, p, len)` copies up to `min(file_name_length, PATH_MAX-1)` bytes from `p`, but there is no check that `p + len` (or the subsequent `p += file_name_length`) stays within the mapped file.
- The extra-field loop (`while (extra_p != p) { get_u2le(extra_p); get_u2le(extra_p); ...; extra_p += data_size; }`) reads `header_id`/`data_size` and advances `extra_p` by an attacker-controlled `data_size` with no bound relative to the mapped region, and can also read `get_u8le(extra)` when the ZIP64 extra tag is seen.
- `p += file_comment_length` similarly advances unconditionally.

The only bounds check that exists is a *global* one performed once, when the central directory location is first found: `cd.central_dir_offset + cd.central_dir_size > in_length` is rejected in `FindZipCentralDirectory`. That check validates the aggregate claimed size of the whole central directory against the file length, but it does **not** validate that each individual entry's internal `file_name_length`/`extra_field_length`/`file_comment_length` fields are consistent with the space actually available for that entry. A single entry (especially the last one before the actual end-of-file) can therefore declare lengths that push the cursor `p`/`extra_p` past `in_length`, causing reads from unmapped or unrelated memory adjacent to the `mmap()` region.

This is architecturally identical to the CVE-2019-9325 bug class: a length/index field taken from untrusted media/container data is used to advance a read cursor without verifying it stays inside the buffer, producing an out-of-bounds read.

### Impact Explanation
Because `ijar`/`run_ijar` operates on `.jar` files that can originate from external, attacker-influenced sources (e.g., a `.jar` fetched by `http_jar`/`http_archive`, or Java dependency jars pulled from a registry/mirror), a malicious central directory entry can cause the tool to read memory beyond the mmap'd input file. Depending on the platform's mmap page-alignment behavior this can manifest as:
- A crash (`SIGSEGV`) of the `ijar` build tool if the read crosses into an unmapped page, causing build failures / potential availability impact for the build.
- An out-of-bounds read of adjacent process memory content (bytes copied into the `filename` buffer or consumed in the extra-field loop), which is the class of "remote information disclosure" described in the CVE, since the returned filename or crc/data derived from OOB bytes could theoretically flow into build artifacts.

Note that sha256/integrity checks on the download (as used by `http_jar`) do not mitigate this: they only prove the downloaded bytes match a pinned hash, not that those bytes are safe to parse — the attacker who controls the origin server can craft a malicious `.jar` and simply publish the corresponding sha256 for it.

### Likelihood Explanation
Reaching this code path only requires the victim's build to run `ijar` on any jar whose bytes are attacker-influenced (a common, default operation for `java_import`/`http_jar`-based third-party dependencies, and for `run_ijar`/`stamp_jar` calls in Starlark rules). No special build flags are needed, and no interaction beyond a normal build is required once the dependency is declared — this matches "User interaction is needed for exploitation" (the victim just has to build).

### Recommendation
Add an explicit bounds check (mirroring `EnsureRemaining()` in `ProcessLocalFileEntry`) inside `InputZipFile::ProcessCentralDirEntry` before reading `file_name_length`, `extra_field_length`, and `file_comment_length` bytes, and before/after each step of the extra-field parsing loop, verifying that `p` (and `extra_p`) never advance past `zipdata_in_ + input_file_->Length()`. On violation, the function should call `error(...)` and return `false`/abort parsing, exactly as the local-header path already does.

### Proof of Concept
A reproducible JUnit/`src/test/shell/bazel` test can be constructed as follows (conceptually — the concrete test file needs to be added by an engineer familiar with the ijar test harness):
1. Build a minimal, otherwise-valid ZIP/JAR with one entry in the central directory.
2. Patch that CDH's `file_name_length` (or `extra_field_length`) to a large value (e.g. `0xFFFF`) so that `p + file_name_length` exceeds the actual mmap'd file size, while keeping the aggregate `central_dir_offset + central_dir_size <= in_length` check satisfied (e.g. by placing this crafted entry as the very last entry, right up against EOCD).
3. Run `ijar`/`InputZipFile::ProcessNext()` (or the higher-level `run_ijar` action) on this file and observe either a crash (ASan: heap/mmap out-of-bounds read) or successful extraction containing garbage/adjacent-memory bytes copied into `filename`, demonstrating the missing bounds check identified in `ProcessCentralDirEntry`. [4](#0-3)

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

**File:** third_party/ijar/zip.cc (L493-545)
```text
bool InputZipFile::ProcessCentralDirEntry(const u1 *&p, u8 *compressed_size,
                                          u8 *uncompressed_size, char *filename,
                                          size_t filename_size, u4 *attr,
                                          u8 *offset) {
  u4 signature = get_u4le(p);

  if (signature != CENTRAL_FILE_HEADER_SIGNATURE) {
    if (signature != DIGITAL_SIGNATURE && signature != EOCD_SIGNATURE &&
        signature != ZIP64_EOCD_SIGNATURE) {
      error("invalid central file header signature: 0x%x\n", signature);
    }
    return false;
  }

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
  return true;
}
```

**File:** third_party/ijar/zip.cc (L704-769)
```text
bool FindZipCentralDirectory(const u1 *bytes, size_t in_length, u8 *offset,
                             const u1 **central_dir) {
  static const int MAX_COMMENT_LENGTH = 0xffff;
  static const int CENTRAL_DIR_LOCATOR_SIZE = 22;
  // Maximum distance of start of central dir locator from end of file
  static const int MAX_DELTA = MAX_COMMENT_LENGTH + CENTRAL_DIR_LOCATOR_SIZE;
  const u1* last_pos_to_check = in_length < MAX_DELTA
      ? bytes
      : bytes + (in_length - MAX_DELTA);
  const u1* current;
  bool found = false;

  for (current = bytes + in_length - CENTRAL_DIR_LOCATOR_SIZE;
       current >= last_pos_to_check;
       current-- ) {
    const u1* p = current;
    if (get_u4le(p) != EOCD_SIGNATURE) {
      continue;
    }

    p += 16;  // skip to comment length field
    u2 comment_length = get_u2le(p);

    // Does the comment go exactly till the end of the file?
    if (current + comment_length + CENTRAL_DIR_LOCATOR_SIZE
        != bytes + in_length) {
      continue;
    }

    // Hooray, we found it!
    found = true;
    break;
  }

  if (!found) {
    fprintf(stderr, "file is invalid or corrupted (missing end of central "
                    "directory record)\n");
    return false;
  }

  EndOfCentralDirectoryRecord cd;
  const u1* end_of_central_dir = current;
  get_u4le(current);  // central directory locator signature, already checked
  cd.number_of_this_disk = get_u2le(current);
  cd.disk_with_central_dir = get_u2le(current);
  cd.central_dir_entries_on_this_disk = get_u2le(current);
  cd.central_dir_entries = get_u2le(current);
  cd.central_dir_size = get_u4le(current);
  cd.central_dir_offset = get_u4le(current);
  u2 file_comment_length = get_u2le(current);
  current += file_comment_length;  // set current to the end of the central dir

  if (!FindZip64CentralDirectory(bytes, in_length, &end_of_central_dir, &cd)) {
    return false;
  }

  if (cd.number_of_this_disk != 0 || cd.disk_with_central_dir != 0 ||
      cd.central_dir_entries_on_this_disk != cd.central_dir_entries) {
    fprintf(stderr, "multi-disk JAR files are not supported\n");
    return false;
  }

  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
