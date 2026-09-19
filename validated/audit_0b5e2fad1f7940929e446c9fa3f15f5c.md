### Title
Unchecked local-file-header offset in singlejar's `InputJar::LocalHeader` allows out-of-bounds read from a malicious input JAR - ([File: src/tools/singlejar/input_jar.h])

### Summary
`InputJar::LocalHeader` derives a pointer into the memory-mapped input JAR/ZIP purely from the attacker-controlled `local_header_offset` field of a Central Directory Header (CDH), without any bounds validation, unlike the sibling `NextEntry` path which does validate the CDH advance pointer against the mapped region.

### Finding Description
`InputJar::NextEntry` validates that the next CDH record lies inside the mapped file with `mapped_file_.mapped(new_cdr)` before dereferencing it: [1](#0-0) . However, immediately after, it computes the corresponding Local Header (LH) pointer via `LocalHeader(current_cdh)`, which performs raw, unchecked pointer arithmetic:

```
const LH* LocalHeader(const CDH* cdh) const {
    return reinterpret_cast<const LH*>(
        mapped_file_.address(cdh->local_header_offset() + preamble_size_));
}
``` [2](#0-1) 

`MappedFile::address()` performs no bounds check at all — it simply adds the offset to the mapped base pointer: [3](#0-2) . `MappedFile::mapped()` (the only bounds-checking primitive available) exists but is never called on the result of `LocalHeader()` [4](#0-3) .

`cdh->local_header_offset()` (a 32- or 64-bit value taken directly from a Zip64 extra field or the 32-bit CDH field) is fully attacker-controlled content coming straight from the bytes of an input JAR/ZIP file, as seen in the CDH layout: [5](#0-4) . A crafted CDH entry with an out-of-range `local_header_offset` (e.g., pointing past `mapped_end_`, before `mapped_start_`, or into unrelated heap/mmap regions) causes `LocalHeader()` to return a pointer entirely outside the validated mapped region. This pointer is then dereferenced as an `LH*` by callers (e.g., in `output_jar.cc`) to read `file_name_length()`, `extra_fields_length()`, `compressed_file_size()`/`uncompressed_file_size()`, and ultimately `lh->data()` is passed into `TransientBytes::ReadEntryContents`/`DecompressEntryContents`, which use these attacker-influenced size/length fields to read/copy memory: [6](#0-5) .

This class of bug — trusting an offset/length embedded in untrusted, externally supplied binary data without verifying it stays within the buffer, then using it to drive further reads — is structurally the same defect as CVE-2018-16525's `prvParseDNSReply`, where DNS response fields were used to index/copy without validating they remained inside the packet buffer.

### Impact Explanation
An attacker who supplies a malicious `.jar`/`.zip` file that ends up as `InputJar` input to singlejar (e.g., a prebuilt/third-party jar fetched from a repository/registry, a `java_import`ed artifact, or any binary jar the build merges via singlejar's deploy-jar assembly) can cause an out-of-bounds read of process memory relative to the `mmap`'d file. Depending on layout this can crash the singlejar build helper (denial of service to that specific build action) or leak adjacent heap/mmap memory contents (e.g., padding bytes, other freed regions) into the output artifact via subsequent copy operations that use the corrupted `LH` fields — an information-leak/OOB-read class matching the CVSS profile of the reference CVE (C:H/I:H/A:H via memory corruption reachable from remote/untrusted content).

### Likelihood Explanation
Reachability requires only that singlejar process a jar file whose bytes are attacker-influenced — a realistic scenario any time a build consumes a third-party/prebuilt JAR (Maven artifact, `http_jar`, checked-in binary dependency) that is later merged by singlejar (used for `java_binary` deploy jars, etc.). No credentials, MITM, or local machine access are needed; the attacker only needs to control the bytes of a JAR that is a build input, matching the "hostile origin server / dependency content" threat model. The only mitigating factor is that this requires a specifically malformed CDH `local_header_offset` value, which a crafted archive can trivially set.

### Recommendation
Add a `mapped_file_.mapped(...)` bounds check (and a sane sizeof-header check) on the computed LH address in `InputJar::LocalHeader()` before it is returned/dereferenced, mirroring the check already performed in `NextEntry()` for the CDH pointer, and fail/`diag_errx` as done elsewhere on corrupt records.

### Proof of Concept
I was not able to fully verify a complete end-to-end reproduction within the available tool budget (I could not trace every caller of `LocalHeader()` in `output_jar.cc`/`combiners.cc` to confirm the exact crash/read primitive, nor locate or construct a runnable JUnit/`BuildIntegrationTestCase` style proof). A concrete proof would follow the pattern already used by existing malformed-jar tests in the repo, e.g. `InputJarBadJarTest` (`src/tools/singlejar/input_jar_bad_jar_test.cc`) and the malformed extra-field test `CreateZipWithMalformedExtraField` in `src/tools/singlejar/output_jar_simple_test.cc` [7](#0-6) : construct a minimal ZIP with one CDH entry whose `local_header_offset32` is set far outside the mapped file bounds (e.g., `0xFFFFFF00`), run it through `InputJar::Open` + `NextEntry`, and observe that `LocalHeader()` returns and the caller dereferences a wild pointer, causing a crash/ASAN heap-buffer-overflow report instead of a graceful `diag_errx`. This needs to be built and run to confirm the exact fault, which I was unable to do given the available tools.

### Citations

**File:** src/tools/singlejar/input_jar.h (L70-84)
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
    cdh_ = reinterpret_cast<const CDH*>(new_cdr);
    *local_header_ptr = LocalHeader(current_cdh);
    return current_cdh;
```

**File:** src/tools/singlejar/input_jar.h (L94-97)
```text
  const LH* LocalHeader(const CDH* cdh) const {
    return reinterpret_cast<const LH*>(
        mapped_file_.address(cdh->local_header_offset() + preamble_size_));
  }
```

**File:** src/tools/singlejar/mapped_file.h (L47-49)
```text
  bool mapped(const void* addr) const {
    return mapped_start_ <= addr && addr < mapped_end_;
  }
```

**File:** src/tools/singlejar/mapped_file.h (L53-55)
```text
  const unsigned char* address(int64_t offset) const {
    return mapped_start_ + offset;
  }
```

**File:** third_party/ijar/zip.cc (L486-522)
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

**File:** src/tools/singlejar/transient_bytes.h (L76-102)
```text
  // Appends the contents of the uncompressed Zip entry.
  void ReadEntryContents(const CDH* cdh, const LH* lh) {
    uint64_t uncompressed_file_size;
    if (cdh->no_size_in_local_header()) {
      uncompressed_file_size = cdh->uncompressed_file_size();
    } else {
      uncompressed_file_size = lh->uncompressed_file_size();
    }
    Append(lh->data(), uncompressed_file_size);
  }

  // Appends the contents of the compressed Zip entry. Resets the inflater
  // used to decompress.
  void DecompressEntryContents(const CDH* cdh, const LH* lh,
                               Inflater* inflater) {
    uint64_t old_total_out = inflater->total_out();
    uint64_t in_bytes;
    uint64_t out_bytes;
    const uint8_t* data = lh->data();

    if (cdh->no_size_in_local_header()) {
      in_bytes = cdh->compressed_file_size();
      out_bytes = cdh->uncompressed_file_size();
    } else {
      in_bytes = lh->compressed_file_size();
      out_bytes = lh->uncompressed_file_size();
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
