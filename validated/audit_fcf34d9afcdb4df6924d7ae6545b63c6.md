### Title
Unbounded Local File Header pointer derived from attacker-controlled Central Directory offset causes out-of-bounds read - (File: src/tools/singlejar/input_jar.h)

### Summary
`InputJar::LocalHeader()` computes the address of a ZIP Local File Header (`LH`) directly from the `local_header_offset()` field stored in the (attacker-controlled) Central Directory Header (`CDH`), with no check that the resulting pointer lies inside the memory-mapped file. The `LH` is then dereferenced throughout `OutputJar::AddJar()` (e.g. `lh->size()`, `lh->extra_fields_length()`, `lh->compressed_file_size()`) to drive further pointer arithmetic and `memcpy` calls, giving a direct analog to CVE-2017-16535's pattern of trusting an attacker-supplied offset/length field without validating it against the actual buffer bounds.

### Finding Description
`InputJar::NextEntry()` validates that the *next CDH* lies inside the mapped file via `mapped_file_.mapped(new_cdr)` [1](#0-0) , but it then calls `LocalHeader(current_cdh)` to obtain the corresponding `LH*` with **no equivalent bounds check**:

```
const LH* LocalHeader(const CDH* cdh) const {
  return reinterpret_cast<const LH*>(
      mapped_file_.address(cdh->local_header_offset() + preamble_size_));
}
``` [2](#0-1) 

`CDH::local_header_offset()` is a 32-bit (or, via the Zip64 extra field, 64-bit) value taken verbatim from the central directory entry of the archive being processed [3](#0-2) . `MappedFile::address()` simply does pointer arithmetic (`mapped_start_ + offset`) with no range check [4](#0-3) , and `MappedFile::mapped()` — the only bounds-check primitive available — is never invoked for this pointer.

The resulting `lh` pointer, which can point far outside the mapped file (or even wrap/underflow the address space for large/negative offsets), is immediately dereferenced by the caller: `OutputJar::AddJar()` reads `lh->size()`, `lh->compressed_file_size()`, `lh->uncompressed_file_size()`, `lh->extra_fields_length()`, `lh->unix_time_extra_field()`, etc., and uses these to compute `num_bytes` and to `memcpy` header bytes for the output jar [5](#0-4) [6](#0-5) .

This is the structural analog of CVE-2017-16535: a length/offset field parsed from an untrusted, externally supplied binary structure is used to compute a pointer/read region without validating it against the actual buffer/descriptor bounds, leading to an out-of-bounds read (and potential crash or disclosure of adjacent process memory into the output jar).

### Impact Explanation
`singlejar`/`InputJar` is Bazel's native (C++) merge/repackage tool used when building Java targets, including processing of jars obtained from external dependencies (e.g. `http_jar`/`maven_install`-fetched artifacts). A crafted ZIP/JAR whose Central Directory Header declares a `local_header_offset` outside the file boundary causes the tool to dereference an out-of-bounds `LH*`, reading (and potentially copying into the output artifact or crashing the build) memory outside the mapped file. Because the check that exists (`mapped()`) is present for `CDH` traversal but omitted for the derived `LH` pointer, integrity of a correctly-hashed-but-maliciously-crafted archive does not protect against this: a checksum only guarantees byte-for-byte fidelity of the malicious file, not the safety of parsing it.

### Likelihood Explanation
Any attacker who can serve/publish a JAR/ZIP that a victim's build consumes (a malicious dependency, mirror, or artifact whose declared sha256 the attacker deliberately computes over their crafted file) can trigger this on every build that runs `singlejar` over that jar — which happens for ordinary Java build/link actions. No special privileges beyond serving content are required, and the corrupted-offset condition is straightforward to construct (a single out-of-range 32-bit `local_header_offset` field in a CDH).

### Recommendation
In `InputJar::LocalHeader()` (or immediately after computing it in `NextEntry()`), validate the derived `LH*` with `mapped_file_.mapped(...)` — verifying both the fixed-size `LH` header and the full `lh->size()` (once `file_name_length`/`extra_fields_length` are also read) remain within `[mapped_file_.start(), mapped_file_.end())` — and call `diag_errx`/return an error on failure, mirroring the existing check on `new_cdr`.

### Proof of Concept
Build a minimal ZIP where:
1. The Central Directory Header (`CDH`) is well-formed and passes the `NextEntry()` `mapped()` check for the CDH itself.
2. The `CDH.local_header_offset32` field is set to a value larger than the mapped file size (e.g. `0x7FFFFFF0`) or, using a Zip64 extra field, to a huge 64-bit offset.
3. Feed this file to `InputJar::Open()` + `NextEntry()` (as exercised by `OutputJar::AddJar`, e.g. through `output_jar_simple_test.cc`'s `CreateOutput`/`AddJar` test helpers, which already build synthetic malformed archives such as `CreateZipWithMalformedExtraField()` [7](#0-6) ).
4. Observe that `LocalHeader()` returns a pointer outside the mapped region and that subsequent `lh->size()`/`lh->extra_fields_length()` reads in `OutputJar::AddJar` dereference unmapped/out-of-range memory, producing a crash (SIGSEGV) under ASan or a JUnit/`BuildIntegrationTestCase`-style reproduction that asserts a controlled error is raised instead of a crash.

### Citations

**File:** src/tools/singlejar/input_jar.h (L63-85)
```text
  const CDH* NextEntry(const LH** local_header_ptr) {
    if (path_.empty()) {
      diag_errx(1, "%s:%d: call Open() first!", __FILE__, __LINE__);
    }
    if (!cdh_->is()) {
      return nullptr;
    }
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
  }
```

**File:** src/tools/singlejar/input_jar.h (L94-97)
```text
  const LH* LocalHeader(const CDH* cdh) const {
    return reinterpret_cast<const LH*>(
        mapped_file_.address(cdh->local_header_offset() + preamble_size_));
  }
```

**File:** src/tools/singlejar/zip_headers.h (L482-493)
```text
  uint64_t local_header_offset() const {
    uint32_t size32 = local_header_offset32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      int attr_no = ziph::zfield_has_ext64(uncompressed_file_size32());
      if (ziph::zfield_has_ext64(compressed_file_size32())) {
        ++attr_no;
      }
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(attr_no);
    }
    return size32;
  }
```

**File:** src/tools/singlejar/mapped_file.h (L53-55)
```text
  const unsigned char* address(int64_t offset) const {
    return mapped_start_ + offset;
  }
```

**File:** src/tools/singlejar/output_jar.cc (L593-609)
```text
    // Now we have to copy:
    //  local header
    //  file data
    //  data descriptor, if present.
    int64_t copy_from = jar_entry->local_header_offset();
    size_t num_bytes = lh->size();
    if (jar_entry->no_size_in_local_header()) {
      const DDR* ddr = reinterpret_cast<const DDR*>(
          lh->data() + jar_entry->compressed_file_size());
      num_bytes +=
          jar_entry->compressed_file_size() +
          ddr->size(
              ziph::zfield_has_ext64(jar_entry->compressed_file_size32()),
              ziph::zfield_has_ext64(jar_entry->uncompressed_file_size32()));
    } else {
      num_bytes += lh->compressed_file_size();
    }
```

**File:** src/tools/singlejar/output_jar.cc (L624-640)
```text
      lh_field_to_remove = lh->unix_time_extra_field();
      fix_timestamp = jar_entry->last_mod_file_date() != kDefaultDate ||
                      jar_entry->last_mod_file_time() != normalized_time ||
                      lh_field_to_remove != nullptr;
    }
    if (fix_timestamp) {
      uint8_t lh_buffer[512];
      size_t lh_size = lh->size();
      LH* lh_new = lh_size > sizeof(lh_buffer)
                       ? reinterpret_cast<LH*>(malloc(lh_size))
                       : reinterpret_cast<LH*>(lh_buffer);
      // Remove Unix timestamp field.
      if (lh_field_to_remove != nullptr) {
        auto from_end = ziph::byte_ptr(lh) + lh->size();
        size_t removed_size = lh_field_to_remove->size();
        size_t chunk1_size =
            ziph::byte_ptr(lh_field_to_remove) - ziph::byte_ptr(lh);
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
