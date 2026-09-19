Confirmed: `get_u4le`/`get_u2le`/`get_u8le` in `third_party/ijar/common.h` perform raw unchecked pointer reads/advances with no bounds checking whatsoever — the caller is entirely responsible for ensuring enough bytes remain. `ProcessCentralDirEntry` in `third_party/ijar/zip.cc` never calls `EnsureRemaining` (unlike `ProcessLocalFileEntry`, which does), so it is the analog surface.

### Title
Global-buffer-overflow (OOB read) walking unvalidated ZIP central-directory/extra-field records in ijar - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` (`third_party/ijar/zip.cc:493-545`) parses attacker-controlled ZIP/JAR central-directory records and their per-entry "extra field" TLV list with no bounds validation against the mapped file length, mirroring the `pcapngoptionwalk` class of bug in hcxtools where TLV/option records are walked without checking they stay inside the buffer.

### Finding Description
`CalculateOutputLength()` (`zip.cc:550-581`) walks the central directory in a `while (true)` loop, calling `ProcessCentralDirEntry(current, ...)` repeatedly and relying solely on encountering a non-`CENTRAL_FILE_HEADER_SIGNATURE` value to stop. Inside `ProcessCentralDirEntry`:
- `get_u4le(p)`/`get_u2le(p)` (`zip.cc:497-515`) are called before any check that `p` plus the fixed 46-byte CDH size is within `input_file_->Length()`. These helpers (`common.h:39-72`) unconditionally dereference and advance the pointer — they contain no bounds check at all.
- `memcpy(filename, p, len)` (`zip.cc:516-522`) and `p += file_name_length` / `p += extra_field_length` / `p += file_comment_length` (`zip.cc:507-525`) advance the cursor using attacker-supplied 16-bit length fields with no check that these lengths are within the remaining mapped file.
- The extra-field TLV walk (`zip.cc:526-541`) reads `header_id`/`data_size` via `get_u2le` and then `get_u8le(extra)` (8 bytes) for zip64 attributes, again with no check that `extra_p + 4` or `extra_p + 8` stay within `extra_field_length` or within the mapped file bounds.

Because a crafted CDH entry's `file_name_length`, `extra_field_length`, or `file_comment_length` can push `p`/`current` arbitrarily far — including past the end of the `mmap`'d input file — the *next* loop iteration's `get_u4le(p)` in `ProcessCentralDirEntry` dereferences memory beyond the mapped region: a global (heap/mmap) out-of-bounds read, directly analogous to `pcapngoptionwalk`'s unchecked TLV walk in hcxtools.

Contrast this with `ProcessLocalFileEntry` (`zip.cc:332-370`), which explicitly calls `EnsureRemaining()` before every variable-length read — showing the codebase's own established invariant (bounds-check before consuming attacker-controlled lengths) that `ProcessCentralDirEntry` fails to uphold.

### Impact Explanation
This tool (`ijar`) processes JAR/ZIP files that are frequently produced from externally-fetched, attacker-influenced artifacts (e.g., prebuilt `.jar`s pulled in via `http_jar`/`http_archive`/`java_import` at a URL an unprivileged attacker controls, pinned by a `sha256`/`integrity` value the attacker themselves chose when publishing the artifact). A crafted central directory record causes an out-of-bounds read that can crash the `ijar` process (denial of the build step) or, depending on adjacent memory layout, read heap data beyond the mapped input into the parsed filename/central-directory state that subsequently drives file selection/output-length computation — a heap-overread with potential for information exposure or corruption of derived accounting (e.g., `CalculateOutputLength`'s size arithmetic), consistent with C/I impact of the CVE analog (CWE-125 out-of-bounds read).

### Likelihood Explanation
Likelihood is high once an attacker can supply the raw bytes of a JAR/ZIP that Bazel will feed into `ijar` (e.g., as a pinned dependency whose bytes the attacker authored at publish time). No privileged access, no MITM, and no bypass of an existing checksum is required — the integrity check (sha256/integrity) only pins *which* attacker-authored bytes are used, not that those bytes are well-formed ZIP structures; a hostile-but-checksum-valid artifact reaches this exact unguarded parsing path deterministically.

### Recommendation
Add explicit bounds checks (analogous to `EnsureRemaining` used in `ProcessLocalFileEntry`) in `ProcessCentralDirEntry` before every fixed-size read and before every pointer advance driven by `file_name_length`, `extra_field_length`, `file_comment_length`, and the nested extra-field TLV walk (validate `header_id`/`data_size` reads stay within `extra_field_length` and within `input_file_->Length()`), erroring out on malformed/truncated records instead of trusting attacker-controlled length fields.

### Proof of Concept
A JUnit/`googletest`-based reproduction would construct a minimal ZIP buffer (as done in `src/tools/singlejar/zip_headers_test.cc`) containing:
1. A valid End-Of-Central-Directory record so `FindZipCentralDirectory` succeeds.
2. A single Central Directory Header entry whose `extra_field_length` (or `file_comment_length`/`file_name_length`) is set to a value that pushes the resulting cursor past the end of the allocated/mapped buffer.

Feed this buffer to `InputZipFile::Open()` + `CalculateOutputLength()` (or drive `ProcessCentralDirEntry` directly, as `zip_headers_test.cc` exercises header classes) under AddressSanitizer; the subsequent `get_u4le(p)` call on the next loop iteration triggers a `global-buffer-overflow`/heap-buffer-overflow read report, mirroring the ASan crash reported for `pcapngoptionwalk` in CVE-2021-32286. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** third_party/ijar/zip.cc (L332-370)
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
```

