### Title
Heap Buffer Over-read in `InputZipFile::ProcessCentralDirEntry` when parsing a malicious ZIP/JAR central directory - (File: third_party/ijar/zip.cc)

### Summary
`third_party/ijar/zip.cc` implements ijar's ZIP reader, used whenever Bazel needs to parse a jar (interface-jar generation, `singlejar`-adjacent tooling, and any code path that opens a `ZipExtractor` on an externally supplied `.jar`/`.zip`). `InputZipFile::ProcessLocalFileEntry()` defensively calls `EnsureRemaining()` before every variable-length read (file name, extra field, file data) to guarantee the read stays inside the mmap'd input file. `InputZipFile::ProcessCentralDirEntry()`, which walks the central directory to enumerate/validate every zip entry, performs no equivalent bounds check: it trusts `file_name_length`, `extra_field_length`, and `file_comment_length` taken directly from attacker-controlled central directory header (CDH) bytes and unconditionally advances the cursor `p`/`extra_p` and `memcpy`s from it.

### Finding Description
`FindZipCentralDirectory()` (third_party/ijar/zip.cc:704-778) validates only that the *aggregate* central directory fits inside the file:
```
if (cd.central_dir_offset + cd.central_dir_size > in_length) { ... return false; }
``` [1](#0-0) 

It does **not** validate the size/consistency of individual entries within that directory. `ProcessCentralDirEntry()` then parses each entry:
```
u2 file_name_length = get_u2le(p);
u2 extra_field_length = get_u2le(p);
u2 file_comment_length = get_u2le(p);
...
memcpy(reinterpret_cast<void*>(filename), p, len);
...
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) { ... }
p += file_comment_length;
``` [2](#0-1) 

There is no call analogous to `EnsureRemaining()` (which `ProcessLocalFileEntry` does use, see third_party/ijar/zip.cc:334-370) to confirm that `file_name_length + extra_field_length + file_comment_length` bytes actually remain inside the mmap'd file before they are read/copied. The surrounding comment even states the (false) safety assumption:
"Note that the central directory is always followed by another data structure that has a signature, so parsing it this way is safe." [3](#0-2) 

That assumption does not hold for an adversarially crafted CDH near the end of the mapped file: a large `file_name_length`/`extra_field_length` on the last central-directory entry drives `p` (and the source pointer passed to `memcpy`) past the end of the mmap region, producing a heap/mmap out-of-bounds read — the same bug class as CVE-2018-10549 (`exif_iif_add_value`): an unvalidated attacker-supplied length field drives a read past the end of a buffer while parsing untrusted structured binary metadata.

### Impact Explanation
An out-of-bounds read in `ProcessCentralDirEntry` can:
- Crash the Bazel build process (SIGSEGV) when the read crosses an unmapped page — a build-time denial for anyone building against the malicious archive.
- Leak adjacent heap/mmap memory into the copied `filename` buffer (via `memcpy`), which can end up embedded in build outputs, error messages, or interface-jar entry names, and could be inspected by the victim/attacker via subsequent output.

This is directly reachable by an unprivileged external party who publishes a crafted `.jar`/`.zip` at a dependency URL (e.g., via `http_jar`/`http_archive`, a `java_import` prebuilt jar, or a maven/registry artifact) that a victim's build subsequently fetches and processes with ijar. Because the attacker controls the artifact's bytes at publication time, they can also compute and publish a matching `sha256`/`integrity` value themselves — pinning the hash prevents undetected in-transit tampering, but does not prevent the original publisher from shipping a malformed but hash-consistent archive.

### Likelihood Explanation
Any Bazel build that processes prebuilt/third-party jars with ijar (extremely common — e.g. via `java_import`, `http_jar`, Maven dependencies resolved through `rules_jvm_external`/registries) will trigger `InputZipFile::ProcessCentralDirEntry` on the untrusted archive's central directory. No special build configuration is required beyond consuming an external jar dependency, making this readily reachable under default flags.

### Recommendation
Add bounds checks to `ProcessCentralDirEntry()` mirroring `EnsureRemaining()` in `ProcessLocalFileEntry()`: before reading/copying `file_name_length`, `extra_field_length`, and `file_comment_length` bytes, verify that `p + <length>` does not exceed the mmap'd file's end (`zipdata_in_ + input_file_->Length()`), and fail parsing with an error instead of proceeding, exactly as local file entries already do.

### Proof of Concept
Construct a ZIP file whose valid End-Of-Central-Directory record declares a `central_dir_size`/`central_dir_offset` that satisfies `FindZipCentralDirectory`'s aggregate check, but whose single CDH entry declares `file_name_length` (or `extra_field_length`/`file_comment_length`) large enough that `CDH_offset + fixed_header_size + file_name_length` exceeds the actual mapped file length (e.g., entry placed at the very end of a minimally sized file with `file_name_length = 0xFFFF`). Feeding this file to any ijar/`zip.cc`-based consumer (e.g., `ijar` binary's `-vt` list mode, or `bazel build` on a `java_import` target whose `jars` attribute points to this crafted file) is expected to trigger a heap/mmap out-of-bounds read in `InputZipFile::ProcessCentralDirEntry`, reproducible under ASan (`-fsanitize=address`) or by observing a SIGSEGV in `third_party/ijar`'s standalone `zip_main` tool. A `src/test/shell/bazel` or C++ unit test analogous to the existing `third_party/ijar/test/zip_test.sh` `test_no_path_traversal` case, but supplying such a truncated/oversized-length central directory entry and running it under ASan, should demonstrate the over-read.

**Note on confidence**: I was unable to actually build/run ijar under ASan or fully trace whether some other guard elsewhere in the mmap/`MappedInputFile` layer (not shown in the retrieved excerpts) implicitly prevents `p` from crossing the mapped region (e.g., via a guard page or a length check that occurs before `Open()` hands control to `ProcessCentralDirEntry`). The code excerpts examined (`FindZipCentralDirectory`, `ProcessCentralDirEntry`, `ProcessLocalFileEntry`) show no such per-entry bounds check, but I could not fully inspect `MappedInputFile`'s implementation or `InputZipFile::Open()` in this pass to rule out an additional safety net.

### Citations

**File:** third_party/ijar/zip.cc (L491-492)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
```

**File:** third_party/ijar/zip.cc (L507-543)
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
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
