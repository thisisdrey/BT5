### Title
Out-of-bounds read via missing bounds checks when parsing ZIP/JAR central directory entries in ijar - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc`, used by the `ijar` tool to strip interface information from `.jar`/`.zip` files consumed during a build, reads fixed-width and variable-length fields (file name length, extra-field length, and the contents of each extra-field record) directly from an attacker-influenced buffer without any of the `EnsureRemaining()` bounds checks that its sibling function `ProcessLocalFileEntry` performs. [1](#0-0) 

### Finding Description
`ProcessLocalFileEntry` explicitly bounds-checks every variable-length read against the mapped input file size before dereferencing, via `EnsureRemaining()`: [2](#0-1) 

In contrast, `ProcessCentralDirEntry`, which parses each Central Directory Header entry (file name, extra field, comment lengths, and the Zip64 extra-field payload), performs no equivalent `EnsureRemaining` check before advancing the cursor `p`/`extra_p` and dereferencing memory: [3](#0-2) 

In particular, the extra-field parsing loop reads a 2-byte `header_id` and a 2-byte `data_size` and then advances `extra_p` by `data_size` and (if the tag matches `ZIP64_EXTRA_FIELD_TAG`) dereferences 8-byte values from `extra` — all without verifying that `extra_p + data_size` stays within `extra_field_length` or within the bounds of the memory-mapped file: [4](#0-3) 

A crafted `.jar`/`.zip` (e.g., fetched as a dependency archive, or supplied as a source artifact processed by `ijar` during interface-jar generation) with an oversized `file_name_length`, `extra_field_length`, or a malformed extra-field `data_size` can push these cursors past the end of the memory-mapped input, causing `get_u2le`/`get_u4le`/`get_u8le`/`memcpy` to read out-of-bounds heap/mmap memory — the same bug class (out-of-bounds read via crafted input driving unchecked length-prefixed field parsing) as the referenced Chrome CVE.

### Impact Explanation
An out-of-bounds read in a build tool can leak adjacent heap memory contents into build outputs or crash the build process (information disclosure / instability), triggered purely by supplying a malformed archive that ijar processes — no local machine access or credentials required.

### Likelihood Explanation
Reaching this code path only requires ijar to process a `.jar`/`.zip` file whose bytes are attacker-influenced (e.g. a dependency fetched over the network or a source archive from an untrusted branch); no additional privileges are needed. However, I was unable to fully confirm within this investigation whether earlier steps in `InputZipFile::Open`/`CalculateOutputLength` already validate the address/length of the central directory record itself before entries are iterated (this would constrain, but not necessarily eliminate, the reachable OOB window for the per-entry length fields). This is a caveat on full certainty of exploitability path, not on the presence of the missing checks in `ProcessCentralDirEntry` itself.

### Recommendation
Add `EnsureRemaining()`-equivalent bounds checks in `ProcessCentralDirEntry` before reading `file_name_length`, `extra_field_length`, `file_comment_length`, and before/while iterating extra-field records (validating `data_size` against remaining bytes in both the extra-field region and the overall mapped file), mirroring the checks already present in `ProcessLocalFileEntry`.

### Proof of Concept
A reproducible test would construct a minimal ZIP/JAR with a Central Directory Header whose `extra_field_length` (or an internal extra-field `data_size`) is set larger than the remaining bytes in the mapped file, then invoke ijar's processing (`InputZipFile::ProcessNext` → `ProcessCentralDirEntry`) on it, asserting that the tool detects the truncation/malformed field rather than reading past the buffer end (analogous to the existing `CreateZipWithMalformedExtraField` pattern used for local/central header extra fields in `src/tools/singlejar/output_jar_simple_test.cc`, but exercised against `third_party/ijar/zip.cc`'s `InputZipFile`). [5](#0-4)

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