**File:** third_party/ijar/zip.cc (L493-545)
```text
bool InputZipFile::ProcessCentralDirEntry(const u1 *&p, u8 *compressed_size,
                                          u8 *uncompressed_size, char *filename,
                                          size_t filename_size, u4 *attr,
                                          u8 *offset) {
  u4 signature = get_u4le(p);

  if (signature != CENTRAL_FILE_HEADER_SIGNATURE) {
    if (signature != DIGITAL_SIGNATURE && signature != EOCD_SIGNATURE &&
        signature != ZIP64_EOCD_SIGNATURE) {
      error("invalid central file header signature: 0x%x\n", signature);
    }
    return false;
  }

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

**File:** third_party/ijar/zip.cc (L550-581)
```text
u8 InputZipFile::CalculateOutputLength() {
  const u1* current = central_dir_;

  u8 compressed_size = 0;
  u8 uncompressed_size = 0;
  u8 skipped_compressed_size = 0;
  u4 attr;
  u8 offset;
  char filename[PATH_MAX];

  while (true) {
    u8 file_compressed, file_uncompressed;
    if (!ProcessCentralDirEntry(current,
                                &file_compressed, &file_uncompressed,
                                filename, PATH_MAX, &attr, &offset)) {
      break;
    }

    if (processor->Accept(filename, attr)) {
      compressed_size += (u8) file_compressed;
      uncompressed_size += (u8) file_uncompressed;
    } else {
      skipped_compressed_size += file_compressed;
    }
  }

  // The worst case is when the output is simply the input uncompressed. The
  // metadata in the zip file will stay the same, so the file will grow by the
  // difference between the compressed and uncompressed sizes.
  return (u8) input_file_->Length() - skipped_compressed_size
      + (uncompressed_size - compressed_size);
}
```

**File:** third_party/ijar/common.h (L39-72)
```text
inline u1 get_u1(const u1 *&p) {
    return *p++;
}

inline u2 get_u2be(const u1 *&p) {
    u4 x = (p[0] << 8) | p[1];
    p += 2;
    return x;
}

inline u2 get_u2le(const u1 *&p) {
    u4 x = (p[1] << 8) | p[0];
    p += 2;
    return x;
}

inline u4 get_u4be(const u1 *&p) {
    u4 x = (p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3];
    p += 4;
    return x;
}

inline u4 get_u4le(const u1 *&p) {
    u4 x = (p[3] << 24) | (p[2] << 16) | (p[1] << 8) | p[0];
    p += 4;
    return x;
}

inline u8 get_u8le(const u1 *&p) {
  u4 lo = get_u4le(p);
  u4 hi = get_u4le(p);
  u8 x = ((u8)hi << 32) | lo;
  return x;
}
```
