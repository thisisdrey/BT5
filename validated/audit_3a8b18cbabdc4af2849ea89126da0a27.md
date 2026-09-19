### Title
Out-of-bounds read in ijar's ZIP central-directory parser due to unchecked length fields - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses each ZIP/JAR central-directory entry (and its ZIP64 extra field) by trusting attacker-controlled 16/32/64-bit length fields without ever checking that reads stay within the memory-mapped file buffer, unlike the sibling local-file-header parser which explicitly bounds-checks via `EnsureRemaining()`.

### Finding Description
`ijar` mmaps an entire JAR file and walks its ZIP central directory to build an "interface jar" used for Java compilation (`java_common.run_ijar`, invoked as a build action on jars that are frequently fetched from external/remote sources such as `http_jar`/`http_file`, where only a `sha256`/`integrity` value pins the exact bytes an attacker chooses to publish).

In `ProcessCentralDirEntry` [1](#0-0) , the function reads `file_name_length`, `extra_field_length`, `file_comment_length`, and later a ZIP64 extra-field payload, and advances the cursor `p` purely based on these values taken from the file with **no `EnsureRemaining`-style bound check** against the mapped buffer size — contrast this with `ProcessLocalFileEntry`, which does call `EnsureRemaining()` before every variable-length read [2](#0-1) .

The ZIP64 extra-field loop is the most direct OOB-read primitive: [3](#0-2) 
Here `extra_field_length` and each extra-field entry's `data_size` are attacker-controlled u2 values. The loop condition `while (extra_p != p)` assumes `data_size` values sum exactly to `extra_field_length`, but nothing prevents a crafted entry where `data_size` is larger than what remains, causing `extra_p` to run past `p` (and past the actual extra-field region), and then `get_u8le(extra)` dereferences memory beyond the intended field — an out-of-bounds read of the mmap'd region (potentially reading past the mapping entirely near the end of the file, which can fault or leak adjacent heap/page content into the emitted interface jar or program state).

Similarly, `FindZipCentralDirectory`/`ProcessCentralDirEntry` compute `central_dir_offset`/`central_dir_size` and entry lengths from the file itself; while `FindZipCentralDirectory` does check `cd.central_dir_offset + cd.central_dir_size > in_length` [4](#0-3) , no equivalent per-entry check exists inside `ProcessCentralDirEntry` itself, so a malformed entry whose declared lengths exceed the actual central-directory size still gets parsed field-by-field without a remaining-bytes guard.

### Impact Explanation
An attacker who serves a malformed jar (as an `http_jar`/`http_file` payload, or as a third-party/Maven artifact fetched by a Bazel build) whose exact bytes match the pinned checksum can craft a central directory entry whose `extra_field_length`/ZIP64 `data_size` fields cause `ProcessCentralDirEntry` to read past the boundaries of the mmap'd input file. This is an out-of-bounds read that can crash the `ijar` process (denial of the build) or, in the ZIP64 8-byte-read case, incorporate out-of-mapping memory contents into program behavior/emitted output. Checksum verification only pins *which* bytes were fetched — it does not validate that those bytes form a structurally sound ZIP, so a hostile origin that a victim's build depends on can supply exactly this malformed-but-hash-matching content.

### Likelihood Explanation
Reaching this code only requires a Bazel target invoking `java_common.run_ijar` (an extremely common, largely implicit step for any `java_library`/`java_import` with compile-time jars) on a jar fetched from a repository the attacker controls (their own release artifact, a compromised/first-seen mirror, etc.). No special build flags or opt-in are needed — `ijar` runs by default whenever Java compile jars are produced.

### Recommendation
Add bounds checking to `ProcessCentralDirEntry` (and its ZIP64 extra-field loop) analogous to `EnsureRemaining()` in `ProcessLocalFileEntry`: verify that `file_name_length`, `extra_field_length`, `file_comment_length`, and each extra-field's `header_id`/`data_size` pair stay within the bytes actually remaining in the mapped central directory / file before advancing pointers or dereferencing them, and reject the archive with a diagnostic instead of reading out of bounds.

### Proof of Concept
A JUnit/`BuildIntegrationTestCase`-style reproduction would:
1. Programmatically construct a minimal ZIP whose central directory contains one entry with a legitimate EOCD/size but with a ZIP64 extra field (`header_id = 0x0001`) whose declared `data_size` extends beyond the actual `extra_field_length` (and beyond the mapped file length near EOF).
2. Feed this file to `ZipExtractor::Create`/`ProcessAll()` (as `ijar.cc`'s `main`/`OpenFilesAndProcessJar` does) and observe a crash/ASan out-of-bounds-read report inside `ProcessCentralDirEntry`'s extra-field loop when `get_u8le(extra)` reads past the mapping, mirroring `src/tools/singlejar/input_jar_scan_entries_test.h`'s existing ZIP64 entry test pattern but with a deliberately malformed extra-field length.

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

**File:** third_party/ijar/zip.cc (L493-542)
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
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
