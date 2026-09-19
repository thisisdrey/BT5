### Title
Missing bounds validation before reading central-directory entry fields lets a malformed (but checksum-valid) ZIP/JAR trigger an out-of-bounds read in ijar's `ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar/zip.cc`'s `InputZipFile::ProcessCentralDirEntry` reads the ZIP central directory record's fixed fields, filename, and extra-field entries directly from the mapped input buffer with **no bounds checking at all**, in contrast to `ProcessLocalFileEntry`, which consistently calls `EnsureRemaining()` before every variable-length read. This mirrors the f2fs bug class: an aggregate/overall validation exists elsewhere (or none at all for the central directory), but the per-field/per-entry size is never checked before dereferencing it, so crafted length fields can walk the read pointer past the end of the mapped file.

### Finding Description
`ProcessCentralDirEntry` ( [1](#0-0) ) performs the following without any remaining-length checks:
- Reads `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, `offset` directly from `p` via `get_u4le`/`get_u2le`.
- `memcpy`s `file_name_length` bytes from `p` into `filename` [2](#0-1) .
- Advances `p` by `file_name_length`, then loops over the extra-field area reading `header_id`/`data_size` pairs and unconditionally advancing `extra_p += data_size`, and for the ZIP64 tag calls `get_u8le(extra)` to read 8 bytes from `extra` [3](#0-2) .

None of `file_name_length`, `extra_field_length`, `file_comment_length`, or the per-extra-field `data_size` are validated against the remaining bytes in the memory-mapped input file before being used to read or copy data. This is the same bug class as the f2fs report: a length/size field taken from untrusted, attacker-supplied structured data is trusted to bound a subsequent read (`get_u8le`, `memcpy`) without checking that the full "entry" actually fits in the available buffer.

By contrast, `InputZipFile::ProcessLocalFileEntry` (same file) explicitly calls `EnsureRemaining()` before reading the fixed header, before reading `file_name_length` bytes, and before reading `extra_field_length` bytes [4](#0-3) , and `SkipFile`/`ProcessFile` also gate reads through `EnsureRemaining` [5](#0-4) . The central directory path was never given the same treatment, so it is the weaker link.

### Impact Explanation
`ijar`/`zip.cc`'s `InputZipFile` is used by bazel's zip-processing tooling (`third_party/ijar/zip_main.cc` extract/list commands, and the ijar interface-jar generator) to parse ZIP-format archives, including JARs that arrive as build inputs (e.g., pre-built jars fetched via `http_jar`/`http_archive` and consumed by `java_import`-style rules that run ijar to strip a jar down to its public interface). A file whose declared SHA-256/integrity hash is legitimate (the attacker controls the whole byte stream and simply computes the hash of it) can still contain an internally malformed ZIP central directory — the checksum verifies byte-for-byte content, not structural validity of the archive format. Because `ProcessCentralDirEntry` never bounds-checks `file_name_length`, `extra_field_length`, `file_comment_length`, or nested extra-field `data_size` against the mapped file's actual length, out-of-bounds reads (`memcpy`, `get_u8le`) past the end of the mapped input file are reachable purely from the crafted archive bytes. This is a memory-safety bug in a native (C++) tool invoked during the build, matching the "attacker-published content that a victim's build consumes" premise and the fact that a declared/pinned checksum does not stop it (checksum validates bytes, not archive structural invariants).

### Likelihood Explanation
Any attacker who can publish an archive at a URL the victim's build depends on (mirror, release artifact, registry-hosted jar) can trivially compute a matching SHA-256/integrity value for their crafted bytes and thus pass Bazel's checksum gate unchanged. Triggering the vulnerable code path only requires that the archive subsequently be parsed by `zip.cc`'s `InputZipFile` (e.g., via ijar interface-jar generation on a downloaded jar, or `third_party/ijar/zip_main.cc extract/list` tooling used in build actions). No cooperation from the victim beyond a normal `bazel build` is required, and no sandbox/exec boundary needs to be crossed — the bug is in the parser itself, which runs as a native binary in the build.

### Recommendation
Add `EnsureRemaining()`-equivalent bounds checks in `ProcessCentralDirEntry` before every variable-length read/copy: validate `file_name_length`, `extra_field_length`, and `file_comment_length` against the bytes remaining from `p` to the end of the mapped file before advancing `p`/`extra_p`, and validate each extra-field's `data_size` (and the 2+2 header bytes themselves) against the extra-field region boundary before reading `header_id`/`data_size`/`get_u8le(extra)`. This mirrors the fix pattern already used in `ProcessLocalFileEntry` and the fix already applied for local-header extra fields in `output_jar.cc` (`WriteEntry`), which explicitly bounds-checks `ef->size()` against `lh_ef_end` before use [6](#0-5) .

### Proof of Concept
A `BuildIntegrationTestCase`/native unit test (analogous to `zip_headers_test.cc`'s malformed-extra-field regression test [7](#0-6) ) should:
1. Construct a minimal ZIP file in memory whose Central Directory Header declares `file_name_length`/`extra_field_length`/`file_comment_length` (and/or a nested extra-field `data_size`) values that exceed the actual remaining bytes in the buffer (e.g., `extra_field_length = 0xFFFF` while the file only has a few trailing bytes).
2. Feed this buffer to `InputZipFile::Open`/`ProcessNext` (or invoke the `ijar`/`unzip` CLI in `third_party/ijar/zip_main.cc`) and, under ASan/valgrind, confirm a heap-buffer-overflow read is triggered in `ProcessCentralDirEntry`'s `memcpy`/`get_u8le` calls, contrasted with `ProcessLocalFileEntry`, which correctly rejects the equivalent malformed local header via `EnsureRemaining()`.

**Note on verification limits:** I was unable to locate the Java-side `ZipDecompressor`/`CompressedTarFunction` (used by `repository_ctx.extract`) in this index, so I could not directly confirm whether an equivalent unchecked-length pattern exists there; the finding above is grounded in `third_party/ijar/zip.cc`, which was directly inspected and shows the asymmetry described. If a fuller check of the Java decompression path is needed, a Devin session with full repository access would be required.

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

**File:** third_party/ijar/zip.cc (L420-476)
```text
int InputZipFile::SkipFile(const bool compressed) {
  if (!compressed) {
    // In this case, compressed_size_ == uncompressed_size_ (since the file is
    // uncompressed), so we can use either.
    if (compressed_size_ != uncompressed_size_) {
      return error("compressed size != uncompressed size, although the file "
                   "is uncompressed.\n");
    }
  }

  if (EnsureRemaining(compressed_size_, "file_data") < 0) {
    return -1;
  }
  p += compressed_size_;
  return 0;
}

u1* InputZipFile::UncompressFile() {
  size_t in_offset = p - zipdata_in_;
  size_t remaining = input_file_->Length() - in_offset;
  DecompressedFile *decompressed_file =
      decompressor_->UncompressFile(p, remaining);
  if (decompressed_file == NULL) {
    if (decompressor_->GetError() != NULL) {
      error(decompressor_->GetError());
    }
    return NULL;
  } else {
    compressed_size_ = decompressed_file->compressed_size;
    uncompressed_size_ = decompressed_file->uncompressed_size;
    u1 *uncompressed_data = decompressed_file->uncompressed_data;
    free(decompressed_file);
    p += compressed_size_;
    return uncompressed_data;
  }
}

int InputZipFile::ProcessFile(const bool compressed) {
  const u1 *file_data;
  if (compressed) {
    file_data = UncompressFile();
    if (file_data == NULL) {
      return -1;
    }
  } else {
    // In this case, compressed_size_ == uncompressed_size_ (since the file is
    // uncompressed), so we can use either.
    if (compressed_size_ != uncompressed_size_) {
      return error("compressed size != uncompressed size, although the file "
                   "is uncompressed.\n");
    }

    if (EnsureRemaining(compressed_size_, "file_data") < 0) {
      return -1;
    }
    file_data = p;
    p += compressed_size_;
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

**File:** src/tools/singlejar/output_jar.cc (L780-785)
```text
  for (const ExtraField* ef = lh_ef_begin; ef < lh_ef_end; ef = ef->next()) {
    if (ziph::byte_ptr(ef) + sizeof(ExtraField) > ziph::byte_ptr(lh_ef_end) ||
        ziph::byte_ptr(ef) + ef->size() > ziph::byte_ptr(lh_ef_end)) {
      diag_errx(1, "malformed extra field in LH for %.*s",
                (int)entry->file_name_length(), entry->file_name());
    }
```

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1179-1225)
```text
std::string CreateZipWithMalformedExtraField() {
  std::string zip_data;
  const std::string filename = "evil.bin";

  // 1. Local File Header (LFH)
  size_t lh_offset = zip_data.size();
  size_t lh_size = sizeof(LH) + filename.size();
  zip_data.resize(lh_offset + lh_size, 0);
  auto* lh = reinterpret_cast<LH*>(&zip_data[lh_offset]);
  lh->signature();
  lh->version(10);
  lh->file_name(filename.data(), filename.size());

  // 2. Extra field payload containing an oversized payload_size
  uint8_t ef_buffer[8] = {0};
  auto* ef1 = reinterpret_cast<ExtraField*>(ef_buffer);
  ef1->signature(0x000d);
  ef1->payload_size(0);

  auto* ef2 = reinterpret_cast<ExtraField*>(ef_buffer + ef1->size());
  ef2->signature(0xdead);
  ef2->payload_size(0xf000);  // Malformed size exceeding extra field buffer

  // 3. Central Directory Header (CDH)
  size_t cdh_offset = zip_data.size();
  size_t cdh_size = sizeof(CDH) + filename.size() + sizeof(ef_buffer);
  zip_data.resize(cdh_offset + cdh_size, 0);
  auto* cdh = reinterpret_cast<CDH*>(&zip_data[cdh_offset]);
  cdh->signature();
  cdh->version(20);
  cdh->version_to_extract(10);
  cdh->local_header_offset32(lh_offset);
  cdh->file_name(filename.data(), filename.size());
  cdh->extra_fields(ef_buffer, sizeof(ef_buffer));

  // 4. End of Central Directory (EOCD)
  size_t ecd_offset = zip_data.size();
  zip_data.resize(ecd_offset + sizeof(ECD), 0);
  auto* ecd = reinterpret_cast<ECD*>(&zip_data[ecd_offset]);
  ecd->signature();
  ecd->this_disk_entries16(1);
  ecd->total_entries16(1);
  ecd->cen_size32(cdh_size);
  ecd->cen_offset32(cdh_offset);

  return zip_data;
}
```
