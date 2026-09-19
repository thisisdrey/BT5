### Title
Out-of-bounds read via unvalidated `local_header_offset` in singlejar's `InputJar::LocalHeader` - (File: `src/tools/singlejar/input_jar.h`)

### Summary
`InputJar::LocalHeader()` computes the address of a ZIP entry's Local Header by adding an attacker-controlled 32/64-bit offset (`CDH::local_header_offset()`) taken directly from a Central Directory Header to `mapped_start_`, via `MappedFile::address()`, which performs raw pointer arithmetic with **no bounds check**. Unlike the Central Directory location itself (validated in `InputJar::LocateCentralDirectory`), the per-entry local header offset is never checked against the mapped file's extent before being dereferenced.

### Finding Description
`MappedFile::address(int64_t offset)` is defined as: [1](#0-0) 
with no comparison against `mapped_end_`, unlike the sibling `mapped()` predicate which does bound-check. [2](#0-1) 

`InputJar::LocalHeader()` uses this unchecked helper directly on the CDH's declared local header offset: [3](#0-2) 

Contrast this with `LocateCentralDirectory()`, which explicitly validates `cen_position`/`cen_size` against the mapped extent before trusting them: [4](#0-3) 

No equivalent check exists for `cdh->local_header_offset()` (a 32-bit field, or a 64-bit value taken from the entry's Zip64 extra field via `attr64()`) before it is used to compute the `LH*` pointer inside `NextEntry()`: [5](#0-4) 

The resulting `LH*` (`lh`) is then dereferenced by `OutputJar::AddJar` without any prior sanity check, e.g. to compute `num_bytes = lh->size()` and, separately, `copy_from = jar_entry->local_header_offset()` which is used to copy raw bytes straight out of the mapped input file into the output jar: [6](#0-5) [7](#0-6) 

Since `local_header_offset()` is fully attacker-controlled (up to `0xFFFFFFFF`, or an arbitrary 64-bit value via the Zip64 extra field), an attacker who supplies a crafted `.jar`/`.zip` file that singlejar merges (e.g. a prebuilt jar checked into an untrusted PR branch that CI builds, or supplied as a `java_import`/`filegroup` input) can force `LocalHeader()` to return a pointer far outside the mapped region. Subsequent field accesses (`lh->size()`, `lh->compressed_file_size()`, `lh->file_name_length()`, `lh->extra_fields()`) and the raw byte copy at `output_jar.cc:667` then read out-of-bounds memory (unmapped pages causing a crash, or adjacent heap/mapped memory whose bytes get written verbatim into the merged output jar — a build-artifact memory-disclosure primitive).

### Impact Explanation
An unprivileged party who can get a malicious `.jar` file included as an input to a `singlejar` action (deploy-jar assembly for `java_binary`/`java_library`, or any rule invoking `singlejar`/`ijar`'s underlying tooling) can crash the build tool (denial of the build) or, in the memory-disclosure variant, cause bytes from the singlejar process's own address space to be spliced into the merged output jar and subsequently shipped/consumed — an out-of-bounds read whose effects escape into build outputs. This matches the requested class of "content parsed from an untrusted container header is trusted to compute a buffer offset/size without bounds validation," analogous to the referenced FFmpeg `track_header` bug, here manifesting as OOB read/dereference rather than a write, because the singlejar output-writing path validated buffer sizes are computed correctly on the output side; it is the source-side pointer that is unchecked.

### Likelihood Explanation
Reaching this requires only that singlejar (or a wrapper such as `ijar`'s `AddJar`/`java_binary` deploy-jar assembly) processes an attacker-influenced `.jar`/`.zip` file as one of its inputs — no MITM, no credential access, and no bypass of a declared checksum is required, since the attacker can supply the file directly (e.g., a checked-in prebuilt `.jar` on an untrusted branch that CI builds, matching the stated attacker model). The bug triggers on the very first malformed CDH entry processed by `NextEntry`/`LocalHeader`.

### Recommendation
Validate `cdh->local_header_offset() + preamble_size_` (and the resulting `LH::size()` extent) against `mapped_file_.mapped()`/`mapped_file_.end()` before dereferencing, mirroring the existing bounds checks already applied to `cen_position`/`cen_size` in `LocateCentralDirectory` and to the "next CDH" pointer in `NextEntry`. Reject the jar (as is already done for corrupt ECD/CDH) rather than returning a raw, unchecked pointer from `LocalHeader()`.

### Proof of Concept
A `src/test/shell/bazel`/`BuildIntegrationTestCase`-style reproduction:
1. Construct a minimal valid ZIP with one entry whose Central Directory Header is patched so `local_header_offset32` is set to a large out-of-range value (e.g. `0xFFFFFFF0`), while the End-of-Central-Directory record and Central Directory location itself remain internally consistent so `LocateCentralDirectory` accepts the file.
2. Feed this crafted jar as an input to singlejar (e.g. via `singlejar --sources crafted.jar --output out.jar`, or as a source in a `java_binary`'s deploy-jar assembly).
3. Observe that `InputJar::NextEntry`/`LocalHeader` computes an out-of-bounds `LH*`, and that `OutputJar::AddJar`'s subsequent field reads (`lh->size()`) and the `WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)` call crash (SIGSEGV under ASan/valgrind) or copy unrelated process memory into `out.jar`.

**Note:** I was not able to fully trace every internal call site of `singlejar`/`ijar` invocation from the Java rules (e.g., exact `java_binary` action wiring) within the indexed portion of the codebase, and did not find an existing unit test exercising this specific offset-validation gap; a background Devin session with full repository/build access would be needed to construct and run the concrete PoC/JUnit test described above.

### Citations

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

**File:** src/tools/singlejar/input_jar.cc (L94-122)
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
    if (mapped_file_.offset(ecd) < cen_position) {
      diag_warnx("%s:%d: %s is corrupt: End of Central Directory at 0x%" PRIx64
                 " precedes Central Directory at 0x%" PRIx32,
                 __FILE__, __LINE__, path.c_str(), mapped_file_.offset(ecd),
                 cen_position);
      mapped_file_.Close();
      return false;
    }
  }
  uint32_t cen_size = ecd->cen_size32();
  if (!ziph::zfield_has_ext64(cen_size)) {
    if (cen_size > mapped_file_.offset(ecd)) {
      diag_warnx("%s:%d: %s is corrupt: Central Directory size 0x%" PRIx32
                 " is too large",
                 __FILE__, __LINE__, path.c_str(), cen_size);
      mapped_file_.Close();
      return false;
    }
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
