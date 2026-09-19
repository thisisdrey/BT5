### Title
Missing bounds validation in ZIP central-directory parsing causes out-of-bounds read past the mapped archive buffer - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry()` in Bazel's `ijar` tool parses the Central Directory Header (CDH) of a ZIP/JAR file that is `mmap`'d into memory, but unlike its sibling `ProcessLocalFileEntry()`, it never calls `EnsureRemaining()` before advancing the cursor `p` past attacker-controlled, length-prefixed fields (`file_name_length`, `extra_field_length`, `file_comment_length`) or before iterating attacker-controlled "extra field" TLV records. A crafted jar/zip whose CDH declares lengths that exceed the actual file size drives `p`/`extra_p` past the end of the mapped buffer, producing an out-of-bounds read, in the same bug class as CVE-2022-2881 (read past end of buffer, information disclosure or crash).

### Finding Description
`ProcessLocalFileEntry()` is careful: it calls `EnsureRemaining(file_name_length_, "file_name")` and `EnsureRemaining(extra_field_length_, "extra_field")` before advancing `p` [1](#0-0) .

`ProcessCentralDirEntry()`, however, reads `file_name_length`, `extra_field_length`, and `file_comment_length` straight from the untrusted CDH and advances `p` by these attacker-supplied values with **no bounds check at all**: [2](#0-1) 

It then walks the "extra fields" region using a TLV loop keyed purely on the untrusted `data_size` field:
```
while (extra_p != p) {
    const u2 header_id = get_u2le(extra_p);
    const u2 data_size = get_u2le(extra_p);
    const u1 *extra = extra_p;
    extra_p += data_size;
    ...
}
``` [3](#0-2) 

If `data_size` does not evenly divide the declared `extra_field_length`, or if `file_name_length`/`extra_field_length` push `p` beyond the actual mmap'd file boundary, `extra_p` never equals `p` and the loop keeps calling `get_u2le(extra_p)` on memory beyond the end of the mapped archive — an unbounded out-of-bounds read until the process reads unmapped memory and segfaults, or reads adjacent process memory into `header_id`/`data_size`. The `memcpy(filename, p, len)` a few lines earlier in the same function also copies from `p`, which may already be beyond the file's real bytes at that point [4](#0-3) .

This is invoked from `InputZipFile::ProcessNext()`, which is the main entry point iterating every entry of an input jar during ijar's interface-jar extraction of a jar dependency [5](#0-4) . The comment above the function ("the central directory is always followed by another data structure that has a signature, so parsing it this way is safe") is the incorrect invariant that the missing bounds check relies on [6](#0-5) .

### Impact Explanation
An attacker who controls the bytes of a `.jar`/`.zip` consumed by a Bazel build (e.g., an artifact served from a hostile Maven/HTTP mirror that a `java_import`/`maven_install`/`http_jar` rule feeds into `ijar` for interface-jar generation) can craft a Central Directory Header with an inconsistent `extra_field_length`/TLV `data_size` to force the parser to read past the end of the mmap'd archive buffer. This can crash the Bazel build process (denial of availability during the build) or leak adjacent process memory bytes into the parsed filename/attribute fields, which then influence generated interface-jar contents — matching the "read past end of the buffer and either read memory it should not read, or crash the process" description of CVE-2022-2881.

### Likelihood Explanation
Reachable whenever ijar parses an externally-supplied jar whose exact bytes are not required to match a previously-pinned strong hash before ijar processing begins (e.g., first-time fetches, or any path where `ProcessNext`/`ProcessCentralDirEntry` runs on untrusted bytes). The bug requires only a malformed CDH extra-field length, which is trivial for a hostile origin server to construct; no privileged access or MITM is needed — the attacker only needs to control content the build fetches and runs `ijar` over.

### Recommendation
Add `EnsureRemaining()` checks in `ProcessCentralDirEntry()` before advancing `p` for `file_name_length`, `extra_field_length`, and `file_comment_length`, mirroring `ProcessLocalFileEntry()`. Additionally, bound the extra-field TLV loop so that `extra_p + data_size` can never exceed `p`/the mapped buffer end, and fail parsing with an error instead of looping indefinitely when a TLV entry's declared size overruns the extra-field region.

### Proof of Concept
Construct a minimal ZIP with one Central Directory Header entry where:
- `extra_field_length` = N (some small value, e.g. 4), but the single TLV entry inside declares `data_size` larger than `N` (e.g. 0xFFFF).
- The file is truncated shortly after the CDH so that the next bytes are not another valid header/EOCD signature.

Feed this file to `ijar <crafted.jar> <output.jar>` (the same code path Bazel invokes via `InputZipFile::ProcessNext` → `ProcessCentralDirEntry`). Expect the process to read past the mapped file's end (observable via ASan `heap-buffer-overflow`/`SEGV` under a fuzzer, or a crash under production build) instead of returning a graceful "invalid central file header" error. A JUnit/`BuildIntegrationTestCase` equivalent would build a `java_import` pointing at this crafted jar and assert Bazel returns a structured build error rather than crashing/hanging.

### Citations

**File:** third_party/ijar/zip.cc (L302-330)
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
  if (signature == LOCAL_FILE_HEADER_SIGNATURE) {
    if (ProcessLocalFileEntry(compressed, uncompressed) < 0) {
      return false;
    }
  } else {
    error("local file header signature for file %s not found\n", filename);
    return false;
  }

  return true;
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

**File:** third_party/ijar/zip.cc (L491-492)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
```

**File:** third_party/ijar/zip.cc (L507-523)
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
```

**File:** third_party/ijar/zip.cc (L526-542)
```text
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
