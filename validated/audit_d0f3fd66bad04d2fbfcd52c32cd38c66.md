### Title
Integer-underflow bypass of bounds checking allows out-of-bounds read of attacker-controlled ZIP/JAR data in ijar's `InputZipFile` - (File: third_party/ijar/zip.cc)

### Summary
`ijar`'s ZIP reader computes a raw file-content pointer directly from an attacker-controlled `local_header_offset` field of the ZIP central directory, then relies on `EnsureRemaining()` to keep all subsequent reads inside the memory-mapped input file. `EnsureRemaining()` computes `remaining = Length() - in_offset` using unsigned (`size_t`) arithmetic; if the attacker-supplied offset pushes the cursor past `Length()`, the subtraction underflows to a huge value, silently disabling every bounds check for the remainder of parsing that entry. This is directly analogous to the ImageMagick `-sample` `sample:offset` bug (CVE-2026-33905): an attacker-controlled offset is trusted without validating it against the buffer bounds before it's used to seek and read.

### Finding Description
`InputZipFile::ProcessNext()` reads the (attacker-controlled) `offset` field from the central directory entry via `ProcessCentralDirEntry()` and immediately computes a raw pointer from it: [1](#0-0) 

`offset` originates from `CDH::local_header_offset()` — a 32-bit field in the central directory that can additionally be sign/width-extended via a Zip64 extra field, both of which are fully attacker-controlled when parsing an untrusted jar/zip: [2](#0-1) 

The only guard against `offset` running off the end of the mapped file is `EnsureRemaining()`: [3](#0-2) 

Both `in_offset` and `remaining` are `size_t` (unsigned). If `offset` is chosen so that `p = zipdata_in_ + in_offset_ + offset` already lies past `zipdata_in_ + input_file_->Length()`, then `in_offset > Length()`, and `remaining = Length() - in_offset` underflows to a value near `SIZE_MAX`. The check `n > remaining` then always evaluates false, so `EnsureRemaining()` reports success even though the cursor is already out of bounds. All subsequent reads through this cursor — the 4-byte signature check, `extract_version_`, `compressed_size_`, `uncompressed_size_`, `file_name_length_`, `extra_field_length_`, and ultimately `file_name_`/`extra_field_`/file-data pointers used in `ProcessFile()`/`SkipFile()`/`UncompressFile()` — read out of bounds relative to the mmap'd input file: [4](#0-3) [5](#0-4) 

The `MappedInputFile` exposes only `Buffer()` and `Length()` with no slack/guard region documented, so nothing else in the call chain re-validates `p` against the mapped extent before dereferencing it: [6](#0-5) 

### Impact Explanation
This is an out-of-bounds *read* of a memory-mapped, attacker-supplied file. Depending on layout this can:
- Crash the `ijar` process (SIGSEGV) if the read crosses an unmapped page — a build-time DoS for the specific target, but more importantly
- Leak adjacent heap/mapped memory into the derived interface jar output via `processor->Process(filename, attr, file_data, uncompressed_size_)`, which copies `file_data` for `uncompressed_size_` bytes (also attacker-controlled) into the interface-jar artifact that becomes a build output/dependency for downstream compilation — a genuine confidentiality impact tied to CWE-125, matching the "OOB read" class of the reference advisory.

`ijar` runs over java_library/java_import-style dependency jars, including binary jars obtained from external repositories (e.g., via `maven_install`/`http_jar`/`http_file` fetches), so the ZIP content is attacker-published content that an unprivileged origin can control; a victim's build consumes it without further validation of internal offset fields (the surrounding `http_archive`/`repository_ctx.download` sha256/integrity check only pins the byte-identity of the whole downloaded archive, not the internal semantic validity of every ZIP offset field it contains, so a byte-identical, checksum-pinned but maliciously crafted jar still reaches this code path).

### Likelihood Explanation
Medium. Triggering it requires crafting a ZIP central directory entry whose `local_header_offset` (optionally amplified through the Zip64 extra field to a 64-bit value) points past the end of the file. Constructing such a malformed-but-parseable ZIP is straightforward with a hex editor or zip-library once the CDH layout is known (it's a public, well-documented format). No special privileges are needed beyond controlling the bytes of a jar that some Bazel target consumes and runs `ijar` on.

### Recommendation
In `EnsureRemaining()`, validate the offset defensively before subtracting: return an error if `in_offset > input_file_->Length()` (or equivalently check `p < zipdata_in_ || p > zipdata_in_ + input_file_->Length()`) prior to computing `remaining`, instead of relying on unsigned subtraction. Additionally, validate `offset` returned from `ProcessCentralDirEntry()` against `input_file_->Length()` immediately after it is read in `ProcessNext()`, before it is used to form the `p` pointer.

### Proof of Concept
A `BuildIntegrationTestCase`/shell-level reproduction:
1. Construct a minimal valid ZIP with one central directory entry whose `local_header_offset32` field is set to a value larger than the physical file length (e.g., `0xFFFF0000`), or use a Zip64 extra field to encode a 64-bit offset far beyond `Length()`.
2. Run the `ijar` tool (`third_party/ijar/zip_main.cc`'s `extract`/interface-jar generation path, e.g. via a `java_library` target whose dependency is this malformed jar) against this file.
3. Observe that `EnsureRemaining(4, "signature")` in `InputZipFile::ProcessNext()` does not report "Premature end of file" despite the offset being invalid, and that the subsequent `get_u4le(p)` / interface-jar copy reads memory outside the mapped file bounds (crash under ASan/valgrind, or leaked bytes appear in the produced `-interface.jar`).

**Note on verification**: I was unable to inspect `third_party/ijar/mapped_file_posix.inc` (file not found via the index — likely excluded due to index size limits), which would show exactly how much slack, if any, exists past the mapped region and whether the OS-level mmap padding would turn this into a hard crash versus a silent OOB read within a page. I recommend a Devin session with full filesystem access to confirm this detail and reproduce the crash/leak with ASan.

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

**File:** third_party/ijar/zip.cc (L332-418)
```text
int InputZipFile::ProcessLocalFileEntry(
    size_t compressed_size, size_t uncompressed_size) {
  if (EnsureRemaining(26, "extract_version") < 0) {
    return -1;
  }
  extract_version_ = get_u2le(p);
  general_purpose_bit_flag_ = get_u2le(p);

  if ((general_purpose_bit_flag_ & ~GENERAL_PURPOSE_BIT_FLAG_SUPPORTED) != 0) {
    return error("Unsupported value (0x%04x) in general purpose bit flag.\n",
                 general_purpose_bit_flag_);
  }

  compression_method_ = get_u2le(p);

  if (compression_method_ != COMPRESSION_METHOD_DEFLATED &&
      compression_method_ != COMPRESSION_METHOD_STORED) {
    return error("Unsupported compression method (%d).\n",
                 compression_method_);
  }

  // skip over: last_mod_file_time, last_mod_file_date, crc32
  p += 2 + 2 + 4;
  compressed_size_ = get_u4le(p);
  uncompressed_size_ = get_u4le(p);
  file_name_length_ = get_u2le(p);
  extra_field_length_ = get_u2le(p);

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

  bool is_compressed = compression_method_ == COMPRESSION_METHOD_DEFLATED;

  // If the zip is compressed, compressed and uncompressed size members are
  // zero in the local file header. If not, check that they are the same as the
  // lengths from the central directory, otherwise, just believe the central
  // directory
  if (compressed_size_ == 0 || compressed_size_ == U4_MAX) {
    compressed_size_ = compressed_size;
  } else {
    if (compressed_size_ != compressed_size) {
      return error("central directory and file header inconsistent\n");
    }
  }

  if (uncompressed_size_ == 0 || uncompressed_size_ == U4_MAX) {
    uncompressed_size_ = uncompressed_size;
  } else {
    if (uncompressed_size_ != uncompressed_size) {
      return error("central directory and file header inconsistent\n");
    }
  }

  if (processor->Accept(filename, attr)) {
    if (ProcessFile(is_compressed) < 0) {
      return -1;
    }
  } else {
    if (SkipFile(is_compressed) < 0) {
      return -1;
    }
  }

  if (general_purpose_bit_flag_ & GENERAL_PURPOSE_BIT_FLAG_COMPRESSED) {
    // Skip the data descriptor. Some implementations do not put the signature
    // here, so check if the next 4 bytes are a signature, and if so, skip the
    // next 12 bytes (for CRC, compressed/uncompressed size), otherwise skip
    // the next 8 bytes (because the value just read was the CRC).
    u4 signature = get_u4le(p);
    if (signature == DATA_DESCRIPTOR_SIGNATURE) {
      p += 4 * 3;
    } else {
      p += 4 * 2;
    }
  }

  return 0;
}
```

**File:** third_party/ijar/zip.cc (L437-480)
```text
u1* InputZipFile::UncompressFile() {
  size_t in_offset = p - zipdata_in_;
  size_t remaining = input_file_->Length() - in_offset;
  DecompressedFile *decompressed_file =
      decompressor_->UncompressFile(p, remaining);
  if (decompressed_file == NULL) {
    if (decompressor_->GetError() != NULL) {
      error(decompressor_->GetError());
    }
    return NULL;
  } else {
    compressed_size_ = decompressed_file->compressed_size;
    uncompressed_size_ = decompressed_file->uncompressed_size;
    u1 *uncompressed_data = decompressed_file->uncompressed_data;
    free(decompressed_file);
    p += compressed_size_;
    return uncompressed_data;
  }
}

int InputZipFile::ProcessFile(const bool compressed) {
  const u1 *file_data;
  if (compressed) {
    file_data = UncompressFile();
    if (file_data == NULL) {
      return -1;
    }
  } else {
    // In this case, compressed_size_ == uncompressed_size_ (since the file is
    // uncompressed), so we can use either.
    if (compressed_size_ != uncompressed_size_) {
      return error("compressed size != uncompressed size, although the file "
                   "is uncompressed.\n");
    }

    if (EnsureRemaining(compressed_size_, "file_data") < 0) {
      return -1;
    }
    file_data = p;
    p += compressed_size_;
  }
  processor->Process(filename, attr, file_data, uncompressed_size_);
  return 0;
}
```

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

**File:** third_party/ijar/mapped_file.h (L26-53)
```text
class MappedInputFile {
 private:
  MappedInputFileImpl *impl_;

 protected:
  const char* errmsg_;
  bool opened_;
  u1* buffer_;
  size_t length_;

 public:
  MappedInputFile(const char* name);
  virtual ~MappedInputFile();

  // If opening the file succeeded or not.
  bool Opened() const { return opened_; }

  // Description of the last error that happened.
  const char* Error() const { return errmsg_; }

  // The mapped contents of the file.
  u1* Buffer() const { return buffer_ ; }

  // The length of the file.
  size_t Length() const { return length_; }

  int Close();
};
```
