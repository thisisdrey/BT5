### Title
Attacker-controlled `uncompressed_file_size` in a jar entry is not bounds-checked against the mapped input file before being copied, causing an out-of-bounds heap read that lands in the deploy jar output - (File: `src/tools/singlejar/transient_bytes.h`)

### Summary
`InputJar::LocateCentralDirectory` and `InputJar::NextEntry` validate that the Central Directory Header records and the Local Header pointer lie within the mapped input file [1](#0-0) , but nothing validates that `local_header->data() + uncompressed_file_size` (or `compressed_file_size`) stays inside the mapped bounds of the source jar. `TransientBytes::ReadEntryContents` in `transient_bytes.h` reads the size straight from the (attacker-supplied) CDH/LH and blindly copies that many bytes starting at `lh->data()` into the in-memory output buffer:

```
void ReadEntryContents(const CDH* cdh, const LH* lh) {
    uint64_t uncompressed_file_size;
    if (cdh->no_size_in_local_header()) {
      uncompressed_file_size = cdh->uncompressed_file_size();
    } else {
      uncompressed_file_size = lh->uncompressed_file_size();
    }
    Append(lh->data(), uncompressed_file_size);
}
``` [2](#0-1) 

### Finding Description
`singlejar` (the tool that builds Bazel "deploy jars"/merged jars from a set of `--sources` inputs) memory-maps every input jar and walks its Central Directory with `InputJar::NextEntry` [1](#0-0) . The only bounds check performed is that the *next CDH record* pointer stays within the mapped file (`mapped_file_.mapped(new_cdr)`); the *entry payload length* fields (`uncompressed_file_size`, `compressed_file_size`) that are stored inside the CDH/LH are never checked against the actual size of the mapped input, either in `input_jar.h`/`input_jar.cc` or in `zip_headers.h` accessors (`LH::uncompressed_file_size()`, `CDH::uncompressed_file_size()`) [3](#0-2) .

Several call sites then trust this attacker-controlled size to determine how many bytes to copy out of the mapped input:
- `TransientBytes::ReadEntryContents`, used for combining "uncompressed" resources (e.g. merged `META-INF/services` style entries), calls `Append(lh->data(), uncompressed_file_size)` with no upper bound check against the mapped file's end [2](#0-1) .
- `TransientBytes::DecompressEntryContents` similarly reads `in_bytes`/`out_bytes` straight from the header and feeds `data` (== `lh->data()`) directly to the inflater for that many bytes without checking that `data + in_bytes` is within the mapped region [4](#0-3) .

Because the input jar is `mmap`'d, reading past the recorded size does not fault immediately (it just reads adjacent heap/mapped pages, and can run past the mapping into unmapped memory only at page boundaries, but well before that it silently discloses adjacent heap contents of the process). This mirrors the class of bug in CVE-2018-1000204: a declared transfer size from an untrusted structure is not validated against the size of the real backing buffer, so the copy routine walks past the legitimate data and copies whatever bytes happen to follow in memory into the destination the attacker (via the victim who runs `singlejar`) can eventually observe — here, the resulting deploy/output jar, which is written to disk and can be inspected, unlike raw process memory.

The attacker model fits: a hostile jar can be supplied as a `data`/`srcs` dependency, a third-party JAR artifact fetched from a remote repository/registry, or a file in an untrusted branch that CI builds with Bazel's `java_binary`/`java_library` `deploy_jar` action, which invokes `singlejar` on all of the target's jars. `singlejar` performs no cryptographic integrity check on entry sizes — it only checks structural offsets of the *headers*, not the payload extents — so this is not stopped by any existing checksum, containment, or lockfile mechanism.

### Impact Explanation
An attacker who can supply one input jar to a `singlejar` invocation (dependency archive, generated jar, or CI-consumed artifact) can craft a CDH/LH pair whose `uncompressed_file_size`/`compressed_file_size` field is larger than the number of bytes actually present for that entry in the mapped file. When `ReadEntryContents` (or the decompression path) processes that entry, it copies bytes beyond the entry's real payload — out-of-bounds heap/mapped-memory read — into the merged output jar contents. This can leak the contents of adjacent heap allocations (which may include unrelated file data mapped nearby, or heap metadata) into a build artifact the victim consumes or ships, and in the worst case can crash the builder if the out-of-bounds read crosses into an unmapped page (since `mmap` regions are page-aligned, a sufficiently large declared size will eventually read unmapped memory and segfault).

### Likelihood Explanation
Any project that merges third-party or CI-produced jars with `singlejar` (which underlies essentially all Bazel `java_binary`/`java_import`/`deploy_jar` targets) is affected. Producing a malicious jar with an inconsistent size field is a two-line binary edit to a normal zip and requires no special privileges — the attacker only needs to get their crafted jar onto the dependency graph that `singlejar` processes (dependency URL, package registry artifact, or file in a repo branch that CI builds), matching the "unprivileged, outsider-published content" model.

### Recommendation
Before trusting `uncompressed_file_size`/`compressed_file_size` from `LH`/`CDH`, validate that `lh->data() + size` (and, for the compressed path, `data + in_bytes`) does not exceed `mapped_file_.end()` for the specific `InputJar` being processed, failing the same way `NextEntry` already does for header offsets (`diag_errx` on `!mapped_file_.mapped(...)`). This check should be added at the point where `TransientBytes::ReadEntryContents`/`DecompressEntryContents` are invoked (or inside `LH::data()`/`uncompressed_file_size()`), and ideally centralized in `InputJar` so every consumer of entry payload gets the same guarantee.

### Proof of Concept
Construct a minimal zip (as in the existing `CreateZipWithMalformedExtraField`-style test harness in `output_jar_simple_test.cc`) with:
1. A Local File Header for an entry with `compression_method = 0` (stored) and only a few bytes of real payload data.
2. A crafted `uncompressed_file_size32` field far larger than the number of bytes actually present before the next header/EOCD, e.g. `0x0FFFFFF0`, while keeping the Central Directory / EOCD structurally valid so `InputJar::Open`/`LocateCentralDirectory` succeed.
3. Run `singlejar --sources evil.jar --output out.jar` (or invoke `OutputJar::AddJar` in a `BuildIntegrationTestCase`/gtest harness) so that entry is processed through `TransientBytes::ReadEntryContents` (or the "uncompressed" combiner path).
4. Observe (with ASan) an out-of-bounds heap read reported when copying past the mapped file's end, or observe that `out.jar`'s entry now contains data far beyond the crafted archive's real 8-byte payload — bytes that originated from adjacent process memory rather than the archive.

This confirms the missing bound check: the declared size in `LH`/`CDH`, not the actual bytes available in the mapped input, controls how much memory gets copied into the output artifact.

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

**File:** src/tools/singlejar/transient_bytes.h (L89-142)
```text
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
      for (;;) {
        uint32_t available_out = ensure_space();
        int ret = inflater->Inflate(append_position(), available_out);
        uint32_t inflated = available_out - inflater->available_out();
        if (Z_STREAM_END == ret) {
          // No more data to decompress. Update write position and we are done
          // for this input chunk.
          advance(inflated);
          break;
        } else if (Z_OK == ret) {
          // No more space in the output buffer. Advance write position, update
          // the number of remaining bytes.
          if (inflater->available_out()) {
            diag_errx(2,
                      "%s:%d: Internal error inflating %.*s: Inflate reported "
                      "Z_OK but there are still %" PRIu32
                      " bytes available in the output buffer",
                      __FILE__, __LINE__, lh->file_name_length(),
                      lh->file_name(), inflater->available_out());
          }
          advance(inflated);
        } else {
          diag_errx(2,
                    "%s:%d: Internal error inflating %.*s: inflate() call "
                    "returned %d (%s)",
                    __FILE__, __LINE__, lh->file_name_length(), lh->file_name(),
                    ret, inflater->error_message());
        }
      }
      data += in_bytes_chunk;
      in_bytes -= in_bytes_chunk;
    }
```

**File:** src/tools/singlejar/zip_headers.h (L255-269)
```text
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
