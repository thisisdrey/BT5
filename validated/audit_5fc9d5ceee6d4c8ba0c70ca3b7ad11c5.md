Based on my investigation, I found a genuine analog in Bazel's own C++ ZIP/JAR parsing code that mirrors the CVE-2018-20430 bug class (an unchecked length-prefixed field triggering an out-of-bounds read while parsing untrusted archive content).

### Title
Out-of-bounds heap read parsing a malformed Extra Field in a JAR's Central Directory - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar's mmap-based ZIP reader parses `file_name_length`, `extra_field_length`, and per-record extra-field `header_id`/`data_size` fields straight out of attacker-controlled bytes without any bounds check against the actual memory-mapped file extent, unlike the equivalent, hardened code path in `src/tools/singlejar`.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` [1](#0-0)  reads `file_name_length`, `extra_field_length`, and `file_comment_length` via `get_u2le(p)` directly from the mmap'd central directory buffer and advances the cursor `p` by these attacker-supplied lengths with **no call to `EnsureRemaining`** (the bounds-check helper that *is* used elsewhere in the same file for local-file-header fields, e.g. `ProcessLocalFileEntry`/`SkipFile`/`ProcessFile`) [2](#0-1) .

Worse, the extra-field walking loop:
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
``` [3](#0-2)  only terminates on **exact pointer equality** (`extra_p != p`), never on `extra_p >= p` or against the true end of the mapped file. A crafted extra field whose `data_size` does not evenly divide the declared `extra_field_length` causes `extra_p` to overshoot `p` and keep looping, reading 4+ attacker-influenced bytes at a time past the intended extra-field region — and potentially past the end of the memory-mapped input file itself — until it happens to coincide with `p` again (extremely unlikely) or the process faults. This is architecturally identical to the CVE-2018-20430 bug class: a length field taken from untrusted, structured binary content (an OLE2/ZIP metadata length) is trusted to walk a buffer without confirming it stays within the buffer bounds.

By contrast, Bazel's other ZIP implementation (`src/tools/singlejar`) explicitly guards this exact case: `ExtraField::find` checks `start + sizeof(ExtraField) > end` and `start + extra_field->size() > end` before dereferencing [4](#0-3) , and `OutputJar::AppendToDirectoryBuffer` additionally asserts on malformed extra fields with `diag_errx` [5](#0-4) , which is directly covered by the `MalformedExtraField` death test [6](#0-5) . `third_party/ijar/zip.cc` has no equivalent guard or test.

### Impact Explanation
`ijar` runs as a native build action to produce interface JARs for Java compilation (`java_library`/`java_import` targets), operating directly on the mmap'd bytes of an input JAR (`MappedInputFile`) supplied by the build — including third-party JARs fetched from a Maven registry/mirror and pinned only by a `sha256`. A hostile origin server can publish a JAR whose bytes it fully controls (and thus whose sha256 it can compute correctly) that contains a corrupted central-directory extra field. When Bazel later builds against that dependency, ijar mmaps and parses it, and the unbounded extra-field walk can read past the mapped file region, producing an out-of-bounds heap/mmap read that can crash the build worker or leak adjacent process memory content into ijar's internal buffers/derived output. Because the checksum only certifies that the bytes match what the attacker chose to publish — not that the structure is safe to parse — pinning does not stop this class of bug.

### Likelihood Explanation
Reaching this code requires only that a project depend on a third-party JAR (a common, everyday occurrence via `http_jar`, `maven_install`, or a `java_import`), and that the malicious origin author crafts the ZIP's central-directory extra field with a `data_size` that doesn't evenly tile `extra_field_length`. No privileged access, no MITM, and no violation of the declared/pinned hash is required — the attacker only needs to control the artifact content once, at publish time.

### Recommendation
Apply the same bounds discipline used in `src/tools/singlejar/zip_headers.h`/`output_jar.cc` to `third_party/ijar/zip.cc`:
- Call `EnsureRemaining` (or an equivalent check against `input_file_->Length()`/the mapped region) before consuming `file_name_length`, `extra_field_length`, and `file_comment_length` in `ProcessCentralDirEntry`.
- Change the extra-field walk's loop condition from `extra_p != p` to `extra_p < p` (and further bound each field read by `extra_p + 4 <= p` before dereferencing `header_id`/`data_size`, and `extra_p + data_size <= p` before advancing), matching `ExtraField::find`'s guarded pattern.
- Add a fuzz/unit test (analogous to `OutputJarSimpleTest.MalformedExtraField`) that feeds ijar a JAR whose extra field length is inconsistent with its declared size and asserts a clean parse error instead of continuing to walk memory.

### Proof of Concept
Construct a minimal ZIP/JAR with one central-directory entry where `extra_field_length` is small (e.g., 6 bytes) but the first embedded extra-field record declares `data_size = 0xF000` (analogous to the existing `CreateZipWithMalformedExtraField` helper in `output_jar_simple_test.cc`, but targeting `third_party/ijar`'s `zip_test`/`ZipExtractor` path instead of singlejar). Feed this file to `ZipExtractor::Create(...)->ProcessAll()` (as used by ijar's main entry point) under a memory-sanitized build (ASan/MSan). The extra-field loop in `ProcessCentralDirEntry` will advance `extra_p` past the declared extra-field region and past `p`, continuing to call `get_u2le` on unrelated/out-of-bounds memory, which ASan reports as a heap-buffer-overflow (read) — confirming the OOB read without any crash-safe bounds check present in this file.

### Citations

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

**File:** src/tools/singlejar/zip_headers.h (L99-114)
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
```

**File:** src/tools/singlejar/output_jar.cc (L935-940)
```text
  for (const ExtraField* ef = ef_begin; ef < ef_end; ef = ef->next()) {
    if (ziph::byte_ptr(ef) + sizeof(ExtraField) > ziph::byte_ptr(ef_end) ||
        ziph::byte_ptr(ef) + ef->size() > ziph::byte_ptr(ef_end)) {
      diag_errx(1, "malformed extra field in CDH for %.*s",
                (int)cdh->file_name_length(), cdh->file_name());
    }
```

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1179-1236)
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

TEST_F(OutputJarSimpleTest, MalformedExtraField) {
  string out_path = OutputFilePath("out.jar");
  string bad_jar = OutputFilePath("malformed.jar");
  ASSERT_TRUE(
      blaze_util::WriteFile(CreateZipWithMalformedExtraField(), bad_jar));

  ParseCommandLine(out_path, {"--sources", bad_jar});
  OutputJar output_jar(&options_);
  ASSERT_DEATH(output_jar.Doit(), "malformed extra field");
}
```
