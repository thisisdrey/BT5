### Title
Out-of-bounds read while parsing ZIP central-directory extra fields with unchecked length arithmetic - ([File: third_party/ijar/zip.cc])

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses the central-directory extra-field area of a ZIP/JAR file by walking a raw byte cursor using attacker-controlled 16-bit length fields, without ever verifying that the reads stay within the bounds established by `extra_field_length`, or within the mapped input file at all. This mirrors the Contiki-NG uip buffer bug class: a structure (here, a `[header_id:u2][data_size:u2]` extra-field record) is repeatedly cast/read out of a buffer at offsets derived from untrusted length fields, with no bounds check before each read.

### Finding Description
`FindZipCentralDirectory` locates and validates the central directory bounds against the mmap'd file length [1](#0-0) , and `InputZipFile::Open` maps the whole file and hands control to per-entry parsing [2](#0-1) .

Per-entry parsing happens in `ProcessCentralDirEntry`, which reads fixed header fields and then walks the extra-field data: [3](#0-2) 

The loop condition is `while (extra_p != p)`, where `p` was advanced by the attacker-supplied `extra_field_length` (`p += extra_field_length;` at line 525) with no check that `extra_field_length` actually corresponds to real, well-formed extra records, and no check that `extra_p + 4` (needed to read `header_id`/`data_size`) is still `<= p` before calling `get_u2le` twice. `extra_p` is then advanced by `data_size` — a 16-bit value fully controlled by the attacker — with no verification that `extra_p + data_size <= p`. A crafted `data_size` can make `extra_p` jump past `p` (skipping the loop's only termination check, since the loop only tests for *exact* equality, not `>=`), causing the loop to keep reading `header_id`/`data_size` pairs — and, if `header_id == ZIP64_EXTRA_FIELD_TAG`, an 8-byte `u8` via `get_u8le(extra)` — from memory far beyond the intended extra-field region, beyond the central directory, and potentially beyond the end of the mmap'd file entirely (lines 526-542, 531-540).

This is structurally identical to the CVE-2022-36053 pattern: a length-prefixed sub-structure is walked using attacker-controlled offsets/sizes, and the code casts/reads a fixed-size struct at each computed offset without confirming the struct fits inside the remaining buffer.

By contrast, the newer `src/tools/singlejar/zip_headers.h::ExtraField::find` implements the same extra-field walk correctly, checking `start + sizeof(ExtraField) > end` and `start + extra_field->size() > end` before every dereference [4](#0-3)  — confirming that `third_party/ijar/zip.cc`'s manual loop lacks the equivalent bounds discipline.

### Impact Explanation
`ijar` is invoked by Bazel to strip class files down to interface jars for any Java dependency, including third-party jars fetched via `http_jar`/`http_file`/Maven-style rules. An attacker who controls (or MITMs, if unpinned) the origin serving such a jar can craft `extra_field_length`/`data_size` values that drive `extra_p` past the true extra-field boundary and even past the mmap'd file end, causing reads of adjacent process memory (heap over-read / potential SIGSEGV on an unmapped page). At minimum this is an out-of-bounds read of process memory triggered by untrusted archive content processed during a build.

### Likelihood Explanation
Any Bazel build that consumes an externally supplied JAR/ZIP and passes it through `ijar` (a very common path for Java dependencies) is affected. The only precondition is an attacker-controlled or unauthenticated/unpinned archive being fed to the ijar tool — no privileged access or MITM of an integrity-checked channel is required if the archive itself is what's fetched (e.g. an unpinned `http_jar`, or a jar behind a URL the attacker controls before a checksum is ever recorded).

### Recommendation
Add explicit bounds checks in `ProcessCentralDirEntry`'s extra-field loop, mirroring `ExtraField::find` in `src/tools/singlejar/zip_headers.h`:
- Before reading `header_id`/`data_size`, verify `extra_p + 4 <= p`.
- After computing `data_size`, verify `extra_p + data_size <= p` before advancing, and abort/error otherwise instead of allowing the cursor to overshoot.
- Additionally bound-check all such offset walks against `zipdata_in_ + input_file_->Length()`, not just the locally computed `p`.

### Proof of Concept
Not fully verified within the scope of this investigation — I could not build/run a JUnit or shell-integration reproduction in this session. A concrete PoC would construct a minimal ZIP whose central directory entry sets `extra_field_length` to a small value (e.g. 4) but embeds a `data_size` in the first extra-field record large enough (e.g. `0xFFFF`) that `extra_p` jumps past `p`, then feeds this file to the `ijar` binary (`third_party/ijar/ijar.cc` entry point) and observes reads/crash beyond the mapped region (e.g. via ASan `heap-buffer-overflow` on `get_u2le`/`get_u8le` in `ProcessCentralDirEntry`). This should be constructed and executed by a background engineer with tool access, since I cannot execute code in this session.

### Citations

**File:** third_party/ijar/zip.cc (L507-545)
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
}
```

**File:** third_party/ijar/zip.cc (L704-769)
```text
bool FindZipCentralDirectory(const u1 *bytes, size_t in_length, u8 *offset,
                             const u1 **central_dir) {
  static const int MAX_COMMENT_LENGTH = 0xffff;
  static const int CENTRAL_DIR_LOCATOR_SIZE = 22;
  // Maximum distance of start of central dir locator from end of file
  static const int MAX_DELTA = MAX_COMMENT_LENGTH + CENTRAL_DIR_LOCATOR_SIZE;
  const u1* last_pos_to_check = in_length < MAX_DELTA
      ? bytes
      : bytes + (in_length - MAX_DELTA);
  const u1* current;
  bool found = false;

  for (current = bytes + in_length - CENTRAL_DIR_LOCATOR_SIZE;
       current >= last_pos_to_check;
       current-- ) {
    const u1* p = current;
    if (get_u4le(p) != EOCD_SIGNATURE) {
      continue;
    }

    p += 16;  // skip to comment length field
    u2 comment_length = get_u2le(p);

    // Does the comment go exactly till the end of the file?
    if (current + comment_length + CENTRAL_DIR_LOCATOR_SIZE
        != bytes + in_length) {
      continue;
    }

    // Hooray, we found it!
    found = true;
    break;
  }

  if (!found) {
    fprintf(stderr, "file is invalid or corrupted (missing end of central "
                    "directory record)\n");
    return false;
  }

  EndOfCentralDirectoryRecord cd;
  const u1* end_of_central_dir = current;
  get_u4le(current);  // central directory locator signature, already checked
  cd.number_of_this_disk = get_u2le(current);
  cd.disk_with_central_dir = get_u2le(current);
  cd.central_dir_entries_on_this_disk = get_u2le(current);
  cd.central_dir_entries = get_u2le(current);
  cd.central_dir_size = get_u4le(current);
  cd.central_dir_offset = get_u4le(current);
  u2 file_comment_length = get_u2le(current);
  current += file_comment_length;  // set current to the end of the central dir

  if (!FindZip64CentralDirectory(bytes, in_length, &end_of_central_dir, &cd)) {
    return false;
  }

  if (cd.number_of_this_disk != 0 || cd.disk_with_central_dir != 0 ||
      cd.central_dir_entries_on_this_disk != cd.central_dir_entries) {
    fprintf(stderr, "multi-disk JAR files are not supported\n");
    return false;
  }

  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```

**File:** third_party/ijar/zip.cc (L814-830)
```text
bool InputZipFile::Open() {
  MappedInputFile* input_file = new MappedInputFile(filename_);
  if (!input_file->Opened()) {
    snprintf(errmsg, sizeof(errmsg), "%s", input_file->Error());
    delete input_file;
    return false;
  }

  void *zipdata_in = input_file->Buffer();
  u8 central_dir_offset;
  const u1 *central_dir = NULL;

  if (!devtools_ijar::FindZipCentralDirectory(
          static_cast<const u1*>(zipdata_in), input_file->Length(),
          &central_dir_offset, &central_dir)) {
    errno = EIO;  // we don't really have a good error number
    error("Cannot find central directory");
```

**File:** src/tools/singlejar/zip_headers.h (L99-115)
```text
  static const ExtraField* find(uint16_t tag, const uint8_t* start,
                                const uint8_t* end) {
    while (start < end) {
      if (ziph::byte_ptr(start) + sizeof(ExtraField) > ziph::byte_ptr(end)) {
        break;
      }
      auto extra_field = reinterpret_cast<const ExtraField*>(start);
      if (ziph::byte_ptr(start) + extra_field->size() > ziph::byte_ptr(end)) {
        break;
      }
      if (extra_field->is(tag)) {
        return extra_field;
      }
      start = ziph::byte_ptr(start) + extra_field->size();
    }
    return nullptr;
  }
```
