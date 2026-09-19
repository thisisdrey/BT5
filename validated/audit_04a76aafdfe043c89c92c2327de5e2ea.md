### Title
Out-of-bounds heap read when parsing ZIP central-directory extra fields with an unvalidated `data_size` - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar's ZIP reader parses the "extra field" block of each central-directory entry without validating declared lengths against the memory-mapped input buffer, exactly analogous to CVE-2023-4458's unchecked xattr length parsing in ksmbd. A hostile jar (e.g. a maven/http_jar artifact an unprivileged party publishes) can drive the parser to read past the end of the mapped file.

### Finding Description
`FindZipCentralDirectory` bounds-checks only the aggregate central-directory offset/size against the file length: [1](#0-0) . It never validates the per-entry `file_name_length`, `extra_field_length`, or `comment_length` fields that `ProcessCentralDirEntry` subsequently trusts.

`ProcessCentralDirEntry` reads `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, and `offset` directly from attacker-supplied bytes with **no `EnsureRemaining`/bounds check at all** (contrast this with `ProcessLocalFileEntry`, which does call `EnsureRemaining` before reading variable-length fields): [2](#0-1) .

It then walks the extra-field records using an attacker-controlled `data_size` with no check that `extra_p + data_size` stays within `[extra_p, p)` or within the mapped buffer end: [3](#0-2) 

If a crafted entry sets `data_size` for a fake `ZIP64_EXTRA_FIELD_TAG` (or any tag) larger than the remaining bytes in the extra field, `extra_p` overshoots `p`. The loop condition `while (extra_p != p)` never becomes false in that case, so parsing continues reading `header_id`/`data_size` from memory beyond the intended extra-field block, beyond the declared central directory, and potentially beyond the memory-mapped file itself — the file is `mmap`-backed (`MappedInputFile`), so reads past its length are heap/page out-of-bounds reads, not bounded array accesses: [4](#0-3) .

Because `*compressed_size`, `*uncompressed_size`, and `*offset` can be overwritten from this OOB-derived `get_u8le(extra)` value, the corrupted `offset` is later used to reposition the read cursor (`p = zipdata_in_ + in_offset_ + offset;`) in `ProcessNext`, and the only subsequent guard, `EnsureRemaining`, computes `remaining = input_file_->Length() - in_offset` with unsigned arithmetic — a sufficiently large/garbage `offset` causes `in_offset` to exceed `Length()`, underflowing `remaining` to a huge value and defeating the check: [5](#0-4)  and [6](#0-5) .

This is the same bug class as CVE-2023-4458: a length field taken from untrusted, attacker-supplied structured data (extended attribute record / zip extra-field record) is used to advance a read cursor without validating it stays inside the allocated buffer, yielding an out-of-bounds read.

### Impact Explanation
ijar is invoked by Bazel to strip interface jars from ordinary jar files that are build inputs (including jars fetched from external, unprivileged-controlled sources such as Maven repositories via `http_jar`/`maven_install`). A jar whose bytes are entirely attacker-chosen (the attacker is the origin publishing the artifact) reaches this parser. The resulting out-of-bounds read can disclose adjacent process heap memory into computed size/offset values that influence subsequent parsing and, potentially, the generated ijar output, and can also crash the ijar action. This matches CVE-2023-4458's classification as an information-disclosure primitive rooted in unchecked length parsing.

### Likelihood Explanation
Likelihood is moderate to high for triggering the read: constructing a ZIP central directory with an oversized `data_size` in one entry's extra field is trivial and requires no special privileges — only the ability to publish a jar that a victim's build fetches and passes through ijar (e.g., as a `java_import`/`maven_install` dependency). No credential, host, or output-base access is needed, satisfying the "unprivileged attacker publishing content" threat model. I was not able to fully verify (within the available search/read budget) the exact allocation/guard-page behavior of `MappedInputFile` to confirm how far past `Length()` reads can go before hitting an unmapped page and whether a checksum is normally pinned for such jars in default Bazel usage (e.g., `http_jar`'s `sha256` is optional), which would materially affect exploitability and should be validated by a background agent with build/test access.

### Recommendation
In `ProcessCentralDirEntry` (and `MaybeReadZip64CentralDirectory`/`FindZip64CentralDirectory` similarly), add explicit bounds checks before consuming any length-prefixed field:
- Verify `file_name_length + extra_field_length + comment_length` does not exceed the number of bytes remaining before the mapped file's end (and ideally before the declared central-directory end).
- In the extra-field loop, verify `extra_p + 4 <= p` before reading `header_id`/`data_size`, and verify `extra_p + data_size <= p` before advancing, aborting with an error (mirroring the containment checks already used in `src/tools/singlejar/zip_headers.h`'s `ExtraField::find`, e.g. [7](#0-6) ) instead of trusting attacker-controlled sizes unconditionally.
- Guard `EnsureRemaining`'s subtraction against underflow when `in_offset > input_file_->Length()`.

### Proof of Concept
A reproducible test can be added under `third_party/ijar/zip_test.cc` (or a new JUnit/shell integration test) that:
1. Builds a minimal ZIP file with one central-directory entry whose `extra_field_length` covers a single 4-byte extra-field header claiming `data_size = 0xFFFF` (far larger than the actual remaining bytes in the extra-field/central-directory region and the mapped file).
2. Invokes `ZipExtractor::Create`/`ProcessAll` (or directly `FindZipCentralDirectory` + `ProcessCentralDirEntry`) on this crafted file under AddressSanitizer.
3. Observes a heap-buffer-overflow (read) reported by ASan when the extra-field loop advances `extra_p` past the mapped buffer, analogous to the `CreateZipWithMalformedExtraField` pattern already used for the singlejar bounds-check regression test: [8](#0-7) .

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

**File:** third_party/ijar/zip.cc (L302-330)
```text
bool InputZipFile::ProcessNext() {
  // Process the next entry in the central directory. Also make sure that the
  // content pointer is in sync.
  u8 compressed, uncompressed;
  u8 offset;
  if (!ProcessCentralDirEntry(central_dir_current_, &compressed, &uncompressed,
                              filename, PATH_MAX, &attr, &offset)) {
    return false;
  }

  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

  if (EnsureRemaining(4, "signature") < 0) {
    return false;
  }
  u4 signature = get_u4le(p);
  if (signature == LOCAL_FILE_HEADER_SIGNATURE) {
    if (ProcessLocalFileEntry(compressed, uncompressed) < 0) {
      return false;
    }
  } else {
    error("local file header signature for file %s not found\n", filename);
    return false;
  }

  return true;
}
```

**File:** third_party/ijar/zip.cc (L493-523)
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

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```

**File:** third_party/ijar/zip.cc (L822-830)
```text
  void *zipdata_in = input_file->Buffer();
  u8 central_dir_offset;
  const u1 *central_dir = NULL;

  if (!devtools_ijar::FindZipCentralDirectory(
          static_cast<const u1*>(zipdata_in), input_file->Length(),
          &central_dir_offset, &central_dir)) {
    errno = EIO;  // we don't really have a good error number
    error("Cannot find central directory");
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
