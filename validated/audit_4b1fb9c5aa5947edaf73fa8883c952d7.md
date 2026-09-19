## Title
Buffer over-read parsing untrusted ZIP central directory entries in ijar's ZIP extractor — (File: `third_party/ijar/zip.cc`)

### Summary
Bazel's ijar tool (used to build interface jars from `.jar`/`.aar`/zip-format dependency archives, including those fetched from external repositories such as `http_jar`/`http_archive` or Maven artifacts) parses ZIP central directory records in `InputZipFile::ProcessCentralDirEntry`. Unlike the local-file-header parser, this function advances the read cursor using attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) without ever verifying that those lengths stay within the bytes remaining in the mapped input file. This is the same bug class as CVE-2018-14587: a length-driven memory-stream operation that trusts an untrusted, attacker-supplied size field instead of checking it against the actual buffer bounds, producing a buffer over-read.

### Finding Description
`InputZipFile::ProcessLocalFileEntry` protects every cursor advance with `EnsureRemaining()`, which checks that the declared field size does not exceed `input_file_->Length() - (p - zipdata_in_)` before dereferencing: [1](#0-0) [2](#0-1) 

`InputZipFile::ProcessCentralDirEntry`, however, reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from the untrusted archive and unconditionally advances `p` (and copies `file_name_length` bytes into a fixed local buffer via `memcpy`) with **no equivalent `EnsureRemaining` bounds check**: [3](#0-2) 

The only validation upstream is a single aggregate check in `FindZipCentralDirectory` that `central_dir_offset + central_dir_size <= in_length`, i.e. that the *whole* central directory region fits inside the mapped file: [4](#0-3) 

This aggregate check does not constrain any *individual* entry's declared `file_name_length`/`extra_field_length`/`file_comment_length`. A malicious archive can place a central directory entry near the end of the (correctly-sized) central directory region but declare a name/extra/comment length large enough that `p` (and the `extra_p`/`memcpy` reads inside the ZIP64 extra-field loop at lines 524-542) run past `zipdata_in_ + in_length`, which is the end of the `mmap`'d input file — an out-of-bounds read of the process's address space, directly analogous to `AP4_MemoryByteStream::WritePartial`'s buffer over-read on an untrusted, length-prefixed byte stream.

### Impact Explanation
`zipdata_in_` is a memory-mapped view of the archive file (see `MappedInputFile`/`Open()` referenced by `InputZipFile`), so reading past `in_length` reads adjacent unmapped or unrelated process memory. Depending on page alignment this can crash the `ijar` helper process (denial of service for that build action) or, since the over-read bytes are subsequently copied into the `filename` buffer and used/printed via `error()`, could leak adjacent memory contents into build diagnostics. The attacker only needs to control the bytes of an archive that Bazel's build pipeline runs through ijar (e.g., a `.jar` produced from a fetched dependency).

### Likelihood Explanation
Any build that depends on a `java_library`-style jar dependency invokes ijar on that jar to produce the interface jar; the archive bytes are fully attacker-controlled when served from an untrusted registry/mirror/URL. No credentials, sandbox escape, or local access are required — the crafted ZIP central directory is the sole payload, and the existing checks (`EnsureRemaining` in the local-file-header path, and the aggregate central-directory-size check) do not cover the per-entry name/extra/comment length fields consumed in `ProcessCentralDirEntry`.

### Recommendation
Add per-field bounds checks in `ProcessCentralDirEntry` (mirroring `EnsureRemaining` used in `ProcessLocalFileEntry`) so that `file_name_length`, `extra_field_length`, and `file_comment_length`, plus the fixed 46-byte central directory record header, are validated against the actual bytes remaining before `p` is advanced or `memcpy`'d, both for the top-level fields and for the ZIP64 extra-field walk (`header_id`/`data_size` loop).

### Proof of Concept
Construct a ZIP archive whose end-of-central-directory record correctly reports `central_dir_offset`/`central_dir_size` (so `FindZipCentralDirectory`'s aggregate check passes), but whose last central directory entry declares `file_name_length` (or `extra_field_length`/`file_comment_length`) large enough that `central_dir_offset + central_dir_size` still fits in `in_length`, yet parsing that single entry's fields walks `p`/`extra_p` past `zipdata_in_ + in_length`. Running such a file through `ZipExtractor::Create`/`ProcessAll` (as ijar does when building an interface jar) triggers the out-of-bounds read; this can be encoded as a new case in ijar's existing `zip_test.cc`-style unit tests that feed a hand-crafted malformed archive buffer to `FindZipCentralDirectory`/`ProcessCentralDirEntry` and observe the over-read under ASan.

### Citations

**File:** third_party/ijar/zip.cc (L158-170)
```text
  // Check that at least n bytes remain in the input file, otherwise
  // abort with an error message.  "state" is the name of the field
  // we're about to read, for diagnostics.
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

**File:** third_party/ijar/zip.cc (L507-530)
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
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
