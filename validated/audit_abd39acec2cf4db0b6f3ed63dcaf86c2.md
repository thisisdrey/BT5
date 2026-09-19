### Title
Heap out-of-bounds read on attacker-crafted JAR/ZIP local header offset in singlejar's `InputJar::LocalHeader` - (File: `src/tools/singlejar/input_jar.h`)

### Summary
`singlejar` (used by `java_binary`/`java_library`/`deploy_jar` actions) parses ZIP/JAR files whose bytes are controlled by whoever publishes the JAR that a `java_import`/`http_jar`/`http_archive` dependency points to. The Central Directory Header (`CDH`) is validated to lie within the mapped file, but the `local_header_offset()` field taken from that same attacker-controlled `CDH` is used to compute a `LH*` pointer with **no bounds check**, and that pointer is subsequently dereferenced for length fields that drive further reads/copies.

### Finding Description
`InputJar::NextEntry` validates that each `CDH` record lies within the mapped file before it is dereferenced: [1](#0-0) 

However, the companion `LocalHeader()` accessor computes the address of the local header purely from the attacker-supplied 32-bit `local_header_offset()` field of the CDH plus `preamble_size_`, and hands back a raw pointer with no membership check against the mapped region: [2](#0-1) 

`MappedFile::address()` performs plain pointer arithmetic and cannot detect an out-of-range offset: [3](#0-2) 

The unchecked `LH*` returned by `NextEntry` is then dereferenced directly in `OutputJar::AddJar`, e.g. `lh->size()`, `jar_entry->local_header_offset()`, and `lh->compressed_file_size()`, to compute a byte range that is subsequently memcpy'd/read from the mapped file: [4](#0-3) 

`LH::size()`, `LH::extra_fields()`, and `LH::data()` themselves read `file_name_length_` / `extra_fields_length_` fields at whatever address the (unchecked) offset lands on, and use them to compute further offsets that are used for the subsequent copy — none of this chain confirms the header or its length-derived tail lies within the mmap'd input file: [5](#0-4) 

This is structurally analogous to the referenced radare2 CVE-2018-12321: a length/offset field taken from parsed, attacker-crafted binary data is used to index into a buffer without validating that the resulting pointer (and the subsequent read length derived from further attacker-controlled fields) stays inside the mapped/allocated region, producing a heap out-of-bounds read.

### Impact Explanation
A crafted JAR (e.g., served from a `http_jar`/`http_archive`/`java_import` dependency URL, or committed by an untrusted PR branch that a CI build compiles) with a `CDH.local_header_offset` pointing outside the mapped file (or near its end, combined with large `file_name_length`/`extra_fields_length`/`compressed_file_size` values) causes `singlejar` to read heap memory outside the `mmap`'d input file region while merging/packaging the output jar. This can crash the build tool (denial of the specific build step) and, because bytes read out-of-bounds are copied verbatim into local-header fields, potentially leak adjacent process heap memory bytes into the produced output jar. The overall SHA-256/integrity check on the *downloaded archive* does not protect against this, since it only confirms the archive matches what was recorded — the attacker fully controls the archive contents (and can supply the matching checksum) from the origin server.

### Likelihood Explanation
Reaching this code path only requires a build that consumes an externally-supplied JAR (a very common pattern via `http_jar`, `http_archive` + prebuilt jars, or `java_import` pointing at a fetched artifact) and runs it through `singlejar` (any `java_binary`/`deploy_jar`-producing target, and merge/combiner logic already iterates every entry of every input jar). No local access, credentials, or privileged position are required — only that the victim's build fetches and packages a jar from a hostile origin.

### Recommendation
In `InputJar::LocalHeader` (and/or its caller `NextEntry`), validate that the computed `LH*` pointer, and the full local-header record including `file_name_length()`/`extra_fields_length()`-dependent tail, remain within `mapped_file_.start()`/`mapped_file_.end()` before returning/dereferencing it — mirroring the `mapped_file_.mapped(new_cdr)` check already done for CDH records. Reject the jar with a diagnostic (as `NextEntry` already does for corrupt CDH chains) rather than returning an unchecked pointer.

### Proof of Concept
A JUnit/`BuildIntegrationTestCase`-style construction, analogous to the existing `OutputJarSimpleTest.MalformedExtraField` death test, but for the local header offset: [6](#0-5) 
1. Build a minimal ZIP whose sole `CDH` entry has a valid signature/lengths but `local_header_offset` pointing past the end of the mapped file (or to `mapped_end_ - 4` combined with a large `file_name_length`).
2. Feed this crafted jar as a `--sources` input to `singlejar` (`OutputJar::Doit`), as done in `OutputJarSimpleTest`.
3. Observe a heap-buffer-overflow/out-of-bounds read reported by ASan (or a segfault) inside `LH::size()`/`LH::file_name_length()` reached via `OutputJar::AddJar` → `InputJar::NextEntry`/`LocalHeader`, confirming the missing bounds check identified above.

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

**File:** src/tools/singlejar/zip_headers.h (L301-320)
```text
  size_t size() const {
    return sizeof(LH) + file_name_length() + extra_fields_length();
  }
  const uint8_t* data() const { return extra_fields() + extra_fields_length(); }
  uint8_t* data() { return extra_fields() + extra_fields_length(); }

  size_t in_zip_size() const {
    return compression_method() ? compressed_file_size()
                                : uncompressed_file_size();
  }

  const Zip64ExtraField* zip64_extra_field() const {
    return Zip64ExtraField::find(extra_fields(),
                                 extra_fields() + extra_fields_length());
  }

  const UnixTimeExtraField* unix_time_extra_field() const {
    return UnixTimeExtraField::find(extra_fields(),
                                    extra_fields() + extra_fields_length());
  }
```

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1227-1236)
```text
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
