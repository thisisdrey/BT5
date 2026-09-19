### Title
Out-of-bounds read past mmap'd JAR file when parsing untrusted central-directory entry lengths in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar` (Bazel's interface-jar generator, used to strip non-ABI content from `.jar` files consumed by `java_library`/`java_import`, including jars fetched from external, attacker-influenced sources) parses the ZIP central directory of an input jar via `InputZipFile::ProcessCentralDirEntry`. Unlike `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before every variable-length read, `ProcessCentralDirEntry` performs no bounds checking at all before reading fixed fields or before using attacker-controlled `file_name_length`/`extra_field_length`/`file_comment_length` values to advance the cursor and to `memcpy` bytes into a stack buffer.

### Finding Description
`FindZipCentralDirectory` only validates the aggregate central-directory bounds (`cd.central_dir_offset + cd.central_dir_size <= in_length`) [1](#0-0) , but nothing constrains the per-entry declared lengths against the actual remaining bytes in the mmap'd input file while `ProcessCentralDirEntry`/`ProcessNext`/`CalculateOutputLength` iterate entries [2](#0-1) [3](#0-2) .

Inside `ProcessCentralDirEntry`, `file_name_length`, `extra_field_length`, and `file_comment_length` are read directly from the untrusted archive with no `EnsureRemaining`-style check, and the pointer `p` is advanced by these attacker-chosen values with no validation that the target still lies within the mapped file: [4](#0-3) 

The filename is then copied with a memcpy whose length is bounded against the *destination* buffer (`filename_size`) but not against how many bytes are actually left in the *source* (mmap'd file): [5](#0-4) 

This is the same bug class as CVE-2025-1675 (`dns_copy_qname`): an untrusted length field is trusted for a `memcpy` without verifying the source buffer actually contains that many bytes. If a crafted central directory entry near the end of the mapped region declares an oversized `file_name_length`/`extra_field_length`, the read (and the `memcpy`) can run past the last mapped page of the input file, causing a read into unmapped memory (crash) or into adjacent process memory if it stays within the same page (information disclosure, since the copied bytes populate `filename`, which is passed to `processor->Accept()` and can be embedded into the generated interface jar's output entries).

The extra-field walk in the same function is similarly unchecked: `data_size` from each extra field tag is attacker controlled and used to advance `extra_p` and to call `get_u8le(extra)`, with no check that 8 bytes remain before the end of the declared extra-field region or the mapped file [6](#0-5) .

### Impact Explanation
`ijar` runs as part of normal Bazel Java build actions on any `.jar` supplied to the build (including jars obtained via `http_jar`/`http_archive`-fetched dependencies, third-party Maven artifacts pinned only by a hash the attacker who authored the artifact also controls). A hostile artifact author can craft a JAR whose SHA-256 matches the value declared in the BUILD/lock file (since they produce the artifact themselves) while the ZIP central directory contains truncated/oversized length fields. Once fetched and hash-verified, `ijar` still parses it unsafely, so the out-of-bounds read is fully reachable with attacker-controlled bytes even though content integrity was "verified" — the vulnerable code path is downstream of, not protected by, checksum verification. Depending on layout this can crash the build (bad) or leak adjacent process memory bytes into filenames that end up written into the generated interface jar's central directory (worse: data exfiltration into a build artifact).

### Likelihood Explanation
Moderate-to-high: any repository rule or `java_import`/`java_library` target that consumes a jar from an external, non-root-controlled source (mirrors, third-party registries) can trigger this by supplying a jar with a truncated/malformed central directory. No special privilege is needed; only the ability to publish/serve a file that a victim's build fetches and later processes with `ijar`. Because `ProcessCentralDirEntry` is unconditionally exercised whenever `ijar` builds an interface jar (the default code path for Java compile-jar generation), reachability is straightforward. The main mitigating factor is that most legitimate jars are well-formed, so triggering requires deliberately malformed input — which is exactly the attacker's capability described in this exploit class.

### Recommendation
Add `EnsureRemaining`-equivalent bounds checks in `ProcessCentralDirEntry` before reading the fixed 46-byte header and before consuming `file_name_length`, `extra_field_length`, and `file_comment_length` bytes, verifying against the actual remaining bytes in the mapped input file (`input_file_->Length()` relative to `p`), mirroring what `ProcessLocalFileEntry` already does. Similarly bound the extra-field walk (`data_size`) against the declared `extra_field_length` and the mapped file end before calling `get_u8le`/`get_u4le`.

### Proof of Concept
Construct a malformed jar whose end-of-central-directory record and aggregate `central_dir_size` are internally consistent (so `FindZipCentralDirectory`'s check at zip.cc:766 passes), but whose last central directory entry declares `file_name_length`/`extra_field_length` values that, when added to that entry's offset, exceed the actual mapped file length (`in_length`). Feed this file to `ZipExtractor::Create`/`ProcessAll` (as `ijar` does) — e.g., via a `BuildIntegrationTestCase`/`src/test/shell/bazel` test that runs `ijar` (or a `java_import` build) over the crafted jar — and observe the out-of-bounds read (ASan report of heap/mmap over-read, or a crash) in `ProcessCentralDirEntry`'s `memcpy` at zip.cc:520. This can be added alongside the existing tests in `third_party/ijar/test/zip_test.sh` and `third_party/ijar/test/IjarTests.java` as a regression test with a fuzzed/truncated central directory entry. [7](#0-6)

### Citations

**File:** third_party/ijar/zip.cc (L302-330)
```text
bool InputZipFile::ProcessNext() {
  // Process the next entry in the central directory. Also make sure that the
  // content pointer is in sync.
  u8 compressed, uncompressed;
  u8 offset;
  if (!ProcessCentralDirEntry(central_dir_current_, &compressed, &uncompressed,
                              filename, PATH_MAX, &attr, &offset)) {
    return false;
  }

  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

  if (EnsureRemaining(4, "signature") < 0) {
    return false;
  }
  u4 signature = get_u4le(p);
  if (signature == LOCAL_FILE_HEADER_SIGNATURE) {
    if (ProcessLocalFileEntry(compressed, uncompressed) < 0) {
      return false;
    }
  } else {
    error("local file header signature for file %s not found\n", filename);
    return false;
  }

  return true;
}
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

**File:** third_party/ijar/zip.cc (L550-581)
```text
u8 InputZipFile::CalculateOutputLength() {
  const u1* current = central_dir_;

  u8 compressed_size = 0;
  u8 uncompressed_size = 0;
  u8 skipped_compressed_size = 0;
  u4 attr;
  u8 offset;
  char filename[PATH_MAX];

  while (true) {
    u8 file_compressed, file_uncompressed;
    if (!ProcessCentralDirEntry(current,
                                &file_compressed, &file_uncompressed,
                                filename, PATH_MAX, &attr, &offset)) {
      break;
    }

    if (processor->Accept(filename, attr)) {
      compressed_size += (u8) file_compressed;
      uncompressed_size += (u8) file_uncompressed;
    } else {
      skipped_compressed_size += file_compressed;
    }
  }

  // The worst case is when the output is simply the input uncompressed. The
  // metadata in the zip file will stay the same, so the file will grow by the
  // difference between the compressed and uncompressed sizes.
  return (u8) input_file_->Length() - skipped_compressed_size
      + (uncompressed_size - compressed_size);
}
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
