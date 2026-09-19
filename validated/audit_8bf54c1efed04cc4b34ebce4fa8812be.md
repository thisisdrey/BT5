## Title
Unvalidated zip entry `uncompressed_file_size`/`compressed_file_size` in `TransientBytes::ReadEntryContents`/`DecompressEntryContents` allows out-of-bounds read from crafted JAR merged by singlejar - (File: src/tools/singlejar/transient_bytes.h)

## Summary
`singlejar` reads Local Header (`LH`) and Central Directory Header (`CDH`) size fields directly out of a memory-mapped input JAR and trusts them without validating that the declared `uncompressed_file_size`/`compressed_file_size` actually fit within the remaining bytes of the mapped file, mirroring the "unvalidated rdlength" class in the reported zeroconf advisory.

## Finding Description
`InputJar::NextEntry` validates only that the *next Central Directory Header* itself lies within the mapped region via `mapped_file_.mapped(new_cdr)` [1](#0-0) . It never validates that the corresponding local file entry's declared payload length is contained within the mapped file. The size values themselves come straight off attacker-controlled header bytes: `LH::uncompressed_file_size()`/`compressed_file_size()` read little-endian 32/64-bit fields (with Zip64 extension) directly from the mapped buffer [2](#0-1) .

Those unchecked sizes then drive raw memory copies in `TransientBytes`:
- `ReadEntryContents` calls `Append(lh->data(), uncompressed_file_size)` for STORED entries, directly copying `uncompressed_file_size` bytes starting at `lh->data()` with no bound check against the mapped file's actual length [3](#0-2) .
- `DecompressEntryContents` feeds `in_bytes = lh->compressed_file_size()` bytes starting at `lh->data()` directly into zlib's `inflater->DataToInflate(data, in_bytes_chunk)` with no check that this many bytes exist in the mapped input [4](#0-3) .

By contrast, `third_party/ijar/zip.cc`'s `InputZipFile` performs an explicit `EnsureRemaining(compressed_size_, "file_data")` bound check before reading entry payload bytes [5](#0-4) [6](#0-5) , showing the codebase is aware of this exact invariant elsewhere but does not apply it in `singlejar`'s `TransientBytes`/`InputJar` path.

`singlejar` is the tool that merges/dedups Java archive entries (used for combining `java_library`/`java_binary` outputs, including third-party jars fetched via `http_jar`/`maven_install`/external repositories). The sha256/integrity check on `http_archive`/`http_jar` only verifies the outer downloaded bytes are unmodified from what was originally served; it does nothing to validate internal consistency of a maliciously-crafted ZIP's own length fields inside that (correctly-hashed) blob. A malicious dependency author can pin a `sha256` for a JAR whose Local Header declares a payload size (e.g. via Zip64 extension) far larger than the number of bytes actually present before EOF/next header. Because there is no bounds check equivalent to `EnsureRemaining`, `lh->data()` combined with the oversized declared length reads past the end of the `mmap`'d input file region during merge into `TransientBytes`, propagating truncated/garbage or out-of-mapped-region bytes into the merged output jar contents — an analog of the mDNS bug's "trust declared length over actual buffer" pattern, but here in a memory-safety-relevant native code path instead of a Python slice.

## Impact Explanation
An out-of-bounds mmap read in a native `singlejar` binary that runs as part of every Java build. Depending on how far past the mapping the size overruns, this can crash the build (segfault reading unmapped memory) or, if adjacent mapped pages happen to be readable, leak adjacent process memory bytes into the resulting merged JAR that gets built into the output artifact — a content-integrity violation analogous to the "cache corruption" impact called out in the advisory (attacker-shaped bytes end up in a build artifact trusted by downstream consumers).

## Likelihood Explanation
Requires a JAR dependency (URL, artifact, or CI-checked-in file) crafted with an internally inconsistent Local/Central Header size field, but that JAR can still have a valid, attacker-known sha256/integrity hash (the checksum covers the whole file's bytes, not internal ZIP field consistency), so pinned-checksum verification does not stop this. Any project consuming an externally-supplied JAR through `http_jar`/`http_archive`/`maven_install` that gets passed through `singlejar` (essentially every `java_library`/`java_binary`/`java_import` target) is a candidate victim.

## Recommendation
Add an explicit bounds check (mirroring `third_party/ijar/zip.cc`'s `EnsureRemaining`) in `InputJar`/`TransientBytes` before consuming `lh->uncompressed_file_size()`/`lh->compressed_file_size()`: verify `lh->data() + declared_size <= mapped_file_.end()` (and analogous check for `cdh`-derived sizes) prior to `Append`/`DecompressEntryContents`, failing the build with a diagnostic instead of performing the unchecked memory copy/inflate call.

## Proof of Concept
Not independently reproduced in this session — I traced the code paths listed above (`InputJar::NextEntry`, `LH::uncompressed_file_size`/`compressed_file_size`, `TransientBytes::ReadEntryContents`/`DecompressEntryContents`) via static reading of `src/tools/singlejar/input_jar.h`, `src/tools/singlejar/zip_headers.h`, and `src/tools/singlejar/transient_bytes.h`, and confirmed the contrasting bounds-checked implementation in `third_party/ijar/zip.cc`, but I did not have the tools available in this session to build and run a `BuildIntegrationTestCase`/shell-test PoC crafting a malformed JAR with an inconsistent Local Header size to empirically trigger the out-of-bounds read. A background Devin session with repo/build access would be needed to construct and run such a proof (e.g., via `src/tools/singlejar/input_jar_bad_jar_test.cc`-style harness with a hand-crafted LH declaring an oversized `uncompressed_file_size`).

### Citations

**File:** src/tools/singlejar/input_jar.h (L70-85)
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
    *local_header_ptr = LocalHeader(current_cdh);
    return current_cdh;
  }
```

**File:** src/tools/singlejar/zip_headers.h (L240-262)
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

**File:** src/tools/singlejar/transient_bytes.h (L87-110)
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

    while (in_bytes > 0) {
      // A single region to inflate cannot exceed 4GB-1.
      uint32_t in_bytes_chunk = 0xFFFFFFFF;
      if (in_bytes_chunk > in_bytes) {
        in_bytes_chunk = in_bytes;
      }
      inflater->DataToInflate(data, in_bytes_chunk);
```

**File:** third_party/ijar/zip.cc (L161-170)
```text
  int EnsureRemaining(size_t n, const char *state) {
    size_t in_offset = p - zipdata_in_;
    size_t remaining = input_file_->Length() - in_offset;
    if (n > remaining) {
      return error("Premature end of file (at offset %zd, state=%s); "
                   "expected %zd more bytes but found %zd.\n",
                   in_offset, state, n, remaining);
    }
    return 0;
  }
```

**File:** third_party/ijar/zip.cc (L420-435)
```text
int InputZipFile::SkipFile(const bool compressed) {
  if (!compressed) {
    // In this case, compressed_size_ == uncompressed_size_ (since the file is
    // uncompressed), so we can use either.
    if (compressed_size_ != uncompressed_size_) {
      return error("compressed size != uncompressed size, although the file "
                   "is uncompressed.\n");
    }
  }

  if (EnsureRemaining(compressed_size_, "file_data") < 0) {
    return -1;
  }
  p += compressed_size_;
  return 0;
}
```
