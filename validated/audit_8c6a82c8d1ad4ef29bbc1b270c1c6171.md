## Title
Integer overflow in zip64 local-header offset bypasses `InputZipFile::EnsureRemaining` bounds check, causing out-of-bounds heap read in ijar - ([File: third_party/ijar/zip.cc])

### Summary
`third_party/ijar/zip.cc` parses ZIP/JAR central-directory records and, for entries using the ZIP64 extension, replaces a 32-bit `offset` field with an unchecked 64-bit value taken directly from the archive's "extra field" bytes. That value is later used to compute a pointer into the memory-mapped input file. The only guard against reading past the end of the mapped buffer, `EnsureRemaining`, performs an **unsigned subtraction** that underflows when the attacker-controlled offset exceeds the file length, silently defeating the check — the same "unconstrained numeric limb overflows the bound check" class of bug as the reported Hintstore `mem_ptr` issue, just realized as a 64-bit file offset instead of a field element.

### Finding Description
In `InputZipFile::ProcessCentralDirEntry`, the local-header offset is read as a 32-bit little-endian value and overridden with a full 64-bit value pulled straight from the ZIP64 extra field, with no range validation against the file size: [1](#0-0) 

That attacker-controlled `offset` is then added to the mapped-file base pointer to compute the read cursor for the "local file header": [2](#0-1) 

Before dereferencing `p`, the code calls `EnsureRemaining`, which is supposed to reject offsets that run past the end of the file: [3](#0-2) 

`in_offset` and `remaining` are `size_t` (unsigned). If `offset` (attacker-controlled, up to 2^64-1) makes `p` point far beyond `zipdata_in_ + input_file_->Length()`, then `remaining = input_file_->Length() - in_offset` **underflows** to a huge unsigned value instead of failing, so `n > remaining` is false and the check passes even though `p` is wildly out of bounds. The subsequent `get_u4le(p)` and further field reads in `ProcessLocalFileEntry` then dereference attacker-influenceable out-of-bounds memory relative to the mmap'd region.

The only prior sanity check, `FindZipCentralDirectory`, validates that the central directory itself fits in the file: [4](#0-3) 
but it never validates individual entries' (zip64-extended) `local_header_offset`, `compressed_size`, or `uncompressed_size` fields, which is exactly the gap exploited here — analogous to how Hintstore's per-limb byte range checks failed to bound the reconstructed 32-bit value against BabyBear's modulus.

### Impact Explanation
`ijar` (the interface-jar stripping tool) is run by Bazel's Java toolchain over `.jar` inputs, including artifacts that originate from external, network-fetched dependencies. A maliciously crafted JAR/ZIP served at a dependency URL can trigger an out-of-bounds read from the memory-mapped archive during the Bazel build, potentially crashing the build process (denial of the build) or leaking adjacent heap memory content into the interface jar's parsed fields (which can affect generated output). This is an integrity/containment failure of the archive parser: attacker-controlled numeric data escapes its intended bound and is used directly as a memory pointer.

### Likelihood Explanation
Reaching this code only requires the attacker to control the bytes of a ZIP/JAR file that Bazel processes with `ijar` — e.g., a dependency artifact fetched from an untrusted or compromised URL/mirror where the build does not yet have a pinned/verified checksum, or any JAR built from an untrusted branch's inputs. No local access, credentials, or privileged position is required; the crafted zip64 extra field is trivial to construct (a standard `PKZIP` central directory entry with a `0xFFFFFFFF` placeholder offset and an attacker-chosen 64-bit value in the ZIP64 extra field).

### Recommendation
In `InputZipFile::ProcessCentralDirEntry`/`ProcessNext`, validate that the (possibly zip64-extended) `offset`, `compressed_size`, and `uncompressed_size` are each less than `input_file_->Length()` before using them, and rewrite `EnsureRemaining` to detect underflow explicitly (e.g., check `in_offset > input_file_->Length()` first and fail) rather than relying on unsigned subtraction wrap-around to "happen" to be safe.

### Proof of Concept
Construct a ZIP file whose single central directory entry has:
- `local_header_offset32 = 0xFFFFFFFF` (marks zip64 extension in use)
- A ZIP64 extra field (tag `0x0001`) containing an 8-byte offset field set to a huge value, e.g. `0x00000000FFFFFFF0` (larger than the actual file length)

Feeding this file to the `ijar`/`zipper` tool (`third_party/ijar/zip_main.cc`, which drives `InputZipFile::ProcessNext`) causes `p = zipdata_in_ + in_offset_ + offset` to point far outside the mapped region; `EnsureRemaining(4, "signature")` computes `remaining = Length() - in_offset`, which underflows to a large positive number instead of failing, and the subsequent `get_u4le(p)` performs an out-of-bounds read. A `BuildIntegrationTestCase`/shell test invoking `ijar` on such a crafted `.jar` (mirroring `third_party/ijar/test/zip_test.sh`) reproduces the crash/OOB read.

### Citations

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

**File:** third_party/ijar/zip.cc (L302-319)
```text
bool InputZipFile::ProcessNext() {
  // Process the next entry in the central directory. Also make sure that the
  // content pointer is in sync.
  u8 compressed, uncompressed;
  u8 offset;
  if (!ProcessCentralDirEntry(central_dir_current_, &compressed, &uncompressed,
                              filename, PATH_MAX, &attr, &offset)) {
    return false;
  }

  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

  if (EnsureRemaining(4, "signature") < 0) {
    return false;
  }
  u4 signature = get_u4le(p);
```

**File:** third_party/ijar/zip.cc (L507-542)
```text
  p += 16;  // skip to 'compressed size' field
  *compressed_size = get_u4le(p);
  *uncompressed_size = get_u4le(p);
  u2 file_name_length = get_u2le(p);
  u2 extra_field_length = get_u2le(p);
  u2 file_comment_length = get_u2le(p);
  p += 4;  // skip to external file attributes field
  *attr = get_u4le(p);
  *offset = get_u4le(p);
  {
    size_t len = (file_name_length < filename_size)
      ? file_name_length
      : (filename_size - 1);
    memcpy(reinterpret_cast<void*>(filename), p, len);
    filename[len] = 0;
  }
  p += file_name_length;
  const u1 *extra_p = p;
  p += extra_field_length;
  while (extra_p != p) {
    const u2 header_id = get_u2le(extra_p);
    const u2 data_size = get_u2le(extra_p);
    const u1 *extra = extra_p;
    extra_p += data_size;
    if (header_id == ZIP64_EXTRA_FIELD_TAG) {
      if (*uncompressed_size == U4_MAX) {
        *uncompressed_size = get_u8le(extra);
      }
      if (*compressed_size == U4_MAX) {
        *compressed_size = get_u8le(extra);
      }
      if (*offset == U4_MAX) {
        *offset = get_u8le(extra);
      }
    }
  }
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
