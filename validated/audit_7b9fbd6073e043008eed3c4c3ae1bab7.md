### Title
Out-of-bounds read in ijar's ZIP central-directory extra-field parsing - (File: `third_party/ijar/zip.cc`)

### Summary
CVE-2019-11372 is an out-of-bounds read in MediaInfo's tag parser (`Synched_Test`) that walks a length-prefixed field without validating that the declared length stays within the buffer, leading to a crash on attacker-controlled input. Bazel's `third_party/ijar` tool contains an analogous unchecked length-prefixed loop when parsing ZIP central-directory extra fields.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from the central directory header bytes and then advances the cursor `p` by these attacker-controlled 16-bit values without any bounds check against the mapped file's end [1](#0-0) . Unlike `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before consuming `file_name_length_` and `extra_field_length_` [2](#0-1) , `ProcessCentralDirEntry` has no equivalent `EnsureRemaining` calls at all.

Inside the same function, the extra-field parsing loop reads a `header_id` and `data_size` from `extra_p`, then advances `extra_p += data_size` and loops `while (extra_p != p)`, again with no check that `extra_p + data_size` stays within the declared extra-field region or the mapped file [3](#0-2) . If `data_size` for a ZIP64 extra field is malformed such that `extra_p` never lands exactly on `p` (e.g., overshoots it), the loop can read `header_id`/`data_size` from memory beyond the mmap'd input file, and `get_u8le(extra)` for `uncompressed_size`/`compressed_size`/`offset` can similarly read past the buffer.

This function is called both from `ProcessNext()` (the normal per-entry extraction path) and from `CalculateOutputLength()`, which walks the entire central directory in a `while (true)` loop purely to size the output buffer, before any entry-level validation occurs [4](#0-3) . The central directory itself is located via `MaybeReadZip64CentralDirectory`/`FindZip64CentralDirectory`, which perform pointer arithmetic on offsets read from the file (`bytes + zip64_end_of_central_dir_offset`) with only partial bounds checks (e.g. `zip64_locator - ZIP64_EOCD_FIXED_SIZE < bytes`), and I could not confirm from the available code that `central_dir_` and `central_dir_size` are fully validated against `input_file_->Length()` before `ProcessCentralDirEntry`/`CalculateOutputLength` begin walking entries [5](#0-4) .

ijar operates on a memory-mapped input file (`MappedInputFile`), so an out-of-bounds read here is a real out-of-bounds memory access on the mapped region, not merely a logical error — reading past the end of the mapping can cross a page boundary and cause a SIGSEGV crash, matching the MediaInfo CVE's "leads to a crash" impact class.

### Impact Explanation
ijar is invoked by Bazel to build interface jars from `.jar`/`.class` archive inputs (e.g., for `java_import`, prebuilt jars fetched via `http_jar`/`http_archive`, or jars produced from third-party binary dependencies). An attacker who controls the bytes of such a jar (e.g., a hostile origin server whose response bypasses/fails a declared `sha256`, or content on an untrusted branch that CI builds) can craft a central directory entry with a malformed extra-field `data_size` or oversized `extra_field_length`/`file_name_length` relative to the actual file size. This can cause `ijar` to read memory outside the mapped file, crashing the process (denial of build) or potentially leaking adjacent process memory into filename buffers that get written into the output interface jar.

### Likelihood Explanation
Reachability requires only that ijar processes an attacker-influenced ZIP/JAR whose declared checksum is not enforced or whose length fields disagree with the actual central directory bounds — no privileged access is needed, only the ability to serve/host the archive content. However, I was not able to fully trace the caller-side integrity checks (e.g., whether `http_jar`/`http_archive` sha256 verification always precedes ijar invocation, and whether `FindZip64CentralDirectory`/central-directory-size validation fully bounds `central_dir_` before parsing) using the available index, so this should be treated as a plausible but not fully confirmed reachable path.

### Recommendation
Add `EnsureRemaining`-style bounds checks in `ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, and `file_comment_length`, and bound-check the extra-field parsing loop (`header_id`/`data_size` reads and `extra_p += data_size`) against both the declared extra-field length and the mapped file's actual end, mirroring the fix pattern already present for local file headers and for `ExtraField::find` in `src/tools/singlejar/zip_headers.h` [6](#0-5) .

### Proof of Concept
I could not construct or verify a concrete reproducible JUnit/shell test within the available tool budget; a full PoC would need to build a crafted ZIP central directory (oversized `extra_field_length`, or a ZIP64 extra field with `data_size` chosen so `extra_p` overshoots `p`) truncated so the mapped file ends before the declared extents, then run `ijar` on it and observe a crash — analogous to the existing malformed-extra-field regression test pattern in `src/tools/singlejar/output_jar_simple_test.cc` (`CreateZipWithMalformedExtraField`), but targeting `third_party/ijar/zip.cc` instead of singlejar [7](#0-6) .

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

**File:** third_party/ijar/zip.cc (L507-542)
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

**File:** third_party/ijar/zip.cc (L662-700)
```text
bool FindZip64CentralDirectory(const u1 *bytes, size_t in_length,
                               const u1 **end_of_central_dir,
                               EndOfCentralDirectoryRecord *cd) {
  // In the absence of a zip64 extensible data sector, the zip64 EOCD is at a
  // fixed offset from the regular central directory.
  if (MaybeReadZip64CentralDirectory(
          bytes, in_length,
          *end_of_central_dir - ZIP64_EOCD_LOCATOR_SIZE - ZIP64_EOCD_FIXED_SIZE,
          end_of_central_dir, cd)) {
    return true;
  }

  // If we couldn't find a zip64 EOCD at a fixed offset, either it doesn't exist
  // or there was a zip64 extensible data sector, so try going through the
  // locator. This approach doesn't work if data was prepended to the archive
  // without updating the offset in the locator.
  const u1 *zip64_locator = *end_of_central_dir - ZIP64_EOCD_LOCATOR_SIZE;
  if (zip64_locator - ZIP64_EOCD_FIXED_SIZE < bytes) {
    return true;
  }
  u4 zip64_locator_signature = get_u4le(zip64_locator);
  if (zip64_locator_signature != ZIP64_EOCD_LOCATOR_SIGNATURE) {
    return true;
  }
  u4 disk_with_zip64_central_directory = get_u4le(zip64_locator);
  u8 zip64_end_of_central_dir_offset = get_u8le(zip64_locator);
  u4 zip64_total_disks = get_u4le(zip64_locator);
  if (MaybeReadZip64CentralDirectory(bytes, in_length,
                                     bytes + zip64_end_of_central_dir_offset,
                                     end_of_central_dir, cd)) {
    // TODO(b/228519294) Add a test for a valid zip64 file with total disks = 0
    if (disk_with_zip64_central_directory != 0 || zip64_total_disks > 1) {
      fprintf(stderr, "multi-disk JAR files are not supported\n");
      return false;
    }
    return true;
  }
  return true;
}
```

**File:** src/tools/singlejar/zip_headers.h (L99-115)
```text
  static const ExtraField* find(uint16_t tag, const uint8_t* start,
                                const uint8_t* end) {
    while (start < end) {
      if (ziph::byte_ptr(start) + sizeof(ExtraField) > ziph::byte_ptr(end)) {
        break;
      }
      auto extra_field = reinterpret_cast<const ExtraField*>(start);
      if (ziph::byte_ptr(start) + extra_field->size() > ziph::byte_ptr(end)) {
        break;
      }
      if (extra_field->is(tag)) {
        return extra_field;
      }
      start = ziph::byte_ptr(start) + extra_field->size();
    }
    return nullptr;
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
