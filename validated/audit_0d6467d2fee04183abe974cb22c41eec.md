## Title
Heap/mmap buffer over-read in `InputZipFile::ProcessCentralDirEntry` due to unchecked, attacker-controlled ZIP central-directory length fields — (File: `third_party/ijar/zip.cc`)

### Summary
`ijar` (the interface-jar generator that Bazel runs on every `.jar` input, including third-party jars fetched via `http_jar`/`http_archive`/Maven rules) parses the ZIP central directory in `InputZipFile::ProcessCentralDirEntry`. Unlike `ProcessLocalFileEntry`, which validates remaining bytes with `EnsureRemaining()` before every read, `ProcessCentralDirEntry` reads the fixed 46-byte central-directory header and then advances the cursor by attacker-controlled `file_name_length`, `extra_field_length`, and `file_comment_length` fields — and, inside the extra-field loop, by an attacker-controlled `data_size` — with no check that the resulting pointer stays inside the region validated at parse time (`cd.central_dir_offset + cd.central_dir_size <= in_length`, checked only once in `FindZipCentralDirectory`).

### Finding Description
`FindZipCentralDirectory` (third_party/ijar/zip.cc:704-778) validates only that the *aggregate* declared central-directory offset/size fits inside the mapped file: [1](#0-0) 
It never re-validates per-entry advances. `ProcessCentralDirEntry` then reads each entry's `file_name_length`, `extra_field_length`, `file_comment_length` and blindly advances `p` by those values: [2](#0-1) 
and walks the extra-fields sub-records using an attacker-controlled `data_size` with no remaining-length check at all: [3](#0-2) 
This is in sharp contrast to the local-file-header parser in the same file, which does bound-check every field via `EnsureRemaining`: [4](#0-3) 
A crafted jar's last central-directory entry can declare `file_name_length`/`extra_field_length`/`file_comment_length`/extra-field `data_size` values (each up to 0xFFFF, and the extra-field loop can be repeated) that push `p` (and thus `central_dir_current_`, stored for the next `ProcessNext()` call) tens of kilobytes past the actual mapped file end (`in_length`), which the aggregate check in `FindZipCentralDirectory` never re-validates per record. The next call to `ProcessNext()`/`ProcessCentralDirEntry` then dereferences `get_u4le`/`get_u2le`/`memcpy` on this out-of-bounds pointer, reading adjacent heap/mmap memory. Because `ProcessCentralDirEntry` also `memcpy`s from `p` into the `filename[PATH_MAX]` buffer (third_party/ijar/zip.cc:516-522) whose contents are subsequently used as the entry name in the output artifact, out-of-bounds process memory can leak into ijar's output or error messages — the same "crafted header field drives a heap-based buffer over-read that discloses information" bug class as CVE-2018-11729.

### Impact Explanation
An attacker who controls a jar consumed by the build (e.g., a `.jar` fetched by `http_jar`, extracted from an `http_archive`, or pulled by Maven/`rules_jvm_external`) can craft a ZIP central directory whose length fields cause ijar to read past the memory-mapped file. This is an out-of-bounds heap/mmap read reachable purely from attacker-published archive bytes, with no code execution or checksum bypass required, and can leak adjacent process memory into build outputs/logs (information disclosure), matching the CVE's class exactly.

### Likelihood Explanation
`ijar` runs automatically as part of Bazel's Java compilation pipeline (`ijar` action) on every jar input, including externally-fetched jars, so the vulnerable code path is reached on ordinary builds without any special configuration. The only requirement is that the attacker control the bytes of a `.jar`/`.zip` file consumed by the build — squarely within the described attacker model (a hostile origin server/mirror serving a malicious jar dependency).

### Recommendation
Add bounds validation in `ProcessCentralDirEntry` (and its extra-field loop) analogous to `EnsureRemaining()` in `ProcessLocalFileEntry`: before reading the fixed 46-byte header, and before each pointer advance by `file_name_length`, `extra_field_length`, `file_comment_length`, and the extra-field `data_size`, verify the resulting cursor stays within `central_dir_ + central_dir_size` (and within `zipdata_in_ + in_length`). Reject the file with a clear error instead of continuing to parse past validated bounds.

### Proof of Concept
A `src/test/shell/bazel/...` or `third_party/ijar/test` style reproduction:
1. Build a minimal valid ZIP/JAR with one central-directory entry.
2. Patch that entry's `file_name_length`/`extra_field_length`/`file_comment_length` fields (or an extra-field `data_size`) to large values (e.g., 0xFFFF each) so the declared entry size, while still satisfying `central_dir_offset + central_dir_size <= in_length` in aggregate (achieved by shrinking `central_dir_size` reported in the EOCD record independently of what a single entry's fields force `p` to consume), causes `ProcessCentralDirEntry`'s cursor to run past `zipdata_in_ + in_length` on the next call.
3. Run `$IJAR crafted.jar out.jar` (as in `third_party/ijar/test/ijar_test.sh`, e.g. `test_wrong_centraldir`/`test_corrupted_end_of_centraldir`) under ASan; observe a heap-buffer-overflow/over-read report instead of a clean, bounded error, confirming the missing `EnsureRemaining`-style check in the central-directory path. [5](#0-4) [6](#0-5)

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
