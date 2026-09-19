### Title
Unbounded local-file-header offset from attacker-controlled JAR central directory computes out-of-bounds pointer, dereferenced without range check - ([File: src/tools/singlejar/input_jar.h])

### Summary
`singlejar`'s `InputJar::LocalHeader()` computes a pointer into a memory-mapped JAR from the `local_header_offset()` field of a Central Directory Header (CDH), which is fully attacker-controlled data read from a fetched/untrusted JAR (e.g. a `.jar` dependency pulled via `http_jar`/`http_archive`/`maven_install` and passed as a `srcs`/`deploy_jar` input). Unlike `InputJar::LocateCentralDirectory`, which explicitly range-checks the Central Directory offset/size against the mapped file (`mapped_file_.mapped(...)`, `mapped_file_.offset(ecd) < cen_position`, `cen_size > mapped_file_.offset(ecd)`), the `LocalHeader()` accessor performs **no equivalent bounds check** before the resulting pointer is cast to `const LH*` and later dereferenced by callers such as `OutputJar::AddJar`.

### Finding Description
`InputJar::LocalHeader()` is defined as: [1](#0-0) 

It calls `mapped_file_.address(offset)`, which does pure pointer arithmetic with no bounds validation: [2](#0-1) 

`cdh->local_header_offset()` is derived directly from the CDH's 32-bit field (or its Zip64 extra-field 64-bit override) in the attacker-supplied JAR — analogous to the kernel's `CHDBOFF`/`ERDBOFF` device-register values that were not range-checked before being used to compute an address (CVE-2023-53598). Here, `NextEntry()` invokes `LocalHeader(current_cdh)` and returns the resulting `LH*` to the caller without ever confirming the computed address lies inside `[mapped_start_, mapped_end_)`: [3](#0-2) 

The caller, `OutputJar::AddJar`, then dereferences this pointer (`lh->size()`, `lh->compressed_file_size()`, etc.) and later uses `jar_entry->local_header_offset()` again as a raw copy source (`copy_from`) without any prior validation: [4](#0-3) 

By contrast, `LocateCentralDirectory` — which computes the *initial* Central Directory location from the same kind of untrusted offset field — explicitly checks that the computed address is inside the mapped region before use: [5](#0-4) 

The asymmetry is the bug: the Central Directory pointer is validated, but the per-entry Local Header pointer (computed from the same untrusted-offset class of field) is not.

### Impact Explanation
A crafted JAR with a CDH whose `local_header_offset` (32-bit, or the Zip64 64-bit override) points far outside the mapped file causes `LocalHeader()` to synthesize an out-of-bounds pointer. Subsequent field reads (`lh->size()`, `lh->compressed_file_size()`, `lh->file_name_length()`, etc. in `output_jar.cc`) dereference attacker-influenced out-of-bounds memory, and the value is also used to seek/copy (`copy_from = jar_entry->local_header_offset()`), which can drive further out-of-bounds reads during the merge/copy step. On most platforms this produces a process crash (SIGSEGV) of `singlejar`/`ijar`, i.e. denial of service; depending on heap layout, out-of-bounds reads could also leak adjacent process memory into the produced output artifact. This is reachable purely by supplying a malformed JAR as a build input (e.g. a fetched `http_jar`/Maven artifact used in `deploy_jar`/`singlejar` merging), matching the "unprivileged, content-only" attacker model.

### Likelihood Explanation
Likelihood is high for triggering a crash: no sha256/integrity check inspects the internal Zip structure (checksums bind the whole file's bytes, not internal offset semantics), so any hostile origin server or malicious archive that passes the outer hash check can still contain a CDH with an out-of-range `local_header_offset`. `singlejar`/`ijar` run as part of ordinary Java build/link actions whenever external jars are merged, so this code path is on by default with no flag required.

### Recommendation
Add an explicit bounds check in `InputJar::LocalHeader()` (and equivalently wherever `local_header_offset()` is consumed, e.g. `output_jar.cc`) verifying that the computed address lies within `[mapped_file_.start(), mapped_file_.end())` and that there is enough remaining space for a full `LH` header plus its variable-length fields before dereferencing, mirroring the existing `mapped_file_.mapped(...)` checks already used in `LocateCentralDirectory`. On failure, fail closed (`diag_errx`) rather than returning a dangling/out-of-range pointer.

### Proof of Concept
A concrete PoC requires constructing a JAR whose End-of-Central-Directory and Central Directory are well-formed and pass `LocateCentralDirectory`'s checks, but whose single CDH entry's `local_header_offset32` (or Zip64 extra-field 64-bit offset) is set to a large out-of-range value (e.g. `0xFFFFFFF0` or a value larger than the mapped file size), then run it through `singlejar`'s `AddJar` path (as exercised by `src/tools/singlejar/output_jar_simple_test.cc`, which already builds similar hand-crafted malformed zips, e.g. `CreateZipWithMalformedExtraField()`): [6](#0-5) 

I was not able to fully verify from the index whether any later code (outside the snippets retrieved) re-validates `local_header_offset()` before use elsewhere in `output_jar.cc`/`transient_bytes.h`, since the index only exposes a limited window of these files. A background Devin session with full file access would be needed to confirm there is no other bounds check earlier in the call chain and to build/run the exact JUnit/shell-based PoC (e.g. extending `output_jar_simple_test.cc` or `third_party/ijar/test/zip_test.sh`, which already has a `test_no_path_traversal` pattern to follow) confirming a crash or OOB read.

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

**File:** src/tools/singlejar/mapped_file.h (L53-58)
```text
  const unsigned char* address(int64_t offset) const {
    return mapped_start_ + offset;
  }
  int64_t offset(const void* address) const {
    return reinterpret_cast<const unsigned char*>(address) - mapped_start_;
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

**File:** src/tools/singlejar/input_jar.cc (L94-103)
```text
  // First, consistency check the directory.
  uint32_t cen_position = ecd->cen_offset32();
  if (!ziph::zfield_has_ext64(cen_position)) {
    if (!mapped_file_.mapped(mapped_file_.address(cen_position))) {
      diag_warnx("%s:%d: %s is corrupt: Central Directory location 0x%" PRIx32
                 " is invalid",
                 __FILE__, __LINE__, path.c_str(), cen_position);
      mapped_file_.Close();
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
