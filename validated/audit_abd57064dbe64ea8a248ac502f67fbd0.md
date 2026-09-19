### Title
OOB read past mmap'd input jar in `InputZipFile::ProcessCentralDirEntry` due to unchecked extra-field/length fields - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` (`third_party/ijar/zip.cc:493-545`) parses the ZIP central directory of an input jar that ijar strips into an interface jar. Unlike `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before reading `file_name`/`extra_field` [1](#0-0) , `ProcessCentralDirEntry` never calls `EnsureRemaining` and instead advances `p`/`extra_p` purely based on attacker-controlled 16-bit length fields taken straight from the file (`file_name_length`, `extra_field_length`, `data_size` inside each extra-field record) [2](#0-1) .

### Finding Description
The whole input jar is `mmap`'d read-only with exactly `length` bytes (the file size), with no guard page or padding: `mmap(NULL, length, PROT_READ, MAP_PRIVATE, fd, 0)` [3](#0-2) . Contrast this with the *output* mapping, which explicitly over-allocates a guard page specifically "to ensure that any buffer overflow ... will result in SIGSEGV or SIGBUS" [4](#0-3)  — no equivalent protection/bookkeeping exists for reads on the input side beyond simple SIGSEGV-on-overrun.

Inside `ProcessCentralDirEntry`, after copying/skipping the file name, the code parses the extra-field area purely by counting bytes according to embedded, attacker-controlled sizes:
```
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  ...
}
```
`third_party/ijar/zip.cc:523-542`. This is structurally identical to the kernel bug class described in the report: a fixed-size record header contains an item/record count or size that is trusted to bound a loop, and the loop is never additionally bounded by the actual size of the surrounding buffer (here, the mmap'd file). If `data_size` for a record does not evenly divide the remainder of `extra_field_length`, `extra_p` overshoots `p` and the `while (extra_p != p)` condition never becomes false — the loop keeps calling `get_u2le`/`get_u8le` on ever-increasing addresses, walking past the end of the `extra_field_length`-declared region, past the current central-directory record, past the rest of the central directory, and ultimately past the end of the mmap'd region entirely (`p` and `extra_p` are raw pointers with no upper-bound check anywhere in this function, unlike `EnsureRemaining` in the local-header path). Because the mapping is exactly file-sized with no trailing guard, reads can walk into adjacent unmapped pages (crash) or, if the file size happens to leave slack within the last mapped page, read adjacent heap/mapped bytes that are not part of the ZIP file and copy them into `*compressed_size`, `*uncompressed_size`, `*attr`, `*offset` (via `get_u8le(extra)` for the ZIP64 extra field, `third_party/ijar/zip.cc:531-541`) — an out-of-bounds read whose result feeds into subsequent decompression/size accounting for the output ijar.

### Impact Explanation
This function is reached whenever Bazel invokes the `ijar` tool to strip an interface jar from a `.jar` file, which happens for `java_library`/`java_import`-style targets, including jars whose *bytes* originate from an external, attacker-controlled source (e.g., a `http_jar`/`http_archive`-fetched dependency, or any other pipeline that feeds an untrusted `.jar` into ijar). No credential, sandbox-escape, or root-repo Starlark trust is required — only that ijar is asked to process a malicious jar file, i.e. bytes served by a hostile origin that a victim's build consumes. The corrupted length/size fields are never validated against the mapped file's actual bounds in `ProcessCentralDirEntry`, so a hostile jar can drive out-of-bounds reads immediately adjacent to (or past) the mapped input, causing a crash (denial of service) or corrupting the size/offset accounting used later, potentially leaking adjacent memory content into internal state consumed by ijar's output-size calculation (`CalculateOutputLength`, `third_party/ijar/zip.cc:550-581`) and entry processing (`ProcessNext`, `zip.cc:302-330`).

### Likelihood Explanation
High for a crash: any attacker who can get their content into a jar consumed by ijar (a common step in ordinary Java/Bazel builds for any external jar dependency) can trivially craft an extra-field with `data_size` values that don't reconcile with `extra_field_length`, with no additional privilege needed. The only variable determining crash-vs-leak is whether the overshoot lands within the same mapped page (info read) or crosses into an unmapped page (SIGSEGV), which the attacker can influence by choosing file size/padding.

### Recommendation
In `ProcessCentralDirEntry`, bound every pointer advance against the mapped file's end (`zipdata_in_ + input_file_->Length()`) the same way `ProcessLocalFileEntry` uses `EnsureRemaining` before reading `file_name_length_`/`extra_field_length_` bytes. Specifically:
- Verify `file_name_length` and `extra_field_length` do not exceed the bytes remaining in the mapped file before advancing `p`.
- In the `while (extra_p != p)` loop, additionally require `extra_p + 4 <= p` before reading `header_id`/`data_size`, and require `extra_p + data_size <= p` before advancing/dereferencing, breaking out (and erroring) rather than looping past `p` on a mismatch — mirroring the fix pattern already used defensively in `src/tools/singlejar/zip_headers.h`'s `ExtraField::find`, which checks `byte_ptr(start) + extra_field->size() > byte_ptr(end)` before trusting a record's size [5](#0-4) .

### Proof of Concept
A JUnit/`src/test/shell` proof (analogous to the existing `MalformedExtraField` regression test for singlejar, `src/tools/singlejar/output_jar_simple_test.cc:1179-1236`) would:
1. Construct a minimal ZIP whose central directory header (`CDH`) declares `extra_field_length = N` but whose embedded extra-field record's `data_size` is deliberately larger than `N` (or not a divisor of `N`), analogous to `CreateZipWithMalformedExtraField()` [6](#0-5) .
2. Place this crafted entry at (or near) the very end of the mmap'd file so the OOB read from `extra_p += data_size` walks off the mapped region.
3. Invoke ijar's extractor (`ZipExtractor`/`InputZipFile::ProcessNext` via `zip_main.cc`/`ijar.cc`) on this file and observe a SIGSEGV/heap-overread (ASan would flag it) rather than a clean parse error, demonstrating the missing bounds check in `ProcessCentralDirEntry`.

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

**File:** third_party/ijar/mapped_file_unix.cc (L47-65)
```text
  off_t length = lseek(fd, 0, SEEK_END);
  if (length < 0) {
    snprintf(errmsg, MAX_ERROR, "lseek(): %s", strerror(errno));
    errmsg_ = errmsg;
    return;
  }

  void* buffer = mmap(NULL, length, PROT_READ, MAP_PRIVATE, fd, 0);
  if (buffer == MAP_FAILED) {
    snprintf(errmsg, MAX_ERROR, "mmap(): %s", strerror(errno));
    errmsg_ = errmsg;
    return;
  }

  impl_ = new MappedInputFileImpl();
  impl_->fd_ = fd;
  buffer_ = reinterpret_cast<u1*>(buffer);
  length_ = length;
  opened_ = true;
```

**File:** third_party/ijar/mapped_file_unix.cc (L106-112)
```text
  // Ensure that any buffer overflow in JarStripper will result in
  // SIGSEGV or SIGBUS by over-allocating beyond the end of the file.
  size_t mmap_length =
      std::min(static_cast<size_t>(estimated_size + sysconf(_SC_PAGESIZE)),
               std::numeric_limits<size_t>::max());
  void* mapped =
      mmap(NULL, mmap_length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
```

**File:** src/tools/singlejar/zip_headers.h (L99-114)
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
```

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1192-1201)
```text
  // 2. Extra field payload containing an oversized payload_size
  uint8_t ef_buffer[8] = {0};
  auto* ef1 = reinterpret_cast<ExtraField*>(ef_buffer);
  ef1->signature(0x000d);
  ef1->payload_size(0);

  auto* ef2 = reinterpret_cast<ExtraField*>(ef_buffer + ef1->size());
  ef2->signature(0xdead);
  ef2->payload_size(0xf000);  // Malformed size exceeding extra field buffer

```
