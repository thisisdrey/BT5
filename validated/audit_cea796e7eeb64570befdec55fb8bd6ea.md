I have enough to write up the analog now.

### Title
Unvalidated ZIP64 extra-field/central-directory offset in `InputZipFile::ProcessCentralDirEntry` causes out-of-bounds memory read via integer-underflow bypass of `EnsureRemaining` - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar`'s ZIP/JAR central-directory parser reads the Zip64 extra-field subfield size (`data_size`) and the local-header `offset` field directly from attacker-controlled bytes without validating them against the declared `extra_field_length` or the mapped file bounds. A crafted offset/size can make the cursor `p` point outside the mapped input buffer, after which the only remaining bounds check, `EnsureRemaining`, computes `remaining = input_file_->Length() - in_offset` using unsigned arithmetic. Because `in_offset` (`p - zipdata_in_`) can exceed `Length()`, this subtraction underflows to a huge value, so the check silently passes and subsequent reads (`get_u4le`, `get_u2le`, `memcpy` of the "filename") pull memory out of the mapped file's bounds. This mirrors CVE-2024-31950's root cause: a length/size field taken from untrusted wire data is used to advance a cursor and is trusted for a subsequent bounds check without being validated against the container's actual declared size.

### Finding Description
In `ProcessCentralDirEntry` [1](#0-0) , the loop walking Zip64 extra-field records reads `data_size` from the file and advances `extra_p` by it, and also overwrites `*offset`/`*compressed_size`/`*uncompressed_size` from `get_u8le(extra)` with **no check** that `data_size` fits inside the extra field, and no check that these fields' declared payload actually has the required 8 bytes present: [2](#0-1) 

The resulting (potentially attacker-forged) `offset` is then used unconditionally to reposition the read cursor in `ProcessNext`: [3](#0-2) 

The only safety net, `EnsureRemaining`, computes remaining bytes with unsigned subtraction that underflows when `p` has been pushed past the end of the mapped file by the forged offset: [4](#0-3) 

Once `remaining` wraps to a huge value, every subsequent size check in `ProcessLocalFileEntry` (file name length, extra field length, compressed data length) passes even though `p` points outside the mmap'd region, and the code proceeds to `memcpy`/read from that wild pointer, e.g. filename copy in `ProcessCentralDirEntry`: [5](#0-4) 
and file-data read in `ProcessFile`: [6](#0-5) 

This class of parser is reached whenever Bazel runs `ijar` to generate interface jars, including for precompiled `.jar` dependencies obtained via `http_jar`/`http_archive`/`java_import`-style external repositories. The integrity check on the download (declared `sha256`/`integrity`) only verifies the raw bytes of the archive match; it does nothing to validate that the internal ZIP central-directory/extra-field structure is internally consistent. Thus a hostile origin server can publish a `.jar` (or a jar packed inside a tarball) whose bytes match a pinned checksum but whose ZIP metadata is deliberately malformed, and still trigger this out-of-bounds read when Bazel processes it with `ijar`.

### Impact Explanation
An out-of-bounds/wild-pointer read in the `ijar` binary that runs as a normal build action. Depending on the crafted offset, this can:
- crash the `ijar` action (denial of service), or
- read adjacent process/heap memory and pass it into `filename`, `compressed_size`, `uncompressed_size`, and ultimately into `processor->Process(...)`, which writes bytes into the generated interface jar — an information-disclosure channel from the `ijar` process's memory into a build output artifact that a user's build/CI may propagate further (e.g., into a package or cache entry consumed by others).

### Likelihood Explanation
Any external repository/dependency fetch that supplies a jar to Bazel's Java toolchain (via `http_jar`, `http_archive` + `java_import`, precompiled dependency in a tarball, etc.) triggers `ijar` processing. An attacker who controls the served bytes of that dependency (and can still satisfy the declared checksum, since checksums do not validate internal ZIP structural consistency) can trivially embed a malformed Zip64 extra field / central directory entry to trigger this path. No privileged access, credentials, or MITM is required — only control over content the build fetches.

### Recommendation
In `ProcessCentralDirEntry` (and the mirrored `ProcessLocalFileEntry`), validate:
1. That the Zip64 extra-field `data_size` fits within the remaining bytes of the declared `extra_field_length` before advancing `extra_p` or reading `get_u8le`.
2. That `data_size` is large enough to actually contain the 8/16/24-byte quantities being read before reading them.
3. That the computed local-header `offset` (whether from the 32-bit field or the Zip64 override) is within `[0, input_file_->Length())` before it is used to reposition `p`, independent of `EnsureRemaining`'s underflow-prone subtraction.
4. Change `EnsureRemaining` to use a check that cannot underflow, e.g. `if (p < zipdata_in_ || static_cast<size_t>(p - zipdata_in_) > input_file_->Length() || n > input_file_->Length() - (p - zipdata_in_))`, guarding the subtraction with an explicit `p` range check first.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style reproduction (analogous to `output_jar_simple_test.cc`'s `CreateZipWithMalformedExtraField`, which already demonstrates crafting a CDH extra field with an oversized `payload_size`): [7](#0-6) 

1. Build a minimal ZIP with one Central Directory Header entry whose Zip64 extra field declares `data_size` covering only, say, 4 bytes but from which the parser still attempts a `get_u8le` (reading 8 bytes) for `uncompressed_size`/`compressed_size`/`offset`, and set the 32-bit `local_header_offset` field to `0xFFFFFFFF` so the Zip64-provided (forged, oversized) 8-byte `offset` is used verbatim.
2. Feed this file to `ijar`'s `InputZipFile` (via the `ijar` binary or a unit test using `ZipExtractor::Create`) and observe that `ProcessNext` computes `p = zipdata_in_ + in_offset_ + offset` pointing outside the mapped file, and that `EnsureRemaining` fails to reject it due to the unsigned-subtraction underflow in `remaining = input_file_->Length() - in_offset`, leading to an out-of-bounds read (crash under ASan, or garbage data copied into `filename`/output) instead of a clean parse error.

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

**File:** third_party/ijar/zip.cc (L312-318)
```text
  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

  if (EnsureRemaining(4, "signature") < 0) {
    return false;
  }
```

**File:** third_party/ijar/zip.cc (L457-479)
```text
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
  }
  processor->Process(filename, attr, file_data, uncompressed_size_);
  return 0;
```

**File:** third_party/ijar/zip.cc (L493-522)
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
```

**File:** third_party/ijar/zip.cc (L524-542)
```text
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
