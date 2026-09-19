### Title
Out-of-bounds heap read parsing the ZIP central directory extra fields in `InputZipFile::ProcessCentralDirEntry` - ([File: third_party/ijar/zip.cc])

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar's zip reader (used by `zipper`/`singlejar`-adjacent tooling and reachable indirectly whenever Bazel processes a zip/jar produced from attacker-controlled archive bytes, e.g. via `http_archive`/`download_and_extract` unpacking a hostile `.zip`) parses the ZIP64 extra-field sub-records inside a central directory entry without validating `data_size` against the bounds of the extra-field region or the overall mapped file, similarly to how GStreamer's `qtdemux_parse_trak` walked MP4 atoms using attacker-controlled length fields without bounds checks.

### Finding Description
`ProcessCentralDirEntry` reads the fixed fields of a Central Directory Header, then computes `extra_field_length` and iterates over it: [1](#0-0) 

The loop condition is `while (extra_p != p)`, where `p = extra_p_start + extra_field_length` is the intended end of the extra-fields region. On each iteration it reads a 2-byte `header_id` and 2-byte `data_size` via `get_u2le`, then does `extra_p += data_size`, with **no check that `data_size` (attacker-controlled, up to 0xFFFF) keeps `extra_p` within `[extra_p_start, p)`, nor even within the bounds of the memory-mapped input file**. If a hostile ZIP entry supplies a `data_size` that doesn't evenly divide the region (e.g., overshoots `p`), the loop's `extra_p != p` termination check can be skipped, causing `extra_p` to run past the end of the extra field, past the end of the current central-directory entry, and potentially past the end of the memory-mapped input buffer, followed by additional `get_u2le`/`get_u8le` reads at that runaway location. This mirrors the "read past end of buffer" primitive from the analog report, but operating on ZIP central directory parsing instead of MP4 `trak` atoms.

Contrast this with the equivalent local-header path, which does call `EnsureRemaining()` before consuming `file_name_length_`/`extra_field_length_` bytes: [2](#0-1) 

but `ProcessCentralDirEntry` has no analogous `EnsureRemaining`-style check at all — it just advances a raw pointer, and the header comment even asserts (without proof) that this is "always safe": [3](#0-2) 

The singlejar side of the codebase (`src/tools/singlejar/zip_headers.h`) contains a more defensive `ExtraField::find` that explicitly bounds-checks each sub-field against `end` before dereferencing: [4](#0-3) 
which shows the codebase already has the correct pattern elsewhere, underscoring that the ijar path in `zip.cc` is missing an equivalent guard.

### Impact Explanation
An out-of-bounds read while parsing a hostile ZIP's central directory extra fields can, at minimum, disclose adjacent heap/mmap memory content (via `get_u8le`/`get_u4le` interpreted as offsets/sizes that get propagated into `compressed_size`/`uncompressed_size`/`offset` output values, which are then used to locate and copy "file data" — potentially copying attacker-adjacent memory into the extracted output) or crash the tool if the read crosses an unmapped page boundary. This satisfies the "read outside the repository / exec root / output base" category in spirit (reading outside the intended parsed structure into adjacent process memory) triggered purely by a malicious archive that a build fetches and extracts — no compromise of the build machine or credentials is required.

### Likelihood Explanation
Any attacker who can serve a ZIP/JAR file consumed by Bazel's build (e.g. as an `http_archive`/`download_and_extract` source, or a dependency `.jar` fed into ijar-based interface-jar generation) can craft the extra-field bytes of any Central Directory Header entry with an oversized/misaligned `data_size` for the ZIP64 tag (`0x0001`) or an unrelated tag, since these values are entirely attacker-controlled and are not validated against remaining buffer length before use. This is a purely data-driven bug requiring no privileged access — it fits the "hostile origin server / archive content" attacker model in the ruleset.

### Recommendation
Add bounds validation in `InputZipFile::ProcessCentralDirEntry`'s extra-field loop, mirroring `EnsureRemaining()`/`ExtraField::find`'s pattern: verify that at least 4 bytes remain before reading `header_id`/`data_size`, and that `extra_p + data_size <= p` (and that `p` itself does not exceed the mapped file's end) before advancing `extra_p`, aborting/erroring out on violation rather than silently overrunning.

### Proof of Concept
A concrete JUnit/`BuildIntegrationTestCase`-style reproduction would construct a minimal ZIP whose single Central Directory Header entry declares `extra_field_length` with a ZIP64 extra field (tag `0x0001`) whose `data_size` field is set larger than the remaining extra-field bytes (analogous to the existing `CreateZipWithMalformedExtraField` helper used for a related singlejar test): [5](#0-4) 
Feeding this archive through the ijar `InputZipFile::Open`/`ProcessNext`/`ProcessCentralDirEntry` path and observing an ASan heap-buffer-overflow (read) or a return of an obviously out-of-bounds `offset`/`compressed_size` value derived from adjacent memory would confirm the vulnerability. I was not able to directly execute this PoC or fully trace all downstream callers of `ProcessCentralDirEntry` (e.g., whether ijar's own build/BUILD wiring is reachable from a standard `bazel build` on attacker content) within the scope of this read-only analysis — a Devin session with full build/test tooling would be needed to compile ijar with AddressSanitizer and confirm the crash empirically.

### Citations

**File:** third_party/ijar/zip.cc (L356-370)
```text
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

**File:** third_party/ijar/zip.cc (L486-496)
```text
// - whether the entry is a class file (to be included in the output).
// Precondition: p points to the beginning of an entry in the central dir
// Postcondition: p points to the beginning of the next entry in the central dir
// Returns true if the central directory contains another file and false if not.
// Of course, in the latter case, the size output variables are not changed.
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
bool InputZipFile::ProcessCentralDirEntry(const u1 *&p, u8 *compressed_size,
                                          u8 *uncompressed_size, char *filename,
                                          size_t filename_size, u4 *attr,
                                          u8 *offset) {
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

**File:** src/tools/singlejar/zip_headers.h (L97-115)
```text
class ExtraField {
 public:
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
