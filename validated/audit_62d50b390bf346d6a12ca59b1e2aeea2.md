### Title
Missing bounds check in central-directory parsing allows out-of-bounds read on attacker-supplied JAR/ZIP - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar (Bazel's interface-jar generator, used when processing JAR/ZIP archives — including third‑party JARs pulled in via `http_jar`/`http_archive`/`java_import`) reads the fixed and variable-length fields of a ZIP central-directory record without ever validating that the declared lengths and record actually fit inside the mapped input file, unlike its sibling `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every read.

### Finding Description
`ProcessLocalFileEntry` is careful: every read of a variable-length field is preceded by an `EnsureRemaining()` bounds check against the mmap'd file length [1](#0-0) .

`ProcessCentralDirEntry`, however, only checks the 4-byte signature and then unconditionally advances the cursor and reads `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, `offset`, followed by a `memcpy` of `file_name_length` bytes and a walk over `extra_field_length` bytes of extra-field records, with **no call to `EnsureRemaining`** anywhere in the function: [2](#0-1) .

The comment above the function claims this is "safe" because "the central directory is always followed by another data structure that has a signature" [3](#0-2) , but that assumption only protects against *walking past the true end of the central directory into the next well-formed structure* — it does nothing to stop a single malicious entry whose `file_name_length`/`extra_field_length`/`file_comment_length`/`compressed_size` fields are crafted to point past the end of the mapped file buffer. Because the file is memory-mapped (`MappedInputFile`), reading past the last byte of file content but still inside the last mapped page returns adjacent heap/page garbage; reading further can cross into an unmapped page and crash the process. The attacker-controlled `filename` bytes read via `memcpy` at line 520 are subsequently used to build the interface JAR's central directory (`CalculateOutputLength`/`ProcessNext` call this on every central-directory record while producing the ijar output) [4](#0-3) [5](#0-4) .

### Impact Explanation
An unprivileged attacker who controls the bytes of a JAR/ZIP archive consumed by a build (e.g., a `java_import`/`aar_import`-style dependency fetched via `http_jar`/`http_archive`, or any archive whose contents get processed by ijar to build an interface JAR) can craft a central-directory entry with out-of-range `file_name_length`/`extra_field_length`/`file_comment_length`/`compressed_size` fields. This causes ijar to read outside the bounds of the memory-mapped input file. Best case this is a crash (build-tool DoS); worst case, out-of-bounds heap bytes get copied into the `filename` buffer and are baked into the resulting interface JAR's directory metadata, leaking adjacent process memory content into a build artifact.

### Likelihood Explanation
Reaching this code only requires supplying a JAR/ZIP whose bytes are consumed by ijar as part of a normal Bazel Java build; no privileged access or MITM is required, matching the "unprivileged attacker publishing content consumed by the build" threat model. However, this is fundamentally a memory-safety/parser-hardening bug (comparable in shape to the ksmbd analog: a size/length field trusted before it's validated against the actual buffer bounds), and its most likely practical manifestation is a segfault/crash rather than a reliably exploitable, attacker-directed leak — the exact byte range read out-of-bounds depends on heap/mmap layout that the attacker cannot fully control.

### Recommendation
Add `EnsureRemaining()`-style bounds checks in `ProcessCentralDirEntry` before reading the fixed 46-byte central-directory header and before each of the variable-length reads (`file_name_length`, `extra_field_length`, `file_comment_length`, and the extra-field walk), mirroring the checks already present in `ProcessLocalFileEntry`, and reject the entry (propagate an error) instead of proceeding when the declared lengths would exceed the mapped file's remaining bytes.

### Proof of Concept
Not fully reproducible from the indexed context alone — a concrete JUnit/shell reproduction would require constructing a crafted ZIP whose EOCD/central-directory pointers make `FindZipCentralDirectory` locate a central directory near the very end of the mapped buffer, with a central-directory record's `file_name_length` (or `extra_field_length`) set large enough that `p += file_name_length` / the subsequent `memcpy` reads past `input_file_->Length()`, then running it through `third_party/ijar/zip.cc`'s `InputZipFile::ProcessNext`/`CalculateOutputLength` path (e.g., via the existing `third_party/ijar/test/ijar_test.sh` harness) and observing a crash or garbage filename bytes in the resulting interface JAR's directory. I could not confirm from the available index whether any existing test in `third_party/ijar/test/` already exercises this specific malformed-central-directory path, nor could I verify the exact page-mapping behavior of `MappedInputFile` (e.g., whether it always over-allocates a guard page) that would determine crash vs. silent OOB read — a Devin session with filesystem/build access would be needed to build such a PoC and confirm the crash/leak behavior on a current release.

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

**File:** third_party/ijar/zip.cc (L332-368)
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
```

**File:** third_party/ijar/zip.cc (L491-492)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
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

**File:** third_party/ijar/zip.cc (L560-574)
```text
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
```
