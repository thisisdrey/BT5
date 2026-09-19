### Title
Heap-based buffer over-read in ijar's ZIP central directory parser via unchecked entry length fields - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in Bazel's embedded `ijar` tool parses ZIP central directory headers by reading attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) directly from the mapped input file and advancing the read cursor `p` by those values, without ever validating that the resulting pointer stays within the bounds of the memory-mapped file or within the previously-validated central-directory region.

### Finding Description
`ZipExtractor::Create` → `InputZipFile::Open` calls `FindZipCentralDirectory`, which validates that `cd.central_dir_offset + cd.central_dir_size <= in_length` [1](#0-0) . This only bounds the *aggregate* size of the central directory against the file length; it does not validate any individual entry.

Iteration over the central directory is then done entry-by-entry by `InputZipFile::ProcessCentralDirEntry`, called from `ProcessNext()` and `CalculateOutputLength()` [2](#0-1) . Inside that function, `file_name_length`, `extra_field_length`, and `file_comment_length` are read straight off the mapped bytes and used to advance `p` with no comparison against `mapped_file`/`in_length` remaining bytes: [3](#0-2) 

Note the function's own comment asserts safety only by convention ("the central directory is always followed by another data structure that has a signature, so parsing it this way is safe") [4](#0-3)  — this assumption does not hold for a maliciously crafted archive where `file_name_length`/`extra_field_length`/`file_comment_length` are set to arbitrarily large 16-bit values, pushing `p` past the actual end of the mmap'd file. The subsequent `memcpy(filename, p, len)` call reads from `p`, which can already point outside the mapped region [5](#0-4) , and the next invocation of `ProcessCentralDirEntry` (or the ZIP64 extra-field walk at lines 526-542) continues reading multi-byte little-endian fields from that out-of-bounds pointer via `get_u4le`/`get_u2le`/`get_u8le`.

This is structurally identical to CVE-2018-18196 in libgig's `RIFF::List::GetListTypeString`: a length/type field taken from untrusted chunked/container data is used to index/advance a read cursor without validating it against the buffer's actual extent, producing a heap-based buffer over-read.

Unlike `InputZipFile::ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before consuming `file_name_length`/`extra_field_length` bytes [6](#0-5) , the central-directory parser has no equivalent guard.

### Impact Explanation
`ijar` is Bazel's built-in tool for generating interface jars, and is invoked on JAR/ZIP-format files that can originate from untrusted external sources (e.g., prebuilt jars fetched via `http_jar`/`http_archive`/`java_import`, or artifacts pulled from a Maven/Bzlmod registry). SHA-256/`integrity` pinning of the whole-file bytes does not prevent an attacker who legitimately controls the published file's *content* (i.e., the file that matches the pinned hash was itself crafted maliciously) from shaping a ZIP whose central directory entry length fields are inconsistent with the real file size. The over-read can leak adjacent heap memory into the ijar output (potential information disclosure written into the interface jar consumed downstream) or crash the `ijar` process.

### Likelihood Explanation
Any build that consumes a prebuilt jar/zip artifact through `ijar` (which Bazel does for essentially every non-source `.jar` dependency to build interface jars) is exposed. The unprivileged attacker only needs to control the bytes of a single dependency artifact (a hostile mirror, a malicious release artifact whose hash the victim pins believing it to be legitimate, or any similarly reachable untrusted-but-hash-pinned file) — no special access to the build machine, credentials, or trusted root-repo Starlark is required.

### Recommendation
Add bounds checking in `InputZipFile::ProcessCentralDirEntry` (and in the ZIP64 extra-field walking loop) so that `file_name_length`, `extra_field_length`, and `file_comment_length` are validated against the number of remaining bytes in the mapped file (an `EnsureRemaining`-style check) before `p` is advanced or dereferenced, mirroring the existing guard already used in `ProcessLocalFileEntry`.

### Proof of Concept
A regression test can be added (e.g., alongside `third_party/ijar/zip_test.cc` conceptually, or as a `src/test/shell/bazel` shell test) that:
1. Crafts a minimal valid ZIP with one central directory entry.
2. Patches the entry's `file_name_length`/`extra_field_length`/`file_comment_length` fields to values far exceeding the remaining bytes in the file (e.g., `0xFFFF`), while keeping the EOCD's `central_dir_size`/`central_dir_offset` self-consistent so `FindZipCentralDirectory`'s aggregate check passes.
3. Runs the archive through `ijar`/`ZipExtractor::ProcessAll()` under AddressSanitizer, expecting a heap-buffer-overflow (over-read) report instead of a clean parse error.

### Citations

**File:** third_party/ijar/zip.cc (L360-370)
```text
  if (EnsureRemaining(file_name_length_, "file_name") < 0) {
    return -1;
  }
  file_name_ = p;
  p += file_name_length_;

  if (EnsureRemaining(extra_field_length_, "extra_field") < 0) {
    return -1;
  }
  extra_field_ = p;
  p += extra_field_length_;
```

**File:** third_party/ijar/zip.cc (L491-492)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
```

**File:** third_party/ijar/zip.cc (L507-525)
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
```

**File:** third_party/ijar/zip.cc (L560-574)
```text
  while (true) {
    u8 file_compressed, file_uncompressed;
    if (!ProcessCentralDirEntry(current,
                                &file_compressed, &file_uncompressed,
                                filename, PATH_MAX, &attr, &offset)) {
      break;
    }

    if (processor->Accept(filename, attr)) {
      compressed_size += (u8) file_compressed;
      uncompressed_size += (u8) file_uncompressed;
    } else {
      skipped_compressed_size += file_compressed;
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
