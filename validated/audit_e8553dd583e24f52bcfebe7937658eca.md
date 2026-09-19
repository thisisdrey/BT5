### Title
Heap-buffer-overflow read from unbounded ZIP central-directory field parsing in ijar - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses a ZIP Central Directory Header's variable-length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) and advances the read cursor `p`/`extra_p` by these attacker-controlled 16-bit values without ever checking that the resulting pointer stays inside the memory-mapped input file, unlike the sibling local-header parser which explicitly bounds-checks with `EnsureRemaining()`.

### Finding Description
`ProcessLocalFileEntry` (third_party/ijar/zip.cc:332-418) calls `EnsureRemaining()` before every variable-length read (filename, extra field), guarding against reading past the end of the mapped buffer: [1](#0-0) 

By contrast, `ProcessCentralDirEntry`, which is the function used to walk every entry of the Central Directory when computing output size (`CalculateOutputLength`) and when extracting entries (`ProcessNext`), performs no equivalent bounds validation. It reads `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, and `offset` directly via `get_u4le`/`get_u2le`, and then does an unchecked `memcpy` of `file_name_length` bytes from `p`, and further advances `p` by `file_name_length`, `extra_field_length`, and `file_comment_length`: [2](#0-1) 

The code comment at line 491-492 asserts "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" — but this assumption only holds for a well-formed archive. A maliciously crafted (but internally self-consistent, e.g. size fields matching, checksum matching) `.jar`/`.zip` file can set `file_name_length`, `extra_field_length`, or `file_comment_length` to values (up to 0xFFFF each) that push `p` past the actual mapped file bounds. The subsequent `memcpy(filename, p, len)` at line 520 or the next iteration's `get_u4le(p)` signature read then dereferences memory beyond the `mmap`'d region — a heap/mapped-buffer over-read, the same bug class as CVE-2018-14531's out-of-bounds read while parsing a length-prefixed atom (`AP4_HvccAtom`) without validating declared sizes against the buffer.

`FindZipCentralDirectory` (third_party/ijar/zip.cc:704-778) validates that `central_dir_offset + central_dir_size <= in_length`, i.e., it bounds the *aggregate* central directory region, but it never validates the internal per-entry length fields against the remaining bytes in that region, so a single entry can claim a `file_name_length`/`extra_field_length`/`comment_length` that overruns the mapped file end (or wraps past the file, into another mapping, or into an unmapped guard page, causing a SIGSEGV/ASAN heap-buffer-overflow crash).

### Impact Explanation
ijar is invoked by Bazel to build interface jars from source/dependency jars (e.g., via `java_import`, prebuilt `.jar` deps, or jars produced by `http_jar`/`http_archive` extraction pipelines feeding into Java rules). A build that consumes a jar published by an untrusted party — matching whatever checksum the victim pinned for it, since the attacker controls the artifact from the start — will invoke `ijar` on that content. A malicious jar with corrupted central-directory length fields can crash the ijar tool via out-of-bounds read (denial of service of the build), and in an ASAN/hardened build this manifests exactly as the reported bug class: heap-buffer-overflow on read while parsing an atom/record whose declared length is trusted without a bounds check.

### Likelihood Explanation
Any build that includes a prebuilt `.jar` dependency (a common, everyday pattern: `java_import`, Maven-fetched jars, `http_jar`) exercises this code path with attacker-influenced bytes. No special build flags are required — `ijar` runs by default whenever Bazel needs an interface jar for Java compilation. The only requirement is that the attacker (as the origin of the dependency) can craft a `.zip`/`.jar` whose central directory entries have oversized length fields while still satisfying the pinned checksum (trivial, since the attacker is the original publisher).

### Recommendation
Add explicit remaining-bytes bounds checks in `InputZipFile::ProcessCentralDirEntry` (analogous to `EnsureRemaining()` in `ProcessLocalFileEntry`) before reading/advancing past `file_name_length`, `extra_field_length`, and `file_comment_length`, and validate that `p` never exceeds `zipdata_in_ + input_file_->Length()`. Reject (with a diagnostic error) any central directory entry whose declared field lengths would read past the mapped file boundary.

### Proof of Concept
A reproducible JUnit/`BuildIntegrationTestCase`-style proof would construct a minimal ZIP file (similar to the existing `CreateZipWithMalformedExtraField()` helper found in `src/tools/singlejar/output_jar_simple_test.cc`, which demonstrates the equivalent, already-mitigated pattern for `singlejar`'s `zip_headers.h`) but targeting `third_party/ijar`:
1. Write a minimal local file header for one entry.
2. Write a Central Directory Header where `file_name_length` (or `extra_field_length`/`comment_length`) is set to a large value (e.g., 0xFFFF) that would place the read cursor past the end of the file buffer, while the End-Of-Central-Directory record's `central_dir_size`/`central_dir_offset` are kept internally consistent so `FindZipCentralDirectory`'s aggregate check passes.
3. Run ijar (`ZipExtractor::Create` / `ProcessAll`) on this file under ASAN.
4. Observe a heap-buffer-overflow/out-of-bounds read report when `ProcessCentralDirEntry`'s `memcpy` or subsequent `get_u4le` dereferences memory past the mapped file.

Note: I was unable to fully trace `MappedInputFile`'s exact allocation/guard-page behavior (its implementation in `third_party/ijar/mapped_file.h`/platform-specific `.cc` was not returned by search), so I cannot confirm with full certainty whether the overrun always lands in an unmapped page (crash) versus adjacent heap/mapped memory (silent over-read) on every platform; this would need to be verified by a background agent with full repository/file access before finalizing exploitation details. [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** third_party/ijar/zip.cc (L704-778)
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

  // Do not change output values before determining that they are OK.
  *offset = cd.central_dir_offset;
  // Central directory start can then be used to determine the actual
  // starts of the zip file (which can be different in case of a non-zip
  // header like for auto-extractable binaries).
  *central_dir = end_of_central_dir - cd.central_dir_size;
  return true;
}
```
