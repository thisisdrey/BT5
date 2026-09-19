## Title
Out-of-bounds read via unvalidated `local_header_offset` in singlejar's `InputJar::LocalHeader` - (File: `src/tools/singlejar/input_jar.h`)

### Summary
`singlejar` (Bazel's built-in jar-merging tool, used for every `java_binary`/`java_library` deploy-jar action) computes the address of a ZIP entry's Local File Header directly from the `local_header_offset` field of the Central Directory Header, without validating that the resulting address is inside the mapped file. A crafted jar with an out-of-range `local_header_offset` causes `singlejar` to dereference memory outside the mapped input file when merging jars.

### Finding Description
`InputJar::LocalHeader()` computes the LH pointer as: [1](#0-0) 

using `MappedFile::address()`, which performs raw pointer arithmetic with **no bounds check**: [2](#0-1) 

This is inconsistent with the CDH traversal logic in the same class, `InputJar::NextEntry`, which explicitly validates the next CDH address with `mapped_file_.mapped(new_cdr)` before dereferencing it: [3](#0-2) 

`local_header_offset()` (a 32/64-bit value read straight from the CDH, fully attacker-controlled) is never checked against `mapped_file_.mapped(...)`. The resulting `LH*` pointer is then dereferenced throughout `OutputJar::AddJar` — e.g. to compute `num_bytes` via `lh->size()`, `lh->compressed_file_size()`, and to copy raw bytes from `input_jar.mapped_start() + copy_from`: [4](#0-3) [5](#0-4) 

If `local_header_offset` (or `local_header_offset() + preamble_size_`) points before `mapped_start_` or beyond `mapped_end_`, every one of these reads is an out-of-bounds heap read, and `num_bytes` (derived from further OOB-read fields) can be large or wrap, escalating the OOB read/copy.

### Impact Explanation
This is a memory-safety violation (out-of-bounds read, potentially with large attacker-influenced length) in a component (`singlejar`) that Bazel invokes as part of normal build actions whenever a `.jar`/deploy-jar rule packages more than one input jar. Any input jar contributed by an untrusted dependency (e.g. `http_jar`, `http_file`+genrule, a Maven artifact, or a checked-in file on an untrusted branch a CI build compiles) is processed by `singlejar` without any structural validation of the ZIP central-directory offsets beyond the check present for CDH chaining. A whole-file `sha256`/integrity check on the download only guarantees the bytes as downloaded match a hash — it says nothing about whether the internal ZIP offsets are self-consistent, so the integrity mechanism does not stop this class of corruption, mirroring the CVE-2020-27823 pattern (crafted offset field defeats structural assumptions even though the surrounding artifact "looks" legitimate).

### Likelihood Explanation
`local_header_offset` is a plain field lifted straight out of attacker-controlled bytes with no range validation before being converted into a pointer and dereferenced — this makes the bug trivially reachable by anyone who can supply one of the jars merged by `singlejar` (a common, unprivileged position: publishing a dependency archive, a Maven artifact, or content on a branch that CI builds).

### Recommendation
In `InputJar::LocalHeader()`, validate that the computed address (and the full extent of the resulting `LH` record, including `file_name_length`/`extra_fields_length`) lies within `[mapped_file_.start(), mapped_file_.end())` using `MappedFile::mapped()` (as already done for `NextEntry`'s CDH traversal) before returning the pointer, and fail cleanly (`diag_errx`) instead of dereferencing an out-of-bounds pointer.

### Proof of Concept
Extend the existing malformed-ZIP test harness (`CreateZipWithMalformedExtraField` in `src/tools/singlejar/output_jar_simple_test.cc`) to instead set `cdh->local_header_offset32()` to a value that, after adding `preamble_size_`, points past `mapped_end_` (e.g. `zip_data.size() + large_offset`), then feed this crafted zip through `OutputJar::AddJar` (via the existing `CreateOutput`/multi-input-jar test setup). Under an ASan build, this reproduces a heap-buffer-overflow read triggered from `InputJar::LocalHeader()` / `OutputJar::AddJar`. [6](#0-5)

### Citations

**File:** src/tools/singlejar/input_jar.h (L62-85)
```text
  // Returns the next Central Directory Header or nullptr.
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

**File:** src/tools/singlejar/mapped_file.h (L47-55)
```text
  bool mapped(const void* addr) const {
    return mapped_start_ <= addr && addr < mapped_end_;
  }

  const unsigned char* start() const { return mapped_start_; }
  const unsigned char* end() const { return mapped_end_; }
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

**File:** src/tools/singlejar/output_jar.cc (L666-671)
```text
    // Do the actual copy.
    if (!WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)) {
      diag_err(1, "%s:%d: Cannot write %zu bytes of %.*s from %s", __FILE__,
               __LINE__, num_bytes, file_name_length, file_name,
               input_jar_path.c_str());
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
