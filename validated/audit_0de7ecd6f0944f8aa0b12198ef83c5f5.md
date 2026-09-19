### Title
Heap-based buffer over-read in ijar's central-directory extra-field parser via unchecked Zip64 field length - (File: third_party/ijar/zip.cc)

### Summary
CVE-2018-12097 describes an out-of-bounds read in a file-parsing routine (`liblnk_location_information_read_data`) that trusts a length field embedded in attacker-supplied file data without validating it against the remaining buffer size, producing an information-disclosure heap over-read. Bazel's own ZIP central-directory parser in `InputZipFile::ProcessCentralDirEntry` (`third_party/ijar/zip.cc`) exhibits the same bug class: it walks a chain of "extra field" records using an attacker-controlled 16-bit `data_size` without ever checking that `data_size` (or the cumulative offset `extra_p`) stays within the bounds of the declared `extra_field_length` region or the memory-mapped file.

### Finding Description
`ProcessCentralDirEntry` reads the central-directory header fields directly from the memory-mapped ZIP file [1](#0-0) , including `file_name_length`, `extra_field_length`, and `file_comment_length`, all taken verbatim from attacker-controlled bytes.

It then walks the "extra fields" region using a loop bounded by `extra_p != p`, where `p` was advanced by the untrusted `extra_field_length`: [2](#0-1) 

Inside the loop, `header_id` and `data_size` are read from the attacker-controlled bytes, and `extra_p` is advanced by `data_size` with no check that `data_size` does not exceed the remaining bytes before `p` (the computed end of the extra-field region). If `data_size` is crafted to overshoot `p`, the loop condition `extra_p != p` can skip past its terminating value entirely (since `extra_p` increases by an attacker-chosen amount rather than a fixed step), causing the loop to continue reading `header_id`/`data_size` pairs from memory beyond the declared extra-field boundary — and, if the terminating central-directory entry is the last one in the mapped file, beyond the end of the mapped file's central directory. When `header_id == ZIP64_EXTRA_FIELD_TAG`, this out-of-bounds data is copied via `get_u8le` into `*uncompressed_size`, `*compressed_size`, and `*offset` [3](#0-2) , and the resulting `*offset` is later used to set the parse cursor `p = zipdata_in_ + in_offset_ + offset` for the next local-file-header read [4](#0-3) .

This mirrors the CVE's bug class: an unvalidated length field in attacker-controlled archive/file metadata is trusted to walk a buffer, allowing reads past the intended structure boundary. There is no equivalent of `EnsureRemaining` (used in `ProcessLocalFileEntry`, e.g. [5](#0-4) ) guarding the extra-field walk inside `ProcessCentralDirEntry`.

### Impact Explanation
`ProcessCentralDirEntry` and the surrounding `InputZipFile`/`ZipExtractor` machinery in `third_party/ijar/zip.cc` back both the `ijar` tool (interface-jar generation) and `PartialZipExtractor::UnzipUntil` in `src/main/cpp/archive_utils.cc`, which is used to install/extract Bazel's own embedded archive and can also be reached when Bazel processes ZIP-format inputs supplied to these code paths. A heap over-read here could leak adjacent heap memory content (via the derived offsets/sizes influencing which bytes get copied out as "file content") or crash the process (info disclosure / potential DoS), consistent with the CVSS profile of the CVE (`C:H`, `I:N`, `A:N`).

### Likelihood Explanation
Exploitability requires only that an attacker control the bytes of a ZIP file consumed by this parser — e.g. a hostile artifact reachable through Bazel's own installation/archive-extraction path, without any pinned/verified checksum protecting the specific bytes parsed here (unlike `http_archive`'s Java-side decompressors, which are gated by `sha256`/`integrity`). Because this is native mmap-based parsing with no bounds assertion in this specific loop (unlike the sibling `ProcessLocalFileEntry`, which does call `EnsureRemaining`), the likelihood of the loop reading past the intended region when fed a crafted `data_size` is high once the code path is reached.

### Recommendation
Add explicit bounds validation before dereferencing `extra_p` in `ProcessCentralDirEntry`'s extra-field loop: ensure `extra_p + 4 <= p` before reading `header_id`/`data_size`, and ensure `extra_p + data_size <= p` before advancing/dereferencing `extra`. This mirrors the existing `EnsureRemaining` pattern already used in `ProcessLocalFileEntry` and should be applied consistently to all extra-field/central-directory parsing in `third_party/ijar/zip.cc` and `zip_headers.h`'s `Zip64ExtraField::find`/`UnixTimeExtraField::find` helpers, which perform similar unbounded walks.

### Proof of Concept
I was not able to fully verify a concrete, buildable JUnit/C++ reproduction within the available tool budget — I could not confirm the exact byte-advance semantics of `get_u2le`/`get_u4le`/`get_u8le` (defined in `third_party/ijar/common.h`, which I located but did not get to read before running out of iterations), nor locate/confirm a test harness (e.g. under `third_party/ijar/*_test.cc`) that exercises `ProcessCentralDirEntry` directly with a crafted central-directory entry. A concrete PoC would need to:
1. Construct a minimal ZIP file with one central-directory entry whose `extra_field_length` is small (e.g. 4 bytes) but whose embedded extra-field record declares a `data_size` larger than the remaining extra-field bytes (and positioned so the record trailing bytes/offset land near/past the end of the mapped file).
2. Feed it to `InputZipFile::Open`/`ProcessNext` (or directly unit-test `ProcessCentralDirEntry`) and, under ASan, observe a heap-buffer-overflow (read) report inside the `while (extra_p != p)` loop or in the subsequent `get_u8le(extra)` calls.

Given I could not confirm the pointer-advance semantics of `get_u2le`/`get_u4le`/`get_u8le` or locate an existing test scaffold to adapt, I recommend a Devin session with file/terminal access to read `third_party/ijar/common.h`, build `third_party/ijar` under ASan, and craft/run the byte-level PoC described above to confirm the over-read before treating this as fully validated.

### Citations

**File:** third_party/ijar/zip.cc (L312-314)
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

**File:** third_party/ijar/zip.cc (L507-515)
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
```

**File:** third_party/ijar/zip.cc (L523-542)
```text
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
