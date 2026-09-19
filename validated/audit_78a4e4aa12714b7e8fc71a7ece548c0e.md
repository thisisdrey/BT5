### Title
Heap buffer over-read parsing untrusted ZIP/JAR central directory entries in `InputZipFile::ProcessCentralDirEntry` - (File: third_party/ijar/zip.cc)

### Summary
`third_party/ijar/zip.cc` parses ZIP/JAR central directory records to build interface jars (`ijar`). Individual central-directory entries carry attacker-controlled length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) that are read and used to advance the parse cursor and to `memcpy` file names without verifying the fields against the actual remaining bytes of the mapped input file, mirroring the CVE-2018-11727 pattern where `libfsntfs_attribute_read_from_mft` trusted an attribute length field from a crafted structure without bounds-checking against the buffer, producing a heap-based buffer over-read/information disclosure.

### Finding Description
`InputZipFile::Open()` locates the central directory via `FindZipCentralDirectory` [1](#0-0) , which only validates that the *aggregate* `central_dir_offset + central_dir_size` fits within `in_length` [2](#0-1) . It does not validate that any individual entry's length fields stay within that region.

Each entry is then parsed by `InputZipFile::ProcessCentralDirEntry`, which reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from the untrusted bytes and immediately uses them to `memcpy` and to advance the `p`/`extra_p` cursors, with no check that `p + file_name_length`, `p + extra_field_length`, or the extra-field walk stays inside the mapped buffer: [3](#0-2) .

This is unlike the local-file-header parser (`ProcessLocalFileEntry`), which explicitly calls `EnsureRemaining()` before consuming `file_name_length`/`extra_field_length` bytes [4](#0-3) , and unlike `EnsureRemaining` itself, which exists specifically to prevent over-reads [5](#0-4) . No equivalent guard is applied in `ProcessCentralDirEntry`.

Because the central directory is parsed from a memory-mapped file (`MappedInputFile`), an entry whose `file_name_length`/`extra_field_length`/`comment_length` push the cursor past the actual end of file data (while still possibly within the aggregate `central_dir_size` bound, or even beyond it, since per-entry sizes are never cross-checked against the declared `central_dir_size`) causes reads of memory beyond the intended data — an out-of-bounds/over-read analogous to the disputed libfsntfs issue, where a crafted length field drove reads past the intended attribute buffer.

### Impact Explanation
An attacker who can get a crafted JAR/ZIP consumed by ijar (e.g., a maven/http_jar dependency that Bazel fetches and later processes with the ijar tool to produce interface jars for Java compilation) can craft a central directory entry with an oversized `file_name_length` or `extra_field_length` to cause a heap/mapping-boundary over-read. Depending on how the read memory is subsequently used (e.g., embedded into filenames written to the output jar, printed in diagnostics, or otherwise observable), this can leak adjacent process memory contents — an information-disclosure impact matching CVE-2018-11727's classification.

### Likelihood Explanation
The parsing path is reached automatically whenever ijar processes an externally-supplied jar; ijar is a standard part of Bazel's Java toolchain and is commonly invoked on third-party/external jars without re-validating their internal structure. No checksum or lockfile mechanism inspects the internal structure of an already-integrity-verified archive, so a byte-for-byte crafted (but pinned-hash-matching, since the attacker controls the file it published) archive can trigger this on first fetch.

### Recommendation
Add bounds checks in `InputZipFile::ProcessCentralDirEntry` (and in `FindZip64CentralDirectory`/`MaybeReadZip64CentralDirectory`, which also perform unchecked `get_u4le`/`get_u8le` reads) mirroring `EnsureRemaining()`: validate that `p + file_name_length`, `p + extra_field_length`, and `p + file_comment_length` (and the extra-field tag/size walk) never exceed `bytes + in_length`, rejecting the archive with an error rather than reading out of bounds.

### Proof of Concept
Not fully reproducible from static analysis alone: confirming an actual out-of-bounds read (vs. a read of zero-filled trailing mmap page) requires knowledge of `MappedInputFile`'s POSIX/Windows mapping implementation (e.g., `third_party/ijar/mapped_file_posix.inc` or equivalent), which was not available in the indexed content for this repository. A concrete JUnit/shell PoC would need to:
1. Construct a minimal ZIP whose EOCD/central-directory-size checks pass but where a single central directory entry declares `file_name_length` (or `extra_field_length`) larger than the remaining bytes in the mapped file.
2. Run it through the `ijar` binary (`third_party/ijar/zip.cc` via `ZipExtractor::Create`) and observe a crash (ASan heap-buffer-overflow) or leaked bytes in the resulting output/error message.

Because the underlying `MappedInputFile` implementation could not be located in the index, I cannot fully confirm whether the over-read manifests as a crash, a zero-read, or genuine adjacent-memory disclosure — this should be verified by a Devin session with full repository/file access before treating this as a confirmed, weaponizable vulnerability. If you want, I can set up a plan to reproduce and validate this in a live checkout.

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

**File:** third_party/ijar/zip.cc (L507-545)
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
