### Title
Out-of-bounds read of a Local Header pointer derived from unvalidated `local_header_offset` in singlejar's `InputJar` - (File: `src/tools/singlejar/input_jar.h`)

### Summary
This is an analog of CVE-2019-7146 (elfutils buffer over-read in `ebl_object_note`): a length/offset field taken from an attacker-supplied binary container is trusted and used to compute a pointer/size that is dereferenced without validating it stays inside the mapped buffer. In `bazel--005`, `singlejar`'s `InputJar` computes the Local Header (`LH`) pointer for each Central Directory entry directly from the entry's `local_header_offset()` field, but — unlike `InputJar::NextEntry`, which validates that the *next CDH* pointer is `mapped_file_.mapped()` — it never validates that the computed `LH*` pointer (or the bytes subsequently read through it) lies within the memory-mapped input file.

### Finding Description
`InputJar::LocalHeader()` computes the local-header address purely from arithmetic on an attacker-controlled 32/64-bit offset field, with no bounds check: [1](#0-0) 

Compare this to `NextEntry()`, which does bound-check the *next CDH* pointer before dereferencing it: [2](#0-1) 

The `local_header_offset()` accessor itself is derived from a CDH field that, for Zip64 entries, is read straight from the (also-unvalidated) Zip64 extra field: [3](#0-2) 

The `LH*` returned by `LocalHeader()` is then dereferenced by callers to determine how many bytes to copy from the mapped input file into the output jar — without any preceding check that the pointer, or the header/`file_name_length`/`extra_fields_length`/`compressed_file_size` derived from it, are within `mapped_file_` bounds: [4](#0-3) [5](#0-4) 

`LH::size()`, `LH::extra_fields()`, and `LH::compressed_file_size()` all trust length fields read at the (potentially out-of-bounds) `LH*` address: [6](#0-5) 

Finally, the number of bytes copied from the input mapping (`num_bytes`) is derived from these unchecked fields and used directly to read from the mapped input file: [7](#0-6) 

Because `local_header_offset()` can be any 32-bit value (or, for Zip64 entries, an attacker-chosen 64-bit value from the Zip64 extra field), `preamble_size_ + local_header_offset()` can point anywhere — including outside `[mapped_start_, mapped_end_)`. Nothing in `LocalHeader()`, `output_jar.cc::AddJar`, or `transient_bytes.h::ReadEntryContents`/`DecompressEntryContents` (which also call `lh->data()`/`lh->compressed_file_size()`/`lh->uncompressed_file_size()`) calls `mapped_file_.mapped()` on the resulting pointer before dereferencing it, unlike the careful bounds check already performed for CDH traversal in `NextEntry()`.

### Impact Explanation
`singlejar` is Bazel's core jar-merging tool, invoked to build `deploy.jar`/merged jars from arbitrary input jars, including third-party dependency jars fetched from Maven repositories, `http_jar`/`http_archive` downloads, or other jars produced/supplied in a build graph, i.e. content that can originate from an untrusted or compromised remote host/mirror. A hostile jar with a corrupted/out-of-range `local_header_offset` (or a malicious Zip64 extra field) causes singlejar to compute a pointer outside the `mmap`'d region and then read attacker-influenced-length data from it. This can crash the build (denial of service) or — more importantly — cause bytes adjacent to the mapping (heap/other mmap'd data) to be copied verbatim into the merged output jar, i.e., unintended memory-content leakage into a build artifact.

### Likelihood Explanation
Any build that consumes third-party or non-hermetic jars through `singlejar` (java_binary, java_library merging, deploy jar creation) will parse the Central Directory and Local Header of every input jar this way; no special build configuration is needed to reach the vulnerable path — it is the default `AddJar` codepath. The only requirement is that the attacker control the bytes of a jar consumed by the build (achievable by serving a malicious jar from a compromised or MITM'd artifact source, subject to the usual checksum caveats for `http_archive`/`http_jar`, which do not apply once the jar is inside the source tree or fetched without a pinned checksum).

### Recommendation
Add bounds validation analogous to the existing `NextEntry()` check: after computing `LocalHeader(cdh)`, verify with `mapped_file_.mapped(lh)` (and additionally that `lh + lh->size()`, and `lh->data() + lh->in_zip_size()`, remain within `[mapped_start_, mapped_end_)`) before any field of `LH` is read or any bytes are copied. Reject the jar with a diagnostic error (as already done elsewhere in `LocateCentralDirectory`) rather than silently trusting offsets/lengths taken from the archive.

### Proof of Concept
A JUnit/`BuildIntegrationTestCase`-style native test (following the existing pattern in `src/tools/singlejar/input_jar_bad_jar_test.cc` / `input_jar_scan_entries_test.h`) can:
1. Build a minimal valid ZIP with one CDH entry, but patch its `local_header_offset` field to a large out-of-range value (e.g. `mapped_file_size + large_offset`, or set the Zip64 extra field's offset attribute to a huge 64-bit value) — analogous to how `test_extract_non_readable_file_ar` in `starlark_repository_test.sh` patches header bytes to craft adversarial archive content.
2. Call `InputJar::Open()` then `NextEntry(&lh)` and observe that `lh` is returned pointing outside `mapped_file_`'s bounds, then pass it through `OutputJar::AddJar`'s copy path (or `TransientBytes::ReadEntryContents`), which triggers an out-of-bounds read/copy (detectable via ASan `heap-buffer-overflow`/`SEGV` in a sanitized build), confirming the missing bounds check identified in `InputJar::LocalHeader()`.

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

**File:** src/tools/singlejar/zip_headers.h (L270-311)
```text
  uint16_t file_name_length() const { return le16toh(file_name_length_); }
  const char* file_name() const { return file_name_; }
  void file_name(const char* filename, uint16_t len) {
    file_name_length_ = htole16(len);
    if (len) {
      memcpy(file_name_, filename, len);
    }
  }
  bool file_name_is(const char* name) const {
    size_t name_len = strlen(name);
    return file_name_length() == name_len &&
           0 == strncmp(file_name(), name, name_len);
  }
  std::string file_name_string() const {
    return std::string(file_name(), file_name_length());
  }

  uint16_t extra_fields_length() const { return le16toh(extra_fields_length_); }
  const uint8_t* extra_fields() const {
    return ziph::byte_ptr(file_name_ + file_name_length());
  }
  uint8_t* extra_fields() {
    return reinterpret_cast<uint8_t*>(file_name_) + file_name_length();
  }
  void extra_fields(const uint8_t* data, uint16_t data_length) {
    extra_fields_length_ = htole16(data_length);
    if (data_length) {
      memcpy(extra_fields(), data, data_length);
    }
  }

  size_t size() const {
    return sizeof(LH) + file_name_length() + extra_fields_length();
  }
  const uint8_t* data() const { return extra_fields() + extra_fields_length(); }
  uint8_t* data() { return extra_fields() + extra_fields_length(); }

  size_t in_zip_size() const {
    return compression_method() ? compressed_file_size()
                                : uncompressed_file_size();
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

**File:** src/tools/singlejar/output_jar.cc (L444-447)
```text
  const CDH* jar_entry;
  const LH* lh;
  while ((jar_entry = input_jar.NextEntry(&lh))) {
    const char* file_name = jar_entry->file_name();
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
