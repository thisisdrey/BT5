### Title
Out-of-bounds read via unchecked `CDH::local_header_offset()` in singlejar's `InputJar::LocalHeader` - (File: `src/tools/singlejar/input_jar.h`)

### Summary
`InputJar::LocalHeader()` computes a pointer into the memory-mapped input archive using an attacker-controlled 32-bit offset taken directly from the Central Directory Header, without validating that the resulting address lies within the mapped file bounds. This is directly analogous to the ksmbd `SMB2_TREE_CONNECT` bug: a length/offset field from untrusted, attacker-supplied structured data is trusted to compute a pointer that is later dereferenced, causing an out-of-bounds read.

### Finding Description
`InputJar::NextEntry` walks the Central Directory of a (potentially attacker-supplied) JAR/ZIP archive and, for each `CDH` entry, calls `LocalHeader(current_cdh)` to obtain a pointer to the corresponding Local Header: [1](#0-0) 

```
const LH* LocalHeader(const CDH* cdh) const {
    return reinterpret_cast<const LH*>(
        mapped_file_.address(cdh->local_header_offset() + preamble_size_));
}
``` [2](#0-1) 

`cdh->local_header_offset()` is a raw 32-bit field taken verbatim from the archive bytes with no range check, and `MappedFile::address()` simply performs pointer arithmetic (`mapped_start_ + offset`) with no bounds validation: [3](#0-2) 

This is in sharp contrast to `InputJar::LocateCentralDirectory`, which explicitly validates offsets read from the End-of-Central-Directory record using `mapped_file_.mapped(...)` before trusting them: [4](#0-3) 

No equivalent check exists for the per-entry `local_header_offset` used by `LocalHeader()`. The returned (potentially out-of-bounds) `LH*` pointer is then dereferenced by callers such as `OutputJar::WriteEntry`, which reads `entry->extra_fields()`, `entry->extra_fields_length()`, and `entry->file_name_length()` directly from the (possibly wild) local-header pointer to build the output Central Directory Header: [5](#0-4) 

The subsequent bounds check inside that loop (`ziph::byte_ptr(ef) + ef->size() > ziph::byte_ptr(lh_ef_end)`) only bounds-checks *within* the (already wild) extra-field region computed from the corrupted `LH*`; it does not validate that the `LH*` itself, or its `file_name_length()`/`extra_fields_length()` derived pointers, point inside the mapped archive.

### Impact Explanation
`local_header_offset` is fully attacker-controlled data inside an archive an unprivileged party can author and publish (e.g., a jar consumed via `http_jar`/`http_archive`, or any dependency jar singlejar reads when assembling a deploy jar). A crafted offset (very large, negative-after-add, or intentionally pointing near/at the end of the mapping) causes `LocalHeader()` to return a pointer outside the `mmap`'d region. Reading `LH` fields (`version_`, `bit_flag_`, `file_name_length_`, `extra_fields_length_`, etc.) or the derived `file_name()`/`extra_fields()` byte ranges from that pointer is an out-of-bounds read of process memory, which can crash the singlejar process (SIGSEGV/OOPS-equivalent) or leak adjacent memory content into fields subsequently written into the output jar's Central Directory Header.

### Likelihood Explanation
An sha256/integrity check on the whole archive (e.g., via `http_jar`) only guarantees the victim receives the exact bytes the attacker published — it does nothing to validate the internal structural correctness of the ZIP/JAR format. An attacker who controls the content behind a dependency URL can freely choose the `local_header_offset` value in any `CDH` entry; no code path revalidates it against the mapped file bounds before it's used to form a pointer that's later dereferenced. This makes the bug directly reachable from an untrusted publisher without any additional privilege.

### Recommendation
In `InputJar::LocalHeader` (and anywhere else `local_header_offset()` is consumed), validate the computed address the same way `LocateCentralDirectory` does for the ECD/CDH offsets — i.e., call `mapped_file_.mapped(...)` on the resulting `LH*` (and on `LH* + sizeof(LH)`) before returning it, and treat any out-of-range offset as a corrupt-archive error (`diag_errx`/graceful failure) rather than silently returning a wild pointer.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style reproduction:
1. Hand-craft a minimal ZIP/JAR whose Central Directory contains a single `CDH` entry with a valid signature/fields but `local_header_offset` set to a large out-of-range value (e.g., `0xFFFFFF00`, or a value that when added to `preamble_size_` lands past `mapped_file_.end()` or before `mapped_file_.start()`).
2. Feed this archive as an input to `singlejar` (e.g., as a `--sources` jar) or drive `InputJar::Open` + `NextEntry` directly in a unit test analogous to `src/tools/singlejar/input_jar_random_jars_test.cc`.
3. Observe that `LocalHeader()` returns a pointer outside `[mapped_file_.start(), mapped_file_.end())`, and that dereferencing it (as `OutputJar::WriteEntry` does for `extra_fields()`/`file_name_length()`) reads out-of-bounds memory, crashing the process or copying unrelated memory bytes into the output jar.

Note: I was unable to fully trace every direct caller that dereferences the `local_header_ptr` output of `NextEntry` beyond `OutputJar::WriteEntry` within the available index (some call sites like `input_jar_scan_entries_test.h` and `one_version_main.cc` reference `NextEntry`/`LocalHeader` but their full bodies were not retrievable through search); a background Devin session with full repo access would be needed to enumerate all consumers exhaustively and confirm the crash reproduces on a current release build.

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

**File:** src/tools/singlejar/output_jar.cc (L774-793)
```text
  auto lh_ef_begin = reinterpret_cast<const ExtraField*>(entry->extra_fields());
  auto lh_ef_end = reinterpret_cast<const ExtraField*>(
      ziph::byte_ptr(lh_ef_begin) + entry->extra_fields_length());
  ExtraField* cdh_extra_fields =
      reinterpret_cast<ExtraField*>(const_cast<uint8_t*>(cdh->extra_fields()));
  uint16_t out_ef_length = 0;
  for (const ExtraField* ef = lh_ef_begin; ef < lh_ef_end; ef = ef->next()) {
    if (ziph::byte_ptr(ef) + sizeof(ExtraField) > ziph::byte_ptr(lh_ef_end) ||
        ziph::byte_ptr(ef) + ef->size() > ziph::byte_ptr(lh_ef_end)) {
      diag_errx(1, "malformed extra field in LH for %.*s",
                (int)entry->file_name_length(), entry->file_name());
    }
    if (!ef->is_zip64()) {
      memcpy(cdh_extra_fields, ef, ef->size());
      cdh_extra_fields = reinterpret_cast<ExtraField*>(
          reinterpret_cast<uint8_t*>(cdh_extra_fields) + ef->size());
      out_ef_length += ef->size();
    }
  }
  cdh->extra_fields(cdh->extra_fields(), out_ef_length);
```
