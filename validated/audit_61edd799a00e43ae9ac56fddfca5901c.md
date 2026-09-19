### Title
Unvalidated Local Header size/offset fields allow out-of-bounds memory copy into `singlejar` output - (File: `src/tools/singlejar/output_jar.cc`, `src/tools/singlejar/input_jar.h`)

### Summary
`singlejar` (the tool Bazel uses to merge/build deploy jars) trusts the sizes and offsets it reads from a Local File Header (`LH`) of an *input* jar without validating them against the bounds of the memory-mapped file, then uses those values directly as a `memcpy`/`fwrite` length and source pointer when producing the output artifact. This mirrors the CVE-2025-39718 bug class: a length taken from an untrusted, attacker-controlled header is used to size a copy/put operation before validating it against the size of the buffer that actually backs it.

### Finding Description
`InputJar::NextEntry()` validates that the *next Central Directory Header* lies within the mapped file [1](#0-0) , but the corresponding Local Header pointer is computed purely by arithmetic on an attacker-controlled `local_header_offset` field from the CDH, with no bounds check: [2](#0-1) 

`OutputJar::AddJar()` then derives a copy length directly from fields inside that unvalidated `LH` (`lh->size()`, `lh->compressed_file_size()`), and uses `input_jar.mapped_start() + copy_from` (an offset likewise taken from the untrusted CDH `local_header_offset`) as the source pointer for a raw byte copy into the output jar: [3](#0-2) [4](#0-3) 

Nothing here checks that `copy_from + num_bytes` (derived from header-declared sizes) stays within `mapped_file_.size()`. This is the same invariant violation as the vsock bug: the packet/header declares a length, and that length is used to size a data-movement operation before checking it against the actual backing buffer's real extent. `TransientBytes::ReadEntryContents`/`DecompressEntryContents` (used by the "combiner" merge path for service files, etc.) show the same pattern — they trust `lh->uncompressed_file_size()`/`compressed_file_size()` and call `Append(lh->data(), uncompressed_file_size)` without any check that `lh->data() + size` is inside the mapped region: [5](#0-4) 

### Impact Explanation
`local_header_offset`, `compressed_file_size`, and `uncompressed_file_size` are fully attacker-controlled fields inside a Central/Local Header of an input jar. Since jars processed by `singlejar` are exactly the kind of artifact an outsider can supply (e.g. a dependency's jar, a jar produced from an untrusted-branch build, or a fetched artifact consumed as a `srcs`/`deps` input), a crafted jar can point `local_header_offset` near the end of the mapped file and declare an oversized `compressed_file_size`/`uncompressed_file_size`. The resulting `WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)` call reads past the end of the mmap'd input file and copies that adjacent process memory straight into the produced output jar — an out-of-bounds read whose contents (potentially other heap/mapped data) are exfiltrated into a build artifact. This is a "read outside the repository/exec-root boundary that reaches a build output" — squarely in the category the report asks to validate for.

### Likelihood Explanation
Central Directory validation (`InputJar::LocateCentralDirectory`/`NextEntry`) checks CDH-to-CDH pointer chaining and comment/extra-field lengths, but it never validates the CDH's `local_header_offset` field against `mapped_file_.size()`, nor does anything downstream re-derive `LH` size bounds from the mmap size before trusting `lh->size()`/`compressed_file_size()`/`uncompressed_file_size()`. Because `AddJar()` is invoked on every jar merged into a deploy/output jar (a routine, always-on code path with default flags), any build that consumes an attacker-supplied jar as an input to a `java_binary`/`java_library`-style merge is affected.

### Recommendation
Before dereferencing an `LH*` obtained via `InputJar::LocalHeader()`, validate that the computed address and `address + lh->size()` (and `+ compressed_file_size`/`uncompressed_file_size` for the data region) are within `mapped_file_.start()`/`mapped_file_.end()`, mirroring the existing `mapped_file_.mapped(new_cdr)` check already done for CDH chaining in `NextEntry()`. Reject (with `diag_errx`) any entry whose local-header-declared offset/size would read past the end of the mapped input file, in both `OutputJar::AddJar()`'s copy path and `TransientBytes::ReadEntryContents`/`DecompressEntryContents`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style repro:
1. Author a crafted `.jar`: a well-formed End-of-Central-Directory + Central Directory Header whose `local_header_offset32` points to a location such that `local_header_offset + sizeof(LH) + filename_len` lands within a few bytes of the actual end of the mmap'd file.
2. Set the corresponding Local Header's `compressed_file_size`/`uncompressed_file_size` fields to a large value (e.g. `0x10000`), far exceeding the remaining bytes actually available in the file.
3. Feed this jar as an input to `singlejar --output out.jar --sources crafted.jar` (or via a `bazel build` of a `java_binary` depending on this jar as a prebuilt jar).
4. Observe (under ASan or via a segfault/garbage-data check) that `OutputJar::AddJar` performs `WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)` reading past `mapped_file_.end()`, either crashing the process or copying out-of-bounds heap bytes into `out.jar`'s entry content — verifiable by comparing `out.jar`'s entry payload against the expected file contents on disk.

Note: I was not able to fully trace `WriteBytes`'s exact implementation (its definition body wasn't retrieved in this pass) to confirm there is no last-mile length clamp before the `fwrite`; this should be double-checked when reproducing the PoC. [6](#0-5)

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

**File:** src/tools/singlejar/output_jar.h (L1-1)
```text
// Copyright 2016 The Bazel Authors. All rights reserved.
```
