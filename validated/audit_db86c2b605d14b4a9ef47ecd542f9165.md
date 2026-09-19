### Title
Out-of-bounds heap read while parsing ZIP central directory entries in ijar - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in the `ijar` tool parses the central directory of a `.jar`/`.zip` file and consumes attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) without ever validating them against the actual size of the memory-mapped input file. A crafted archive (e.g. a prebuilt `.jar` fetched via `http_jar`/`http_file`, brought in through `java_import`, or checked into a branch that CI builds) whose final central-directory entry declares oversized length fields drives the cursor `p`/`extra_p` past the end of the mmap'd buffer, causing out-of-bounds heap reads during `memcpy` (filename) and `get_u2le`/`get_u4le`/`get_u8le` (extra-field parsing loop).

### Finding Description
`ProcessCentralDirEntry` reads the fixed fields of a Central Directory Header, then: [1](#0-0) 
- `file_name_length`, `extra_field_length`, and `file_comment_length` are taken directly from the file bytes.
- `p += file_name_length` is used as the source for a `memcpy` into `filename` at line 520, with **no check** that `p + file_name_length` stays inside the mapped buffer.
- The subsequent loop iterates `extra_p` from `p` to `p + extra_field_length`, reading a `header_id`/`data_size` pair and possibly 8-byte `Zip64` fields at `extra`, again with **no check** against the mapped buffer's bounds: [2](#0-1) 

This contrasts with the sibling function `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before consuming `file_name_length_` and `extra_field_length_`: [3](#0-2) 

Critically, `EnsureRemaining()` is defined in terms of the class member `p` (the file cursor), computing `remaining = input_file_->Length() - (p - zipdata_in_)`: [4](#0-3) 

But `ProcessCentralDirEntry` takes its own reference parameter also named `p` (shadowing the member), which is bound to `central_dir_current_` — a completely different cursor walking the central directory rather than the local/file-data cursor. Even if a developer wanted to reuse `EnsureRemaining()` here, it would compute against the wrong pointer. As a result, no bounds check whatsoever exists for the central directory's variable-length fields.

The only bounds validation that exists is at the initial location of the *central directory as a whole*, in `FindZipCentralDirectory`, which checks `cd.central_dir_offset + cd.central_dir_size > in_length`: [5](#0-4) 
This validates the aggregate central-directory region fits in the file, but never validates that an individual entry's `file_name_length + extra_field_length + file_comment_length` stays within that region or within the mapped file — the code's own comment optimistically assumes "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" — an assumption a malicious/corrupted archive can violate.

`InputZipFile::Open()` maps the file and hands control straight to this unchecked parser: [6](#0-5) 

### Impact Explanation
A crafted `.jar`/`.zip` processed by `ijar` (used to generate interface jars for Java compilation, including for prebuilt/imported jars) can drive reads past the end of the mmap'd input file. This is a heap/mapped-memory out-of-bounds read that can crash the build (SIGSEGV on unmapped pages) or, more subtly, leak adjacent heap memory into the `filename` buffer (via `memcpy`) or into interpreted `extra_field` values that influence subsequent processing/output — mirroring the CVE-2025-0437 bug class (OOB read via crafted content leading to heap corruption/information disclosure).

### Likelihood Explanation
Any project that consumes a prebuilt `.jar` from an untrusted origin (a compromised mirror whose URL/sha256 pin was itself copied from the attacker-controlled artifact, a dependency fetched via `http_jar`/`http_file`, or a `.jar` checked into an untrusted branch that CI builds with Bazel) will have that jar run through `ijar` during normal Java build actions (`java_import`, `java_library` interface-jar generation). Because the integrity check (sha256) validates only that the bytes match a pin — not that the ZIP structure is well-formed — a functionally malicious archive that matches its own declared hash sails straight past any checksum defense into this unguarded parser.

### Recommendation
Add explicit bounds checks in `ProcessCentralDirEntry` (and the extra-field loop) verifying that `p`, `extra_p`, and each computed field boundary never exceed `zipdata_in_ + input_file_->Length()` (i.e., the actual end of the mapped input), returning an error via `error(...)` instead of continuing to read, analogous to the `EnsureRemaining()` checks already used in `ProcessLocalFileEntry`.

### Proof of Concept
Construct a `.jar` whose central directory's last `CDH` entry declares `file_name_length` and/or `extra_field_length` values that, when added to the entry's offset, exceed the file's actual length (e.g., set `extra_field_length = 0xFFFF` while only a few bytes of real data follow before EOF). Feed this file to the `ijar` binary (`ijar <input.jar> <output.jar>`) or trigger it via a Bazel `java_import`/interface-jar action consuming this file as a dependency. Under ASan/valgrind this reproduces as a heap-buffer-overflow/read on the `memcpy` at `zip.cc:520` or on `get_u2le`/`get_u4le` calls inside the extra-field loop at `zip.cc:526-542`; a JUnit/shell test analogous to the existing `third_party/ijar/test/ijar_test.sh` `test_corrupted_end_of_centraldir` case, but crafting an oversized `extra_field_length`/`file_name_length` instead of truncating the file, demonstrates the missing bounds check.

### Citations

**File:** third_party/ijar/zip.cc (L161-170)
```text
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

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```

**File:** third_party/ijar/zip.cc (L814-846)
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
    delete input_file;
    return false;
  }
  const u1 *zipdata_start = static_cast<const u1*>(zipdata_in);
  in_offset_ = - static_cast<off_t>(zipdata_start
                                    + central_dir_offset
                                    - central_dir);

  input_file_ = input_file;
  zipdata_in_ = zipdata_start;
  central_dir_ = central_dir;
  central_dir_current_ = central_dir;
  p = zipdata_in_ + in_offset_;
  errmsg[0] = 0;
  return true;
}
```
