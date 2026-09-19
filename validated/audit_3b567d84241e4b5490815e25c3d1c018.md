### Title
Unbounded local-header offset in `InputJar::LocalHeader` allows out-of-bounds read that is copied verbatim into the output jar - ([File: src/tools/singlejar/input_jar.h])

### Summary
`singlejar` (`OutputJar::AddJar`) merges third‑party jar/zip inputs (e.g. dependency jars fetched from external repositories/registries) by walking each input jar's Central Directory and following each `CDH::local_header_offset()` to the corresponding Local Header via `InputJar::LocalHeader()`. That offset is fully attacker-controlled data taken directly from the archive's Central Directory Header, and it is never validated against the mapped file's bounds before it is dereferenced and used to size a `memcpy`/write.

### Finding Description
`InputJar::LocalHeader()` computes the address of the Local Header purely by adding the untrusted, attacker-supplied `local_header_offset()` (plus `preamble_size_`) to the mapped file's start pointer, with **no bounds check**: [1](#0-0) 

Compare this to `NextEntry()`, which does validate `new_cdr` against the mapped file range via `mapped_file_.mapped(new_cdr)`: [2](#0-1) 

`MappedFile::address()` performs no clamping either — it just adds the offset to the mapped base pointer: [3](#0-2) 

The pointer returned by `LocalHeader()` is immediately dereferenced in `OutputJar::AddJar` to compute the number of bytes to copy from the (potentially out-of-bounds) location: [4](#0-3) 

and that byte count (`num_bytes`, ultimately derived from unvalidated `lh->size()`/`lh->compressed_file_size()`/`lh->uncompressed_file_size()`) is used directly to copy bytes starting at `input_jar.mapped_start() + copy_from` into the output artifact: [5](#0-4) 

This mirrors the kernel bug class exactly: a size/offset value taken from attacker-controlled metadata is used, without bounds validation, to copy a region of memory whose contents were never validated to belong to the intended buffer — resulting in a slab/heap-adjacent out-of-bounds read whose bytes are then exposed to a "consumer" (there: `recvmsg()` caller; here: the merged output `.jar` written to disk / distributed as a build artifact).

Note: elsewhere in the same file, extra-field parsing (`ExtraField::find`, the loops in `WriteEntry`/`AppendToDirectoryBuffer`) *does* perform explicit bounds checks and calls `diag_errx`/aborts on malformed data (see the `MalformedExtraField` death test), showing the authors are aware of this bug class and have hardened most of the parser — but the `local_header_offset()` → `LocalHeader()` path was missed.

### Impact Explanation
`local_header_offset()` is a plain `uint32`/64-bit field fully controlled by whoever produced the input jar. An attacker who can get a project to consume a malicious `.jar`/`.zip` as a `singlejar` input (e.g., a dependency archive from an external registry/mirror, matching a pinned checksum that the victim's build rule accepted) can set this offset to point anywhere within — or beyond — the mapped file. Depending on the resulting pointer:
- If it lands past the end of the mapping, the process reads unmapped memory and typically crashes (denial of service on that build action) — out of scope per the rules for pure DoS.
- If it lands within the mapping but on an unrelated region of the file (or, since the mapping can be adjacent to other heap/mmap'd memory depending on `Open(path, data, length)` usage), the attacker can force `singlejar` to interpret arbitrary bytes as a fabricated `LH` header, causing `num_bytes` to be computed from attacker-influenced adjacent memory and copied into the output `.jar`. This is a concrete out-of-bounds **read** that leaks bytes outside the intended local-header region into a build artifact that is written to disk / potentially shared via caches — an information-disclosure/memory-safety violation of the "the archive's own byte length fields must be trusted only within the archive's own bounds" invariant.

### Likelihood Explanation
`singlejar` is a core, widely used Bazel build tool invoked for essentially every Java `_deploy.jar`/merged jar build action, and it processes every jar listed via `--sources`, including jars originating from external dependencies. No special privileges are required beyond supplying (or poisoning) one such jar; the offset field is a simple integer in the Central Directory Header that requires no cryptographic bypass to control — only that the archive itself be accepted by the build (which checksum/lockfile mechanisms verify only the archive's aggregate hash, not the internal consistency of its zip metadata). This makes the trigger straightforward once a malicious jar is on the dependency path.

### Recommendation
In `InputJar::LocalHeader()`, validate that the computed address (and the full extent implied by the subsequent `LH` fields: `file_name_length()`, `extra_fields_length()`, `compressed_file_size()`) lies within `[mapped_file_.start(), mapped_file_.end())` before returning/dereferencing it, mirroring the existing `mapped_file_.mapped(new_cdr)` check used in `NextEntry()`. Abort with `diag_errx` (as already done for other malformed-metadata cases) if the local header offset is out of range.

### Proof of Concept
Construct a jar containing:
1. A minimal valid Local Header for filler content.
2. A Central Directory Header (`CDH`) whose `local_header_offset32` field is set to a value at or slightly beyond the file's total size (or wraps to point into the End-of-Central-Directory area) rather than a valid local header offset — following the same pattern already used by `CreateZipWithMalformedExtraField()` in `output_jar_simple_test.cc`.
3. A valid `ECD` describing this single entry.

Run `singlejar --sources bad.jar --output out.jar`. `OutputJar::AddJar` calls `input_jar.NextEntry(&lh)`, which internally calls `LocalHeader(current_cdh)` with the malicious offset; the resulting `lh` pointer is dereferenced at `output_jar.cc:598` (`lh->size()`) without any bounds validation, leading to an out-of-bounds read (crash under ASan/valgrind, or silent leakage of adjacent memory bytes into `out.jar` if the read lands on mapped-but-unrelated memory) — analogous to the existing `ASSERT_DEATH(output_jar.Doit(), "malformed extra field")` test pattern, except no such guard exists for this code path.

### Citations

**File:** src/tools/singlejar/input_jar.h (L70-82)
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

**File:** src/tools/singlejar/output_jar.cc (L666-671)
```text
    // Do the actual copy.
    if (!WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)) {
      diag_err(1, "%s:%d: Cannot write %zu bytes of %.*s from %s", __FILE__,
               __LINE__, num_bytes, file_name_length, file_name,
               input_jar_path.c_str());
    }
```
