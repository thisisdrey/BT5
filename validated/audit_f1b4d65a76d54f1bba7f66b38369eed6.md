### Title
Heap/mmap out-of-bounds read via unvalidated Zip local-header size fields in singlejar's `LocalHeader`/`ReadEntryContents` - (File: `src/tools/singlejar/input_jar.h`, `src/tools/singlejar/transient_bytes.h`)

### Summary
`InputJar::LocateCentralDirectory` validates the End-of-Central-Directory and Central-Directory-Header chain, but the per-entry `local_header_offset()` value stored in each `CDH` and the size fields stored in the corresponding `LH` (local file header) are never checked against the boundaries of the memory-mapped input jar before being dereferenced and used as a copy length, analogous to `SDL_FillRect`'s missing bounds check on a caller-controlled rectangle/size that produced CVE-2019-7637's heap overflow.

### Finding Description
`InputJar::LocalHeader()` computes the local header pointer purely by adding an attacker-controlled offset to the mapped base address, with no check that the resulting pointer (or the following fixed-size `LH` struct) is inside `[mapped_start_, mapped_end_)`: [1](#0-0) 

`MappedFile::address()` performs the same unchecked pointer arithmetic: [2](#0-1) 

Once an `LH*` is obtained, `TransientBytes::ReadEntryContents` and `TransientBytes::DecompressEntryContents` trust the 32-bit `uncompressed_file_size`/`compressed_file_size` fields read directly from that (potentially out-of-range) local header and pass them straight into `Append()`, which performs `memcpy`-style copies for that many bytes starting at `lh->data()`: [3](#0-2) 

`Concatenator::Merge`, used by `OutputJar::AddJar` when merging classpath/`+`-combined jar resources, calls straight into these unchecked paths for every entry produced by `InputJar::NextEntry`: [4](#0-3) 

`OutputJar::AddJar`'s non-merge copy path is equally unchecked: it computes `num_bytes` and `copy_from` purely from CDH/LH-declared sizes and hands them to `WriteBytes`/`memcpy` without ever confirming `copy_from + num_bytes` stays inside the mapped input file: [5](#0-4) [6](#0-5) 

The only bounds check present anywhere in this path is `NextEntry`'s check that the *next central directory record* is inside the mapped range — it never validates that `cdh->local_header_offset()` or the LH's own declared file/extra-field/data sizes point within the file: [7](#0-6) 

This is structurally the same bug class as `SDL_FillRect`: a size/offset value taken from untrusted input (here, a Zip Local Header inside an attacker-supplied `.jar`/`.zip` consumed by a build, e.g. via `http_jar`, `http_archive`, or any `java_import`-style rule feeding third-party jars into `singlejar`) is used directly to drive a byte copy without validating it against the actual bounds of the underlying buffer.

### Impact Explanation
An attacker who controls the bytes of a jar/zip archive fetched by a build (a Maven artifact, `http_jar`/`http_archive` target, or any file singlejar merges) can craft a `CDH.local_header_offset` pointing outside the mapped input file, or an `LH` whose `uncompressed_file_size`/`compressed_file_size`/`file_name_length`/`extra_fields_length` fields describe a region larger than the remaining mapped bytes. When `singlejar` processes this during `AddJar`/`Merge` it will read (and for merged/compressed streams, potentially inflate) memory adjacent to the mmap'd region — an out-of-bounds heap/mmap read that can crash the build tool (denial of the build) or, when the resulting bytes are copied into the output jar (e.g. via `TransientBytes::Append`/`CompressOut` and `WriteBytes`), leak adjacent process memory content into build artifacts.

### Likelihood Explanation
Any build that consumes a third-party `.jar`/`.zip` through `singlejar` (very common for Java rules — `java_import`, jar merging for deploy jars, resource jars, etc.) is reachable by this path merely by supplying a malformed archive; no privileged access, credentials, or MITM is required — this matches the "hostile origin server / malicious artifact" attacker model. The archive still has to pass basic CDH/ECD consistency checks in `LocateCentralDirectory`, but those checks bound only the central directory chain, not per-entry local-header offsets or size fields, so a mismatched-but-structurally-valid zip is sufficient.

### Recommendation
In `InputJar::LocalHeader`, validate that the computed `LH*` (and `sizeof(LH)` plus its declared `file_name_length()`/`extra_fields_length()`) lies fully within `mapped_file_.start()`/`mapped_file_.end()` before returning it, mirroring the existing check already done for the next `CDH` in `NextEntry`. In `TransientBytes::ReadEntryContents`/`DecompressEntryContents` and in `OutputJar::AddJar`'s raw-copy path, bound-check `uncompressed_file_size()`/`compressed_file_size()` (and the derived `copy_from`/`num_bytes` range) against the mapped file's actual remaining length before calling `Append`/`WriteBytes`, and reject the entry with a diagnostic error instead of reading past the mapping.

### Proof of Concept
Not executed — I could not run a build in this environment to confirm the overread reproduces in practice; this is based on static code review of `input_jar.h`, `mapped_file.h`, `transient_bytes.h`, `combiners.cc`, and `output_jar.cc`. A concrete reproduction would need a `src/tools/singlejar/*_test.cc`-style test (following the pattern of existing tests like `input_jar_random_jars_test.cc`/`output_jar_simple_test.cc`, e.g. `MalformedExtraField`) that crafts a jar with a `CDH.local_header_offset` pointing near `mapped_end_` and an `LH.uncompressed_file_size` exceeding the remaining mapped bytes, then invokes `OutputJar::AddJar`/`Concatenator::Merge` under ASan to observe the heap-buffer-overflow read. I was not able to verify with a live tool run whether existing tests (e.g. `input_jar_random_jars_test.cc`) already fuzz this specific offset/size mismatch case; this would need to be checked in a full Devin session with repo/tool access.

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

**File:** src/tools/singlejar/combiners.cc (L40-57)
```text
bool Concatenator::Merge(const CDH* cdh, const LH* lh) {
  if (insert_newlines_ && buffer_.get() && buffer_->data_size() &&
      '\n' != buffer_->last_byte()) {
    Append("\n", 1);
  }
  CreateBuffer();
  if (Z_NO_COMPRESSION == lh->compression_method()) {
    buffer_->ReadEntryContents(cdh, lh);
  } else if (Z_DEFLATED == lh->compression_method()) {
    if (!inflater_) {
      inflater_.reset(new Inflater());
    }
    buffer_->DecompressEntryContents(cdh, lh, inflater_.get());
  } else {
    diag_errx(2, "%s is neither stored nor deflated", filename_.c_str());
  }
  return true;
}
```

**File:** src/tools/singlejar/output_jar.cc (L597-609)
```text
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

**File:** src/tools/singlejar/output_jar.cc (L629-651)
```text
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
        size_t chunk2_size = lh->size() - (chunk1_size + removed_size);
        memcpy(lh_new, lh, chunk1_size);
        if (chunk2_size) {
          memcpy(reinterpret_cast<uint8_t*>(lh_new) + chunk1_size,
                 from_end - chunk2_size, chunk2_size);
        }
        lh_new->extra_fields(lh_new->extra_fields(),
                             lh->extra_fields_length() - removed_size);
      } else {
        memcpy(lh_new, lh, lh_size);
      }
```
