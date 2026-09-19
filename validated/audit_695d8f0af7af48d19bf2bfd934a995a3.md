Found a concrete analog: `InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads fixed-size and length-prefixed fields directly from the central directory buffer with **no bounds checking against the mapped file's end**, unlike the sibling function `EnsureRemaining()` used elsewhere in the same file for local-file-header parsing.

### Title
Heap buffer over-read in `ijar`/`singlejar` ZIP central-directory parsing due to missing bounds checks - (File: `third_party/ijar/zip.cc`)

### Summary
`ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, `file_comment_length`, and a nested Zip64 extra-field (`data_size`) from attacker-controlled bytes and advances the read cursor `p`/`extra_p` by those attacker-supplied lengths without ever verifying that the resulting pointer stays within the mapped input file bounds.

### Finding Description
`ProcessCentralDirEntry` (`third_party/ijar/zip.cc:493-545`) reads the 2-byte `file_name_length`, `extra_field_length`, and `file_comment_length` fields directly via `get_u2le(p)` [1](#0-0)  and then advances `p += file_name_length`, `p += extra_field_length`, `p += file_comment_length` without any check that these advances stay inside the mapped zip buffer [2](#0-1) [3](#0-2) . The inner extra-field walk (`extra_p != p`) reads a 2-byte `data_size` and advances `extra_p += data_size` and, for a Zip64 tag, calls `get_u8le(extra)` to read 8 bytes from `extra` — again without validating `data_size` against the extra-field region size [4](#0-3) . Contrast this with `InputZipFile::ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining(n, state)` before every variable-length read of `file_name_length_` and `extra_field_length_` [5](#0-4) , and with `EnsureRemaining` itself, which computes `remaining = input_file_->Length() - in_offset` and errors out if not enough bytes remain [6](#0-5) . No equivalent guard exists in `ProcessCentralDirEntry`, so a crafted central directory record with an oversized `file_name_length`/`extra_field_length`/`data_size` causes reads (and the `memcpy` into `filename`) past the end of the mapped input file — an mmap'd heap buffer — mirroring the GStreamer `qtdemux_parse_tree` heap over-read pattern from CVE-2025-47183.

### Impact Explanation
This is an information-disclosure primitive: bytes beyond the mapped zip file (including adjacent heap or unmapped memory) can be copied into `filename` via `memcpy(filename, p, len)` [7](#0-6)  or read via `get_u8le`/`get_u2le`, and the mis-parsed offsets can cause the function to report bogus but "valid" `compressed_size`/`uncompressed_size`/`offset` used later to slice file data, potentially leaking adjacent process memory into build outputs or crashing on unmapped pages.

### Likelihood Explanation
`ijar`/`singlejar` process jars that flow through the build graph (e.g., as dependencies or `deps` inputs); an attacker who controls a jar consumed as a build input (e.g., an unpinned/untrusted archive fetched by a repo rule and later fed into `ijar`) can trivially control central-directory bytes and trigger this path deterministically, since no checksum or containment check inspects the internal directory-entry field lengths before parsing.

### Recommendation
Add explicit remaining-bytes checks (analogous to `EnsureRemaining`) in `ProcessCentralDirEntry` before every pointer advance based on `file_name_length`, `extra_field_length`, `file_comment_length`, and inner extra-field `data_size`, rejecting the archive if any length would read past the end of the mapped file.

### Proof of Concept
A `BuildIntegrationTestCase`/unit test analogous to the existing `CreateZipWithMalformedExtraField` helper in `output_jar_simple_test.cc` [8](#0-7)  can be extended for `ijar`'s `zip.cc`: construct a minimal zip whose central directory header declares `file_name_length`/`extra_field_length` far larger than the actual remaining bytes in the mapped buffer, then invoke `InputZipFile::ProcessNext`/`ProcessCentralDirEntry` and observe an out-of-bounds read (detectable under ASan) rather than a graceful parse error, since no `EnsureRemaining`-style check exists in that function.

### Citations

**File:** third_party/ijar/zip.cc (L161-170)
```text
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

**File:** third_party/ijar/zip.cc (L360-369)
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
```

**File:** third_party/ijar/zip.cc (L510-512)
```text
  u2 file_name_length = get_u2le(p);
  u2 extra_field_length = get_u2le(p);
  u2 file_comment_length = get_u2le(p);
```

**File:** third_party/ijar/zip.cc (L516-522)
```text
  {
    size_t len = (file_name_length < filename_size)
      ? file_name_length
      : (filename_size - 1);
    memcpy(reinterpret_cast<void*>(filename), p, len);
    filename[len] = 0;
  }
```

**File:** third_party/ijar/zip.cc (L523-525)
```text
  p += file_name_length;
  const u1 *extra_p = p;
  p += extra_field_length;
```

**File:** third_party/ijar/zip.cc (L526-541)
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
```

**File:** third_party/ijar/zip.cc (L543-543)
```text
  p += file_comment_length;
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
