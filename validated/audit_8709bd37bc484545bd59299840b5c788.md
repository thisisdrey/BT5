### Title
Heap buffer over-read in ZIP central directory parsing via unchecked `extra_field_length`/`file_comment_length` - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from a memory-mapped ZIP central directory and advances the cursor `p`/`extra_p` by these attacker-controlled 16-bit values without ever checking them against the actual remaining size of the mapped file, unlike the analogous local-file-header parser `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before every variable-length read.

### Finding Description
`ijar` (the interface-jar generator used by Bazel's Java build actions, invoked from `zip_main.cc`/`ijar.cc`) opens a `.jar`/`.zip` file by `mmap`ing it (`InputZipFile::Open`, `MappedInputFile`) and locates the end-of-central-directory record with `FindZipCentralDirectory`, which does validate that `central_dir_offset + central_dir_size <= in_length` [1](#0-0) . However, once inside the central directory, each entry is parsed by `ProcessCentralDirEntry`, which reads `file_name_length`, `extra_field_length`, and `file_comment_length` from the mapped bytes and immediately does `p += file_name_length; ... p += extra_field_length; ... p += file_comment_length;` with **no bounds check at all** against `in_length` or against the declared `central_dir_size` [2](#0-1) .

Inside the extra-field loop, `header_id`/`data_size` are read from `extra_p`, and if `header_id == ZIP64_EXTRA_FIELD_TAG` the code unconditionally calls `get_u8le(extra)` up to three times without verifying that `data_size >= 8/16/24` or that `extra + 8/16/24` stays within the declared extra-field region or the mapped file at all [3](#0-2) . This is structurally the same bug class as CVE-2024-32613: a length field taken from untrusted, cached/serialized binary data is used to compute a read offset/size without validating it against the actual buffer extent, producing a heap (here, mmap'd file) buffer over-read.

By contrast, `ProcessLocalFileEntry`, which parses the local file header for the same file, explicitly guards every variable-length field with `EnsureRemaining()`: [4](#0-3) 
No equivalent guard exists on the central-directory path, and the function-level comment incorrectly asserts that "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" [5](#0-4) , which is not actually enforced by any check on `file_name_length`, `extra_field_length`, or `file_comment_length`.

### Impact Explanation
A crafted ZIP/JAR with an inflated `file_name_length`, `extra_field_length`, or `file_comment_length` in a central directory entry can push `p`/`extra_p` past the end of the `mmap`'d file region, causing `get_u2le`/`get_u4le`/`get_u8le`/`memcpy` (for the filename copy) to read out-of-bounds heap/mapped memory. This can crash the `ijar` process (denial of service for that build action) or, more relevant to this class of CVE, leak adjacent memory contents into the copied `filename` buffer or into computed sizes used later (`CalculateOutputLength`, output jar generation), potentially corrupting or disclosing memory the build process should not be able to read.

### Likelihood Explanation
`ijar` is invoked as part of ordinary Java build actions on jars that are inputs to the build graph, including externally supplied dependency jars (e.g., prebuilt jars pulled in via `java_import`, `http_jar`, or maven-style external repositories). If such a jar is fetched from an attacker-controlled or compromised origin without effective integrity pinning (or is otherwise attacker-influenced, e.g. via an untrusted CI branch that builds against a hostile source), the malformed central directory reaches `ProcessCentralDirEntry` unmodified — no sha256/checksum layer inspects the internal ZIP structure of the file, only its overall byte content. Since the bug requires only a single malformed ZIP central-directory record and no cooperation from any other Bazel subsystem, likelihood is moderate-to-high wherever `ijar` processes any non-self-produced jar.

### Recommendation
Add explicit bounds checks in `InputZipFile::ProcessCentralDirEntry` (and in the analogous ZIP64-extra-field loop) mirroring `EnsureRemaining()`'s pattern used in `ProcessLocalFileEntry`: validate `file_name_length`, `extra_field_length`, and `file_comment_length` against the actual remaining bytes in the mapped file/central directory region before advancing `p`, and validate `data_size` against the declared extra-field boundary and against the minimum size needed (8 bytes) before calling `get_u8le` inside the ZIP64 extra-field loop.

### Proof of Concept
A JUnit/`BuildIntegrationTestCase`-style or `third_party/ijar` unit test (in the style of the existing `zip_headers_test.cc`/`output_jar_simple_test.cc` malformed-extra-field tests [6](#0-5) ) should:
1. Construct a minimal ZIP file whose single central directory entry declares `extra_field_length` (or `file_comment_length`) larger than the number of bytes actually present before EOF, or whose Zip64 extra field declares `data_size < 8` while `header_id == ZIP64_EXTRA_FIELD_TAG`.
2. Feed this file to `ZipExtractor::Create`/`ProcessAll` (the entry point used by `ijar.cc`/`zip_main.cc`).
3. Run under AddressSanitizer; observe a heap-buffer-overflow (read) report originating in `InputZipFile::ProcessCentralDirEntry` (`get_u2le`/`get_u8le`/`memcpy` calls) confirming the over-read past the mapped file buffer. [7](#0-6)

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

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
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
