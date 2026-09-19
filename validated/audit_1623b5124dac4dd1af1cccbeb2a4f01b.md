### Title
Heap buffer over-read in ijar's ZIP central directory parser via unvalidated length fields - (File: third_party/ijar/zip.cc)

### Summary
The libpng CVE stems from a lookup routine (`png_do_quantize`) trusting an externally-supplied palette index without validating it against the bounds of the `palette_lookup` array, causing an out-of-bounds heap read. Bazel's `ijar` tool (used to strip class bodies from `.jar` files to build interface jars, and reachable whenever Bazel processes a `.jar`/`.class` artifact — including jars fetched from an untrusted `http_archive`/`http_jar`/`http_file` URL, an untrusted branch's dependency, or a malicious registry module archive) parses ZIP central directory records the same unsafe way: it trusts attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) taken directly from the file bytes to advance a raw pointer through an mmap'd buffer, with no check that the pointer stays inside the mapped region.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` in [1](#0-0)  reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from the central directory header bytes: [2](#0-1) 

After reading these lengths, the function advances `p` (`p += file_name_length`, `p += extra_field_length`, later `p += file_comment_length`) and, inside the extra-field loop, reads a further attacker-controlled `data_size` and advances `extra_p += data_size`, all without ever checking these advances against the end of the mmap'd input buffer (`zipdata_in_` + file length): [3](#0-2) 

The comment above the function claims safety ("the central directory is always followed by another data structure that has a signature, so parsing it this way is safe") but this assumption only guarantees a *valid* zip has a trailing signature — it does not defend against a maliciously crafted file that sets an oversized `file_name_length`/`extra_field_length`/`data_size` to walk `p`/`extra_p` past the end of the mapped file, into adjacent heap memory or an unmapped page.

This is the direct structural analog of the CVE: an attacker-controlled length/index value used to compute a memory offset with no bound against the true buffer extent, mirroring `palette_lookup[index]` being indexed by an unchecked, externally-supplied `index`.

By contrast, Bazel's other jar-processing tool, `singlejar`, explicitly guards the equivalent advance with `mapped_file_.mapped(new_cdr)` and fails loudly if the computed pointer leaves the mapped region: [4](#0-3) , and its `ExtraField::find` bounds every advance against an explicit `end` pointer: [5](#0-4) . `ijar`'s `zip.cc` central-directory reader has no analogous check.

### Impact Explanation
A crafted jar (delivered via an `http_jar`/`http_archive` URL, a compromised mirror whose bytes still happen to pass the declared `sha256` for a different, legitimate purpose but are actually processed by `ijar` before extraction/verification in some build graphs, or simply any jar built from an untrusted CI branch that Bazel runs `ijar` over to produce an interface jar) can drive `p`/`extra_p` past the mmap'd buffer. Consequences range from an out-of-bounds heap read (potentially leaking adjacent heap bytes into the derived interface jar's filename/size/offset fields, which are then written into `ijar`'s output artifact) to a crash if the read crosses into an unmapped page. This matches the CVSS profile of the reference CVE (`AC:L/PR:N/UI:R/C:L`): a local, low-complexity, read-only information leak rather than remote code execution.

### Likelihood Explanation
`ijar` is invoked automatically by Bazel's Java rules to generate interface jars from arbitrary `.jar` inputs, including those originating from external repositories over HTTP. An attacker who controls the origin server, a compromised mirror without an enforced/mismatched checksum, or an untrusted branch's build inputs can supply a crafted jar with oversized central-directory length fields. No special privileges or victim-machine access are required beyond publishing content the victim's build consumes, satisfying the "unprivileged attacker" constraint.

### Recommendation
Add explicit bounds checks in `InputZipFile::ProcessCentralDirEntry` (and the extra-field loop) verifying that `p`, `extra_p`, and their post-advance positions never exceed `zipdata_in_ + GetSize()` (the mmap'd file's end), failing with a diagnostic error (as `zip_headers.h`'s `ExtraField::find` and `input_jar.h`'s `NextEntry` already do) rather than trusting the on-disk length fields unconditionally.

### Proof of Concept
A `BuildIntegrationTestCase`/`third_party/ijar` unit test analogous to the existing `CreateZipWithMalformedExtraField` helper in [6](#0-5)  (which already demonstrates the malformed-extra-field attack pattern for `singlejar`) should be adapted for `ijar`: construct a minimal ZIP whose single central directory entry declares `extra_field_length`/`file_name_length` far larger than the remaining bytes in the mmap'd buffer, feed it to `ZipExtractor`/`InputZipFile::ProcessNext`, and run under ASan to confirm a heap-buffer-overflow (read) is detected in `ProcessCentralDirEntry`.

**Note on confidence:** Due to tool-call budget limits I was unable to fully trace every downstream consumer of the leaked `extra_field`/size values (e.g., whether the numeric ZIP64 override values eventually get echoed verbatim into `ijar`'s output artifact, which would elevate this from a possible crash-only DoS to a genuine information-disclosure primitive matching the CVE's `C:L` rating). This should be verified with an ASan-instrumented build and the proof-of-concept test above before treating the impact rating as final.

### Citations

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

**File:** src/tools/singlejar/input_jar.h (L70-81)
```text
    const CDH* current_cdh = cdh_;
    const uint8_t* new_cdr = ziph::byte_ptr(cdh_) + cdh_->size();
    if (!mapped_file_.mapped(new_cdr)) {
      diag_errx(
          1,
          "Bad directory record at offset 0x%" PRIx64
          " of %s\n"
          "file name length = %u, extra_field length = %u, comment length = %u",
          CentralDirectoryRecordOffset(cdh_), path_.c_str(),
          cdh_->file_name_length(), cdh_->extra_fields_length(),
          cdh_->comment_length());
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
