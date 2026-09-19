### Title
Heap-based buffer over-read in ijar's ZIP central-directory parser during interface-jar generation - (File: third_party/ijar/zip.cc)

### Summary
`FindZipCentralDirectory` validates that `cd.central_dir_offset + cd.central_dir_size <= in_length` before trusting the central directory region, but `InputZipFile::ProcessCentralDirEntry` (called in a loop by `CalculateOutputLength` and `ProcessNext`/`ProcessAll`) never re-checks that each entry's variable-length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) actually stay inside that validated region before advancing the cursor `p` and reading through it.

### Finding Description
`ProcessCentralDirEntry` reads the fixed 46-byte CDH fields, then reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from attacker-controlled bytes and unconditionally does: [1](#0-0) 
It then advances `p` by `file_name_length` and `extra_field_length`/`file_comment_length` with no bound check against `central_dir_` end or the mmap'd file length — unlike the analogous local-file-header parser `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before reading `file_name` and `extra_field`: [2](#0-1) 

The only prior validation is a single aggregate check in `FindZipCentralDirectory` that the whole central directory block fits in the file: [3](#0-2) 
This bounds the *total* central directory size but says nothing about individual entries: a crafted central directory can declare a `file_name_length`/`extra_field_length`/`file_comment_length` for one entry that is inconsistent with `central_dir_size`, causing `p` (and the inner extra-field walk in lines 524-542) to read past the mmap'd region — the same class of bug as ImageMagick's `BlobToStringInfo` heap over-read: a length field taken from untrusted format metadata is used to index/copy memory without validating it against the actual backing buffer size.

Since `InputZipFile::Open()` maps the file only up to its exact length via `MappedInputFile`, any read past `zipdata_in_ + input_file->Length()` is an out-of-bounds/heap over-read of adjacent memory (or a page-fault crash), fully analogous to the CVE's over-read during untrusted-format decoding.

### Impact Explanation
`ijar`/`zip.cc` is Bazel's interface-jar extraction/generation tool, which parses attacker-suppliable `.jar` (ZIP) files as part of normal Java build/dependency processing. A malicious jar (e.g., served from a compromised or hostile artifact source, or an intentionally malformed jar a victim's build consumes) can trigger an out-of-bounds read in the Bazel build process, leaking adjacent heap memory into build output/error messages or crashing the build (`filename[len] = 0` after `memcpy` at line 520-521 also risks writing past `filename_size` bounds interplay, though `len` is clamped there). This is a memory-safety violation in native (non-sandboxed) code executed during the build.

### Likelihood Explanation
Reaching this requires only a plausible JAR file with an internally-inconsistent central directory (a task any attacker producing/serving a jar controls completely); no cooperation from Bazel's HTTP/download integrity layer is needed since the bug is in interpreting bytes *after* they are already on disk and validated as "the right file" by checksum — the checksum does not protect against a maliciously-crafted-but-hash-matching or first-use-pinned file with adversarial ZIP structure.

### Recommendation
Add explicit remaining-bytes checks (mirroring `EnsureRemaining` in `ProcessLocalFileEntry`) in `InputZipFile::ProcessCentralDirEntry` before reading `file_name_length`, `extra_field_length`, and `file_comment_length` bytes, and before dereferencing the extra-field walk loop (lines 524-542), bounding all advances of `p` to the previously-validated `[central_dir_, central_dir_ + central_dir_size)` / mapped-file range.

### Proof of Concept
Not independently reproduced against a running Bazel build (index/tool access does not include execution). A reproduction would construct a JAR/ZIP file whose End-Of-Central-Directory record declares a `central_dir_size` that is internally consistent (passing the check at zip.cc:766-769) but whose first central-directory entry declares `file_name_length`/`extra_field_length`/`file_comment_length` values that push the cursor `p` past `central_dir_ + central_dir_size` (and past the actual mmap length), then feed this file to `ijar` (e.g., via a `java_import`/interface-jar generation path) and observe an ASan heap-buffer-overflow report or crash inside `InputZipFile::ProcessCentralDirEntry`. I was not able to execute this PoC in the current environment to confirm the exact crash signature; this should be validated with an ASan-instrumented build and a JUnit/shell test analogous to `src/test/shell/bazel/starlark_repository_test.sh`'s extraction tests before treating this as fully confirmed.

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

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
