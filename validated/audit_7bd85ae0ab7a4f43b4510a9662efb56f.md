Confirmed: `run_ijar`/`ijar` (`third_party/ijar/zip.cc`, `third_party/ijar/ijar.cc`, `third_party/ijar/zip_main.cc`) directly parses attacker-supplied JAR/ZIP files (e.g. a malicious dependency jar fetched via `http_jar`/`maven_install`/a hostile registry) as part of the normal `java_common.run_ijar` build action that produces compile-time interface jars [1](#0-0) . The core parser `InputZipFile::ProcessCentralDirEntry` reads the ZIP central-directory fields (`compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `offset`) and then walks embedded "extra field" TLV records purely by trusting attacker-controlled 16-bit length fields, with **no `EnsureRemaining`/bounds check** against the mapped file's end before dereferencing [2](#0-1) . This is in sharp contrast to `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every variable-length read [3](#0-2) .

### Title
Unbounded central-directory / extra-field parsing in ijar's ZIP reader causes heap out-of-bounds read on malicious JAR/ZIP dependency - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses the ZIP central directory of an mmap'd input file entirely without bounds checking, unlike its sibling `ProcessLocalFileEntry`. A crafted `compressed_file_size`/`file_name_length`/`extra_field_length`/`file_comment_length`, or a crafted extra-field `data_size`, can advance the cursor `p`/`extra_p` past the end of the mapped file, causing `get_u4le`/`get_u2le`/`get_u8le`/`memcpy` to read out of the mapped region. This directly mirrors the dnsmasq `extract_addresses()`/`extract_name()` bug class: a length field taken from untrusted data advances a read cursor past the record's real end with no bound imposed by the surrounding buffer size.

### Finding Description
`ProcessCentralDirEntry` reads the fixed 46-byte CDH fields off the raw pointer `p` (`get_u4le`, `get_u2le`, etc.) without ever calling the file-length-aware `EnsureRemaining()` helper that guards every read in `ProcessLocalFileEntry` [4](#0-3) [5](#0-4) . After copying `file_name_length` bytes into a fixed `filename` buffer (itself bounded, but the *source* read is not proven in-bounds), the code advances `p += file_name_length` and then walks a run of "extra field" TLV records:

```
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  ...
}
``` [6](#0-5) 

Every value (`file_name_length`, `extra_field_length`, `file_comment_length`, and each extra field's `data_size`) is taken verbatim from attacker-controlled bytes with no comparison against `input_file_->Length()` or against `zipdata_in_ + in_offset_`. A crafted `data_size` that doesn't align to `extra_field_length` (e.g. `data_size` larger than the remaining extra-field bytes) causes the `while (extra_p != p)` loop condition to be skipped over (pointer inequality, not "less than"), so `extra_p` can run past `p` and past the mapped file entirely, with `get_u2le`/`get_u8le` dereferencing unmapped memory on subsequent iterations if `extra_p` doesn't exactly equal `p` — or, if it does land past the true buffer end but before the OS page boundary, silently reads heap-adjacent bytes into `*uncompressed_size`/`*compressed_size`/`*offset`, which are later used by `ProcessNext()`/`ProcessLocalFileEntry()` as trusted values that gate further reads (`p = zipdata_in_ + in_offset_ + offset`).

This same tainted `*offset`, `*compressed_size`, and `*uncompressed_size` are then used as authoritative values seeding the local-file-header parse, meaning corrupted/out-of-bounds-read values leak into subsequent parsing/output logic that ultimately writes an "interface jar" to disk.

### Impact Explanation
An attacker who controls the content of a JAR/ZIP file that a Bazel build fetches as a dependency (e.g., a malicious artifact from a Maven repository referenced by `maven_install`/`http_jar`, or any target that runs `java_common.run_ijar` / the `ijar` binary on downloaded content) can craft a central directory whose length fields cause `ijar` to read past the end of the memory-mapped input file. Depending on heap/page layout this results in a crash (denial of service on the build) or a heap out-of-bounds read whose bytes influence buffer offsets used for subsequent parsing, potentially reading additional out-of-bounds memory content into build outputs. It does not, by itself, demonstrate arbitrary write/RCE, but it is a memory-safety violation over data supplied by an unprivileged, remote, hostile origin (mirror or malicious package publisher), and integrity checks (`sha256`/`integrity` on the *download*) do not protect against malformed-but-checksum-matching archive-internal structure, since the checksum only validates the raw bytes, not the ZIP structure's internal consistency.

### Likelihood Explanation
`ijar` is invoked automatically as part of ordinary Java builds through `java_common.run_ijar`, which is used to build compile-time interface jars for essentially all `java_library` targets and dependencies (including externally-downloaded jars) [1](#0-0) . Any project consuming a Java dependency from an untrusted or compromised mirror/registry will run this parser on attacker-supplied bytes with default flags, with no opt-in required, making the reachable path common and low-effort to trigger. However, the CVSS severity should be treated as reduced (crash/OOB-read, no direct proof of memory corruption beyond read) compared to the referenced dnsmasq analog, absent a concrete demonstrated write-primitive.

### Recommendation
Add bounds checks in `ProcessCentralDirEntry` mirroring `EnsureRemaining()` in `ProcessLocalFileEntry`: validate `file_name_length`, `extra_field_length`, `file_comment_length`, and each extra field's `data_size` against the remaining bytes in the mapped file (`input_file_->Length() - (p - zipdata_in_)`) before consuming them, and fail closed (return an error) instead of silently under/over-reading. The `while (extra_p != p)` loop should be `while (extra_p < p)` with an explicit check that `get_u2le(extra_p)+get_u2le(extra_p)+data_size <= (p - extra_p_saved)` before advancing, matching the equivalent defensive pattern already used in `src/tools/singlejar/zip_headers.h`'s `ExtraField::find` [7](#0-6) .

### Proof of Concept
A JUnit/shell reproduction would construct a minimal ZIP with:
1. A valid End-Of-Central-Directory record pointing to one central directory entry.
2. A central directory header (`CENTRAL_FILE_HEADER_SIGNATURE`) at the very end of the mapped file, with `extra_field_length` set to a small value (e.g., 4), but whose first (and only) extra-field TLV declares `data_size` far larger than the remaining bytes in the file (e.g., `0xFFFF`), causing `extra_p` to run past the file end.
3. Invoking `third_party/ijar/zip_main.cc`'s `extract`/ijar entry point (or `bazel-bin/.../ijar path/to/malicious.jar out.jar`) on this file, run under AddressSanitizer, to observe a heap-buffer-overflow read report at the `get_u2le(extra_p)`/`get_u8le(extra)` call sites in `ProcessCentralDirEntry`.

This is analogous to `src/tools/singlejar/output_jar_simple_test.cc`'s existing `CreateZipWithMalformedExtraField()` helper, which constructs exactly this kind of malformed-extra-field ZIP to test the (already bounds-checked) `singlejar` extra-field walker [8](#0-7) ; an equivalent fixture targeting `ijar`'s unchecked `ProcessCentralDirEntry` would demonstrate the same class of bug in the unguarded code path.

### Citations

**File:** docs/versions/9.0.0/rules/lib/toplevel/java_common.mdx (L117-120)
```text
## run\_ijar

```
File java_common.run_ijar(actions, *, jar, target_label=None, java_toolchain)
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

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1179-1224)
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
```
