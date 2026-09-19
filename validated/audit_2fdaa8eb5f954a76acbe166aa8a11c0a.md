### Title
Unvalidated Local File Header size fields in `InputJar::NextEntry` allow out-of-bounds reads of the mapped input archive - (File: `src/tools/singlejar/input_jar.cc`, `src/tools/singlejar/transient_bytes.h`, `src/tools/singlejar/output_jar.cc`)

### Summary
`InputJar::NextEntry` (`src/tools/singlejar/input_jar.cc`) validates that the *Central Directory Header* record fits within the memory-mapped archive, but it never validates that the size fields declared in the corresponding *Local File Header* (`LH::compressed_file_size()` / `LH::uncompressed_file_size()`, both attacker-controlled 32/64-bit fields defined in `src/tools/singlejar/zip_headers.h`) actually fit within the bounds of the mapped input file. Callers such as `Concatenator::Merge`/`TransientBytes::ReadEntryContents`/`DecompressEntryContents` (`src/tools/singlejar/transient_bytes.h`) and `OutputJar::AddJar` (`src/tools/singlejar/output_jar.cc`) then use these unvalidated sizes directly to read `LH::data()` for `uncompressed_file_size` (or `compressed_file_size`) bytes, an out-of-bounds read of adjacent process memory when singlejar processes a maliciously crafted jar/zip.

### Finding Description
`InputJar::NextEntry` ( [1](#0-0) ) advances through Central Directory Headers and only checks `mapped_file_.mapped(new_cdr)` — i.e., that the *next* CDH record pointer lies within the mapped file. It resolves the corresponding Local Header via `LocalHeader()`: [2](#0-1) 

There is no check that `lh->local_header_offset()` is sane, nor that `lh->data() + lh->uncompressed_file_size()` (or `compressed_file_size()`) stays inside `mapped_file_.start()`/`mapped_file_.end()`. Both `LH::compressed_file_size()`/`uncompressed_file_size()` and `CDH::compressed_file_size()`/`uncompressed_file_size()` are read straight from the untrusted archive bytes with no bound relative to actual remaining mapped data: [3](#0-2) 

These unvalidated, attacker-controlled sizes are then used to copy data starting at `lh->data()`: [4](#0-3) [5](#0-4) 

and this code path is reachable from `OutputJar::AddJar`, which is invoked for every input jar processed by the `singlejar` tool (used by Bazel's Java/Android build actions to merge dependency jars, e.g. `META-INF/services/*` entries via `Concatenator::Merge`): [6](#0-5) [7](#0-6) 

`MappedFile::mapped()` only checks a pointer against `[mapped_start_, mapped_end_)`; it is never invoked to bound-check `lh->data() + size`: [8](#0-7) 

This is structurally analogous to CVE-2021-29646: a size/length field taken from untrusted input (`tipc_nl_retrieve_key`'s `TIPC_NLA_NODE_KEY_LEN` in the kernel; here `LH::uncompressed_file_size()`/`compressed_file_size()`) is used to drive a memory copy/read without validating it against the actual size of the buffer it indexes into.

### Impact Explanation
A jar/zip file that is an input to a `singlejar`-driven build action (e.g. a dependency archive fetched via `http_jar`/`http_archive`, or any build input under an untrusted branch) can declare a `LH` entry whose `uncompressed_file_size`/`compressed_file_size` vastly exceeds the number of bytes actually remaining in the mmap'd file. When `TransientBytes::ReadEntryContents`/`DecompressEntryContents` or the CRC/copy logic in `OutputJar::AddJar` process that entry, singlejar reads past the end of the memory-mapped file into adjacent process address space, and that out-of-bounds memory can end up copied into the output jar (an information disclosure of Bazel worker process memory) or cause a crash (SIGSEGV) of the singlejar action, i.e. of the build itself. This is a read primitive, not attacker-controlled write.

### Likelihood Explanation
Reaching this code path only requires an unprivileged attacker to control the bytes of a jar/zip that Bazel eventually feeds into a `singlejar`-based merge action — e.g. an archive served at a dependency URL. No special privileges, credential access, or execution of Starlark in the trusted root repo are required; the malformed size fields are ordinary bytes inside an otherwise well-formed-looking zip central directory/local header, so the attacker only needs to author a slightly corrupted archive.

### Recommendation
In `InputJar::NextEntry`/`LocalHeader` (or in `TransientBytes::ReadEntryContents`/`DecompressEntryContents` before use), validate that `lh->data() + declared_size <= mapped_file_.end()` (and `>= mapped_file_.start()`) for both `compressed_file_size()` and `uncompressed_file_size()`, aborting with a diagnostic (as is already done for CDH record bounds) instead of proceeding to copy/inflate the declared number of bytes.

### Proof of Concept
A reproducible proof requires a `BuildIntegrationTestCase`/`src/test/shell/bazel` style test that:
1. Crafts a minimal zip archive with a valid Central Directory Header/ECD but a Local File Header whose `uncompressed_file_size32` field is set to a large value (e.g. `0x7FFFFFFF`) while the actual file data section following the header is truncated to a few bytes (i.e., the file is much shorter than the declared size).
2. Feeds this archive as an input to `singlejar` (directly via the `singlejar` binary's `--sources`/merge flags, or through a `java_binary`/`android_binary` deploy-jar build whose dependency jar is this crafted archive).
3. Observes that singlejar crashes (SIGSEGV/ASan heap-buffer-overflow) or emits out-of-bounds bytes into the output jar, instead of failing with a clean "corrupt zip" diagnostic — confirming the missing bounds check identified above.

I was not able to fully trace every call site that consumes `LH::data()`/size (e.g. all combiner subclasses beyond `Concatenator`), so there may be additional or fewer reachable paths than described; a background agent with build/test execution access would be needed to actually build the PoC archive and run it through `singlejar` to confirm the crash/OOB read behavior.

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

**File:** src/tools/singlejar/zip_headers.h (L240-268)
```text
  size_t compressed_file_size() const {
    size_t size32 = compressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(1);
    }
    return size32;
  }
  size_t compressed_file_size32() const {
    return le32toh(compressed_file_size32_);
  }
  void compressed_file_size32(uint32_t v) {
    compressed_file_size32_ = htole32(v);
  }

  size_t uncompressed_file_size() const {
    size_t size32 = uncompressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(0);
    }
    return size32;
  }
  size_t uncompressed_file_size32() const {
    return le32toh(uncompressed_file_size32_);
  }
  void uncompressed_file_size32(uint32_t v) {
    uncompressed_file_size32_ = htole32(v);
  }
```

**File:** src/tools/singlejar/transient_bytes.h (L76-85)
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
```

**File:** src/tools/singlejar/transient_bytes.h (L87-102)
```text
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

**File:** src/tools/singlejar/output_jar.cc (L440-446)
```text
  InputJar input_jar;
  if (!input_jar.Open(input_jar_path)) {
    return false;
  }
  const CDH* jar_entry;
  const LH* lh;
  while ((jar_entry = input_jar.NextEntry(&lh))) {
```

**File:** src/tools/singlejar/output_jar.cc (L536-542)
```text
      // Handle special entries (the ones that have a combiner).
      if (entry_info.combiner_ != nullptr) {
        // TODO(kmb,asmundak): Should be checking Merge() return value but fails
        // for build-data.properties when merging deploy jars into deploy jars.
        entry_info.combiner_->Merge(jar_entry, lh);
        continue;
      }
```

**File:** src/tools/singlejar/mapped_file.h (L47-49)
```text
  bool mapped(const void* addr) const {
    return mapped_start_ <= addr && addr < mapped_end_;
  }
```
