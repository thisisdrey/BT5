### Title
Unbounded central-directory field parsing causes heap over-read in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar/zip.cc`'s `InputZipFile::ProcessCentralDirEntry` parses attacker-controlled Central Directory Header fields (`file_name_length`, `extra_field_length`, `file_comment_length`, and nested extra-field `header_id`/`data_size` pairs) from a memory-mapped ZIP file without any bounds validation against the mapped region, unlike the sibling local-file-header parser which explicitly calls `EnsureRemaining()` before every read/advance.

### Finding Description
`ProcessLocalFileEntry` (third_party/ijar/zip.cc:332-418) is careful: before reading the file name and extra fields it calls `EnsureRemaining(file_name_length_, "file_name")` and `EnsureRemaining(extra_field_length_, "extra_field")` [1](#0-0) , which checks against `input_file_->Length()` before advancing the cursor `p`.

`ProcessCentralDirEntry`, which walks the Central Directory (the structure that is fully attacker-controlled in any ZIP served to `http_archive`/`http_jar`/`java_import` inputs processed by `ijar`/`singlejar`), performs no equivalent check. It reads 16-bit length fields straight from the mapped buffer and then does pointer arithmetic and a `memcpy` based on those lengths: [2](#0-1) 

The nested extra-field walking loop is similarly unchecked — it reads a `header_id`/`data_size` pair and advances `extra_p += data_size` without verifying that `extra_p` remains within `[p, p+extra_field_length)`, let alone within the mapped file: [3](#0-2) 

The function's own comment acknowledges this is a structural assumption, not a real bounds check: "Note that the central directory is always followed by another data structure that has a signature, so parsing it this way is safe." [4](#0-3)  That assumption is false for a maliciously crafted archive: a hostile server can set `file_name_length`, `extra_field_length`, or `file_comment_length` to values that push `p` (or `extra_p`) past the end of the mmap'd region.

This mirrors the CVE-2019-19777 bug class: `stbi__load_main` trusted length/dimension fields taken directly from untrusted image bytes to drive buffer reads without validating them against the actual buffer bounds, producing a heap-based buffer over-read. Here, `file_name_length`/`extra_field_length`/`data_size` read straight from an untrusted ZIP central directory drive `memcpy` and pointer advances without validating against the mapped file's actual size.

### Impact Explanation
`ProcessCentralDirEntry` is invoked both when iterating entries via `ProcessNext()` (`InputZipFile::ProcessNext` at third_party/ijar/zip.cc:302-330) and when precomputing sizes via `CalculateOutputLength()` (third_party/ijar/zip.cc:550-581), i.e., on every entry of every ZIP/JAR that `ijar`/`singlejar` processes. A malformed length field can cause `memcpy` to copy from `p` past the end of the memory-mapped file into adjacent heap/mapped memory (out-of-bounds read), which can manifest as a crash (SIGSEGV when crossing an unmapped page boundary) or, if the over-read stays within mapped-but-unrelated memory, leak adjacent bytes into the resulting interface/merged jar output. This is a memory-safety violation, not merely a documentation gap — it stems directly from trusting attacker-supplied length fields as if they were validated.

### Likelihood Explanation
Any archive/JAR file fetched without a `sha256`/`integrity` pin (both parameters are optional on `http_archive`/`http_jar`) and subsequently processed as a Java dependency (feeding into `ijar` for interface-jar generation, or into `singlejar` for jar merging) exposes this path directly to a hostile origin server or mirror. Even when a checksum is pinned, this parsing routine is exercised on every legitimate ZIP/JAR the build processes, so any bug in length validation is reachable purely by the shape of the file, independent of checksum enforcement — the checksum only guarantees byte-for-byte integrity of a file, not that its internal structure is well-formed. Since ZIP central directories are attacker-authored at the byte level, this is a directly reachable, low-complexity trigger requiring only building against or importing a crafted jar/zip.

### Recommendation
Add the same `EnsureRemaining()`-style bounds validation used in `ProcessLocalFileEntry` to `ProcessCentralDirEntry`: before reading and advancing past `file_name_length`, `extra_field_length`, and `file_comment_length`, and before advancing `extra_p` by each `data_size`, verify the resulting pointer does not exceed the mapped input length (or the enclosing `extra_field_length`). Reject (`error(...)`) any entry whose declared lengths exceed the remaining file/extra-field size rather than assuming central-directory well-formedness.

### Proof of Concept
A reproducible JUnit/`gtest` test would construct a minimal ZIP with:
1. A valid End-of-Central-Directory record and one Central Directory Header near the very end of the mapped file.
2. `file_name_length` (or `extra_field_length`) set to a value that, when added to `p`, points past `input_file_->Length()`.

Feeding this file into `ZipExtractor::Create(...)` / `ProcessAll()` (third_party/ijar/zip_main.cc, `zip.cc:785-803`) demonstrates the `memcpy` in `ProcessCentralDirEntry` (zip.cc:520) reading past the mapped file boundary, distinct from the already-guarded `ProcessLocalFileEntry` path — a test analogous to the existing malformed-extra-field regression test in `src/tools/singlejar/output_jar_simple_test.cc:1179-1225` (`CreateZipWithMalformedExtraField`), but targeting the *central directory* parser in `ijar/zip.cc` rather than singlejar's local-header extra-field handling. [5](#0-4)

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

**File:** third_party/ijar/zip.cc (L491-492)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
```

**File:** third_party/ijar/zip.cc (L507-525)
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
```

**File:** third_party/ijar/zip.cc (L526-542)
```text
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
