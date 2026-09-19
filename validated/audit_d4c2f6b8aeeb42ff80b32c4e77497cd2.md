### Title
Heap buffer over-read in central directory entry parsing due to unrestricted length fields - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads attacker-controlled `file_name_length`, `extra_field_length`, and `file_comment_length` fields from a central directory record and advances the parsing cursor `p` by these lengths without ever validating them against the remaining bytes of the mapped input buffer [1](#0-0) . This is directly analogous to CVE-2017-17858, where MuPDF's `ensure_solid_xref` trusted unrestricted, attacker-supplied xref subsection object numbers to index/advance internal structures without bounds checking, leading to a heap-based buffer overflow.

### Finding Description
`FindZipCentralDirectory` validates only that `central_dir_offset + central_dir_size <= in_length` before returning a pointer to the start of the central directory [2](#0-1) . It performs no per-entry validation of the fields inside individual central directory headers.

The actual per-entry parsing happens in `ProcessCentralDirEntry`, which is invoked in a loop from `ProcessNext` for each file in the archive [3](#0-2) . Inside `ProcessCentralDirEntry`, the function reads three attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) directly from the mapped file content and then unconditionally advances the shared cursor `p` (which aliases `central_dir_current_`) by `file_name_length + extra_field_length + file_comment_length`, with **no call to `EnsureRemaining`** to confirm the cursor stays within the mapped buffer:

```
p += 16;  // skip to 'compressed size' field
*compressed_size = get_u4le(p);
*uncompressed_size = get_u4le(p);
u2 file_name_length = get_u2le(p);
u2 extra_field_length = get_u2le(p);
u2 file_comment_length = get_u2le(p);
...
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
...
p += file_comment_length;
return true;
``` [4](#0-3) 

Contrast this with the sibling function `ProcessLocalFileEntry`, which does the equivalent parsing for local file headers and explicitly guards every pointer advance with `EnsureRemaining`:
```
if (EnsureRemaining(file_name_length_, "file_name") < 0) {
  return -1;
}
file_name_ = p;
p += file_name_length_;

if (EnsureRemaining(extra_field_length_, "extra_field") < 0) {
  return -1;
}
``` [5](#0-4) 

`ProcessCentralDirEntry` has no equivalent check. Because `p`/`central_dir_current_` is advanced with attacker-controlled, unrestricted 16-bit lengths (up to 65535 each, ~196KB total per entry) with no upper bound tied to the actual remaining central directory or mapped-file size, repeated calls (one per declared central directory entry) can walk the cursor arbitrarily far past the end of the memory-mapped input file. The very next iteration of the loop then calls `get_u4le(p)` to read a "signature" from this out-of-bounds pointer , and if that happens to look like `CENTRAL_FILE_HEADER_SIGNATURE`, parsing continues, walking further into (or past) the process's heap, copying unrelated heap memory into the `filename` buffer via `memcpy` [6](#0-5)  and reporting attacker-influenceable `attr`/`offset` values that are later dereferenced to locate the "local file header" (`p = zipdata_in_ + in_offset_ + offset` in `ProcessNext`) [7](#0-6) , which is itself unchecked before use — an unrestricted-offset primitive that can point far outside the mapped buffer.

This mirrors the CVE-2017-17858 bug class precisely: a size/index/offset value taken from crafted, untrusted archive metadata is not restricted to the bounds of the actual container before being used to advance a parsing cursor and subsequently dereferenced.

### Impact Explanation
An attacker who supplies a crafted `.jar`/`.zip` file — for example, as a `http_jar`/`http_archive` dependency, a prebuilt `.jar` checked into an untrusted branch, or any archive consumed by `ijar` or `singlejar` during a build — can trigger heap out-of-bounds reads (and copies of adjacent heap memory into internal buffers, e.g. `filename`) purely through unrestricted length/offset fields in the central directory. This can crash the build tool process (denial of service for that build invocation) or leak heap memory content into a filename that ijar acts upon, depending on subsequent processing.

### Likelihood Explanation
This code path is exercised whenever `ijar`/`zip.cc`'s `InputZipFile` is used to read a `.jar`/`.zip` archive — this happens for any jar processed by the ijar tool (used to build interface jars from java targets) and is reachable purely from the bytes of an externally supplied jar/zip file. No special privilege beyond publishing/serving the archive content is required; there is no sha256/integrity check that would catch a mismatch in internal structure (checksums, if present at the http_archive/http_jar level, validate the archive as a whole, not that internal ZIP metadata fields are self-consistent/bounded before this parser trusts them).

### Recommendation
Add bounds checks (analogous to those already present in `ProcessLocalFileEntry`'s use of `EnsureRemaining`) to `ProcessCentralDirEntry` before advancing `p` by `file_name_length`, `extra_field_length`, and `file_comment_length`, and validate the returned entry `offset` against the mapped file length before it is used to compute the local file header pointer in `ProcessNext`.

### Proof of Concept
I could not fully verify a complete, buildable JUnit/shell reproduction within the available exploration budget (the index does not expose enough of the `EnsureRemaining` implementation, `MappedInputFile` internals, or existing `third_party/ijar` test harness to construct and confirm a byte-exact crafted `.zip` PoC in this session). A concrete reproduction would require: (1) building an on-disk `.zip` with a valid EOCD/central-dir-offset/size that passes `FindZipCentralDirectory`'s single sanity check, but whose first central directory header declares `file_name_length`/`extra_field_length`/`file_comment_length` summing to a value that pushes `p` past `zipdata_in_ + input_file_->Length()`, and (2) driving it through `ZipExtractor`/`InputZipFile::ProcessNext()` in a small C++ test (e.g., under `third_party/ijar/zip_test.cc` if present) or via the `singlejar`/`ijar` command-line binary, ideally run under ASan to observe the heap-buffer-overflow/over-read report. This should be validated with a background Devin session that has full repository and build access.

### Citations

**File:** third_party/ijar/zip.cc (L302-310)
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
```

**File:** third_party/ijar/zip.cc (L312-315)
```text
  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

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

**File:** third_party/ijar/zip.cc (L507-544)
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
  p += file_comment_length;
  return true;
```

**File:** third_party/ijar/zip.cc (L766-777)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }

  // Do not change output values before determining that they are OK.
  *offset = cd.central_dir_offset;
  // Central directory start can then be used to determine the actual
  // starts of the zip file (which can be different in case of a non-zip
  // header like for auto-extractable binaries).
  *central_dir = end_of_central_dir - cd.central_dir_size;
  return true;
```
