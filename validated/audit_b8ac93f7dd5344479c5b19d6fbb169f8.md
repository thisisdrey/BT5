### Title
Missing bounds checking in `InputZipFile::ProcessCentralDirEntry` causes out-of-bounds heap read when parsing a hostile ZIP/JAR central directory - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar`'s central-directory parser reads attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) from a memory-mapped ZIP/JAR file and advances its read cursor by those lengths — and `memcpy`s from that cursor — without ever validating that the declared lengths stay inside the mapped buffer. This mirrors the ALPINE-CVE-2020-0093 bug class (missing bounds check on an attacker-influenced length field causing an out-of-bounds read).

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads a raw central directory header: [1](#0-0) 

`file_name_length`, `extra_field_length`, and `file_comment_length` are read directly from attacker-supplied bytes as `u2` values (up to 65535 each), and used to advance `p` and to bound a `memcpy` (`len = min(file_name_length, filename_size-1)`) with **no check that `p + len` (or `p + extra_field_length`, or `p + file_comment_length`) is still within the mapped file**. The extra-field walking loop has the same problem: [2](#0-1) 

Contrast this with the sibling function `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before reading variable-length fields: [3](#0-2)  and the `EnsureRemaining` helper itself: [4](#0-3) .

`ProcessCentralDirEntry` has no equivalent guard — this is exactly the missing-bounds-check pattern from the CVE (a length field trusted without a corresponding remaining-bytes check before the read).

The only integrity gate upstream is `FindZipCentralDirectory`, which validates that the *whole* central directory region fits in the file: [5](#0-4) 

This check bounds the aggregate central-directory size but does not validate that any individual entry's `file_name_length`/`extra_field_length`/`comment_length` is consistent with the space actually remaining before the mapped file's end. A crafted last (or only) central-directory entry can declare lengths that push `p` past `zipdata_in_ + input_file_->Length()`, causing `get_u2le`/`get_u4le`/`memcpy` in `ProcessCentralDirEntry` to read adjacent heap memory outside the `mmap`'d region.

### Impact Explanation
`ijar` (via `ZipExtractor`) is Bazel's own C++ archive-processing code, not a third-party dependency, and it is invoked on ZIP/JAR content that originates from outside the trust boundary:
- to build Java interface jars from any `.jar` dependency (including ones fetched via `http_archive`/Maven-style rules),
- to unpack Bazel's own embedded-tools archive in `src/main/cpp/archive_utils.cc` via `ZipExtractor::Create`/`UnzipUntil` ( [6](#0-5) ).

A hostile publisher of a dependency jar controls the file bytes and can therefore also compute a matching `sha256`/`integrity` value, so an integrity pin does not stop a maliciously-*structured* (but hash-correct) ZIP from being processed. The out-of-bounds read discloses adjacent heap contents (potentially including other in-process data such as buffered file contents, paths, or metadata) by copying them into the `filename[PATH_MAX]` buffer or by mis-parsing extra-field records, which are then used in `Accept()`/`Process()` callbacks and diagnostics. This is a local information-disclosure primitive analogous in class and severity to the exif OOB read (CWE-125), not a memory-corruption/RCE bug.

### Likelihood Explanation
Reaching the vulnerable code only requires supplying a ZIP/JAR whose central directory's last entry declares name/extra/comment lengths that exceed the actual remaining bytes in the mapped file — a small, deterministic malformation requiring no special privileges, race conditions, or trusted-root-repo BUILD/Starlark access. Any code path that runs `ijar` or `ZipExtractor` over attacker-supplied archive bytes (Java interface-jar generation over dependency jars, or Bazel's own self-extraction routine) triggers it.

### Recommendation
Add the same `EnsureRemaining`-style bounds validation used in `ProcessLocalFileEntry` to `ProcessCentralDirEntry` before every pointer advance and `memcpy`: validate `p + 46` (fixed header) is available before reading, then validate `p + file_name_length`, `p + extra_field_length`, and `p + file_comment_length` each stay within `[central_dir_, zipdata_in_ + input_file_->Length())` before dereferencing/copying, failing with `error(...)` on violation exactly as the local-file-header parser does.

### Proof of Concept
Not fully verified/executed — this is a static-analysis-based finding. A concrete reproduction would need a `BuildIntegrationTestCase`/C++ unit test (e.g., extending `third_party/ijar/test/IjarTests.java` or a new `zip_test.cc`) that constructs a minimal ZIP with:
1. A valid `EOCD` record satisfying `FindZipCentralDirectory`'s `central_dir_offset + central_dir_size <= in_length` check.
2. A single central-directory entry whose `file_name_length` (or `extra_field_length`/`file_comment_length`) is set to a value larger than the bytes actually remaining between that entry and the end of the mmap'd buffer.
3. Run under AddressSanitizer/`ASAN` while invoking `ZipExtractor::ProcessAll()`/`CalculateOutputLength()` (as done by ijar's main or `archive_utils.cc`'s `UnzipUntil`) to observe the heap-buffer-overflow read reported by ASan.

I was not able to locate an existing fuzzer or unit test in the repo that already exercises `ProcessCentralDirEntry` with malformed length fields, so this would need to be authored as new test code; I could not run it in this environment to confirm the crash empirically.

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

**File:** third_party/ijar/zip.cc (L524-542)
```text
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

**File:** src/main/cpp/archive_utils.cc (L56-84)
```text
  string UnzipUntil(const string &archive_path, const string &stop_entry,
                    vector<string> *entry_names = nullptr,
                    CallbackType &&callback = {}) {
    std::unique_ptr<devtools_ijar::ZipExtractor> extractor(
        devtools_ijar::ZipExtractor::Create(archive_path.c_str(), this));
    if (!extractor) {
      BAZEL_DIE(blaze_exit_code::LOCAL_ENVIRONMENTAL_ERROR)
          << "Failed to open '" << archive_path
          << "' as a zip file: " << blaze_util::GetLastErrorString();
    }
    stop_name_ = stop_entry;
    seen_names_.clear();
    callback_ = callback;
    done_ = false;
    while (!done_ && extractor->ProcessNext()) {
      // Scan zip until EOF, an error, or Accept() has seen stop_entry.
    }
    if (const char *err = extractor->GetError()) {
      BAZEL_DIE(blaze_exit_code::LOCAL_ENVIRONMENTAL_ERROR)
          << "Error reading zip file '" << archive_path << "': " << err;
    }
    if (!done_) {
      BAZEL_DIE(blaze_exit_code::LOCAL_ENVIRONMENTAL_ERROR)
          << "Failed to find member '" << stop_entry << "' in zip file '"
          << archive_path << "'";
    }
    if (entry_names) *entry_names = std::move(seen_names_);
    return stop_value_;
  }
```
