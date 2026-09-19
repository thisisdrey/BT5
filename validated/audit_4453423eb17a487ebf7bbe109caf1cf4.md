### Title
Out-of-bounds read in `InputZipFile::ProcessCentralDirEntry()` when enumerating ZIP central-directory extra fields - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar` is Bazel's own vendored ZIP/JAR parser, used by `ijar`/`singlejar` to build interface jars and merge jars during a build [1](#0-0) . Unlike `InputZipFile::ProcessLocalFileEntry()`, which validates every variable-length field against the remaining mapped-file size via `EnsureRemaining()` before advancing the read cursor [2](#0-1) , `InputZipFile::ProcessCentralDirEntry()` reads `file_name_length`, `extra_field_length`, and `file_comment_length` straight from attacker-controlled central-directory bytes and advances the cursor `p` by those lengths — and then walks a sub-loop over "extra field" records — with **no bounds check at all** against the size of the mapped input file. This is the same bug class as CVE-2024-50248 (`ntfs3` `mi_enum_attr()`): enumerating variable-length attribute/record structures without verifying each one stays inside the backing buffer.

### Finding Description
`ProcessCentralDirEntry()` parses one central-directory header:
```
p += 16;  // skip to 'compressed size' field
*compressed_size = get_u4le(p);
*uncompressed_size = get_u4le(p);
u2 file_name_length = get_u2le(p);
u2 extra_field_length = get_u2le(p);
u2 file_comment_length = get_u2le(p);
...
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  ...
}
p += file_comment_length;
``` [3](#0-2) 

None of `file_name_length`, `extra_field_length`, `file_comment_length`, or the per-record `data_size` inside the extra-field enumeration loop is checked against the actual remaining size of the mmap'd input file (`input_file_->Length()`), unlike every read in `ProcessLocalFileEntry()`, which calls `EnsureRemaining()` first [4](#0-3) [5](#0-4) . A crafted central-directory entry can set:
- `extra_field_length` so large that `p` is pushed past the end of the mapped file, or
- a per-record `data_size` inside the extra-field loop so large that `extra_p` overshoots `p`, causing the `while (extra_p != p)` loop to keep reading 2-/4-/8-byte little-endian values (`get_u2le`/`get_u4le`/`get_u8le`) far beyond the mapped region.

`ProcessNext()` (used by `singlejar`/`ijar`'s main iteration) and `CalculateOutputLength()` both call `ProcessCentralDirEntry()` repeatedly in a `while (true)` loop with no independent bound on `central_dir_current_`/`current` relative to the file's end [6](#0-5) [7](#0-6) , so a single malformed entry can drive the cursor arbitrarily far past the mmap'd file boundary, unlike the local-file-header path that is bounds-checked at every step.

This differs from the fully bounds-checked "output" side (`OutputJar::AppendToDirectoryBuffer` in `src/tools/singlejar`), which explicitly validates that each extra-field record stays within `[ef_begin, ef_end)` before touching it [8](#0-7)  — showing Bazel's own code base knows this containment check is required, but it is missing in the `third_party/ijar/zip.cc` central-directory reader.

### Impact Explanation
The input to `InputZipFile` is a raw JAR/ZIP file supplied to the build (e.g., a prebuilt jar dependency, a fetched `http_jar`/`http_archive` artifact, or any jar consumed by `ijar`/`singlejar`). Since `ijar` parses the mmap'd bytes directly rather than through a safety-checked unzip library, a byte-for-byte adversarial JAR (which an attacker can fully control if the content is unpinned, or if it is consumed from an untrusted branch/CI context) triggers an out-of-bounds read past the end of the mapped file region. Depending on memory layout this causes a crash (denial of the build tool) or an over-read of adjacent process memory whose bytes can leak into further processing (e.g., filenames, sizes) that ultimately influence the generated interface jar/output — a concrete memory-safety violation in Bazel's own toolchain code, not a third-party dependency.

### Likelihood Explanation
Exploitability only requires supplying a malformed ZIP/JAR central directory (a few crafted bytes in an otherwise valid-looking archive) to any Bazel rule that invokes `ijar` or `singlejar` on that file — a routine, unprivileged action for anyone who can publish/serve a jar dependency the build consumes. No credentials, sandbox escape, or trusted-repo Starlark access is needed; the bug is purely in the byte parsing of untrusted archive content.

### Recommendation
Add bounds checking in `InputZipFile::ProcessCentralDirEntry()` analogous to `EnsureRemaining()` in `ProcessLocalFileEntry()`: validate that `file_name_length`, `extra_field_length`, and `file_comment_length` (and each per-record `data_size` in the extra-field enumeration loop) do not exceed the remaining bytes in the mapped input file before advancing any pointer or dereferencing memory, mirroring the containment check already used in `OutputJar::AppendToDirectoryBuffer`.

### Proof of Concept
A reproducible test would extend `input_jar_preambled_test.cc` (or a new `zip_test.cc` case) to construct a minimal ZIP with:
1. One central-directory header whose `extra_field_length` (or an internal extra-field `data_size`) is set larger than the remaining bytes of the archive.
2. Feed this buffer through `InputZipFile::Open(path, data, length)` and call `NextEntry`/`ProcessNext` (as exercised in `input_jar_preambled_test.cc`'s `Verify()` helper) [9](#0-8) .
3. Run under AddressSanitizer to observe the heap-buffer-overflow/over-read triggered inside `ProcessCentralDirEntry()`'s extra-field loop [10](#0-9) , confirming no bounds check stops the read before it exceeds the mapped file's extent.

### Citations

**File:** third_party/ijar/zip.cc (L14-21)
```text
//
// zip.cc -- .zip (.jar) file reading/writing routines.
//

// See README.txt for details.
//
// See http://www.pkware.com/documents/casestudies/APPNOTE.TXT
// for definition of PKZIP file format.
```

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

**File:** third_party/ijar/zip.cc (L493-543)
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
```

**File:** third_party/ijar/zip.cc (L550-574)
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
```

**File:** src/tools/singlejar/output_jar.cc (L934-940)
```text
  ExtraField* out_ef = out_ef_begin;
  for (const ExtraField* ef = ef_begin; ef < ef_end; ef = ef->next()) {
    if (ziph::byte_ptr(ef) + sizeof(ExtraField) > ziph::byte_ptr(ef_end) ||
        ziph::byte_ptr(ef) + ef->size() > ziph::byte_ptr(ef_end)) {
      diag_errx(1, "malformed extra field in CDH for %.*s",
                (int)cdh->file_name_length(), cdh->file_name());
    }
```

**File:** src/tools/singlejar/input_jar_preambled_test.cc (L45-63)
```text
void Verify(const std::string& path) {
  InputJar input_jar;
  ASSERT_TRUE(input_jar.Open(path));
  const LH* lh;
  const CDH* cdh;
  while ((cdh = input_jar.NextEntry(&lh))) {
    ASSERT_TRUE(cdh->is()) << "No expected tag in the Central Directory Entry.";
    ASSERT_NE(nullptr, lh) << "No local header.";
    ASSERT_TRUE(lh->is()) << "No expected tag in the Local Header.";
    EXPECT_EQ(lh->file_name_string(), cdh->file_name_string());
    if (!cdh->no_size_in_local_header()) {
      EXPECT_EQ(lh->compressed_file_size(), cdh->compressed_file_size())
          << "Entry: " << lh->file_name_string();
      EXPECT_EQ(lh->uncompressed_file_size(), cdh->uncompressed_file_size())
          << "Entry: " << cdh->file_name_string();
    }
  }
  input_jar.Close();
}
```
