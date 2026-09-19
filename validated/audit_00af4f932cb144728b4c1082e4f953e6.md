### Title
Heap-based buffer over-read parsing ZIP/JAR central directory entries in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` reads a JAR/ZIP central directory record's fields — `file_name_length`, `extra_field_length`, `file_comment_length`, and the Zip64 extra-field `data_size` — directly from the mmap'd input buffer and advances the cursor `p` by attacker-controlled 16-bit lengths without any bounds check against the actual mapped file length, unlike its sibling `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every variable-length read.

### Finding Description
`ProcessCentralDirEntry` [1](#0-0)  reads:
- `file_name_length`, `extra_field_length`, `file_comment_length` as raw `u2` values from the mapped buffer,
- then does `p += file_name_length; ... p += extra_field_length; ... p += file_comment_length;`

and separately walks the Zip64 extra field with a length-prefixed loop:
```
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  ...
}
``` [2](#0-1)  — `data_size` is attacker-controlled and not validated against the remaining bytes of the `extra_field` region or the file itself, so `extra_p` can run past the intended `extra_field` boundary, and even past `p`, causing `get_u2le`/`get_u8le` to read from unmapped or out-of-bounds heap/mmap memory (`get_u2le`/`get_u8le` themselves perform no bounds checking) [3](#0-2) .

Contrast this with `ProcessLocalFileEntry`, which calls `EnsureRemaining(file_name_length_, "file_name")` and `EnsureRemaining(extra_field_length_, "extra_field")` before advancing `p` [4](#0-3) . `EnsureRemaining` computes `input_file_->Length() - in_offset` and errors out if the requested length exceeds what remains in the mapped file [5](#0-4) . `ProcessCentralDirEntry` has no equivalent guard anywhere in its body, so a JAR whose central directory record declares `file_name_length`, `extra_field_length`, `file_comment_length`, or a Zip64 extra-field `data_size` larger than the remaining bytes of the mmap'd file will cause reads past the end of the mapping.

This is the direct analog of CVE-2019-14438: `xiph_PackHeaders()` trusted attacker-supplied length fields inside an Ogg header without validating them against the actual buffer size, causing a heap over-read; here `ProcessCentralDirEntry` trusts attacker-supplied `u2` length fields inside a ZIP central-directory header without validating them against the mmap'd buffer size.

`InputZipFile` is the core class used by `third_party/ijar/zip.cc`'s `ZipExtractor`, which is used by `ijar` (interface-jar generation, invoked as a Bazel build tool action on every `.jar` input to `java_library`/`java_import` rules) and is also referenced from `src/main/cpp/archive_utils.cc`. The JAR being parsed can originate from an attacker-controlled origin: e.g. a `java_import`/`http_jar`/`http_file` target whose declared `sha256` is satisfied (the byte content is exactly what the attacker published and matches the pinned hash) but whose internal ZIP central-directory structure is malformed in a way that the checksum check cannot detect, since sha256 only validates the raw bytes, not that the internal length fields are self-consistent.

### Impact Explanation
A malformed-but-hash-valid JAR file processed by `ijar` during a normal build (interface jar generation for `java_library`) can cause the `ijar` binary to read past the end of its mmap'd input file. Depending on page/heap layout this is an out-of-bounds read that can crash the build tool (denial of service for that action) or, in the worst case, leak adjacent process memory contents into the derived output (interface jar) that gets embedded into the build graph. This matches "heap-based buffer over-read" impact class (Confidentiality/Integrity/Availability on the local build tool process), analogous to the VLC CVE's over-read impact.

### Likelihood Explanation
Likelihood is High for any build that ingests third-party/external JAR files (a very common pattern: `java_import`, `http_jar`, Maven-resolved dependencies, or JARs unpacked from `http_archive`). An attacker who controls the origin server/mirror serving the JAR (or a compromised registry entry whose sha256 the victim pins) can craft a central directory with over-long `file_name_length`/`extra_field_length`/`file_comment_length` or a malformed Zip64 extra field; because sha256/integrity checks validate only the byte stream, not the internal ZIP structural invariants, the check does not prevent this from reaching `ProcessCentralDirEntry`.

### Recommendation
Add explicit remaining-length validation in `ProcessCentralDirEntry` (mirroring `EnsureRemaining` used in `ProcessLocalFileEntry`) before advancing `p` by `file_name_length`, `extra_field_length`, and `file_comment_length`, and before each `get_u2le`/`get_u8le` read inside the Zip64 extra-field loop, bounding `extra_p` advancement by `data_size` to never exceed the declared `extra_field_length` or the file's total length.

### Proof of Concept
A reproducible test would live alongside `third_party/ijar/` (there is no existing unit test file for `zip.cc` found in this index, so a new one following the pattern of `src/tools/singlejar/zip_headers_test.cc` [6](#0-5)  would be needed): construct a minimal ZIP file whose single central directory record declares `extra_field_length = 0xFFFF` but supplies a central directory / file buffer far shorter than that, or embeds a Zip64 extra field entry whose `data_size` exceeds the actual central-directory buffer bounds, then invoke `InputZipFile::ProcessNext()` / `ProcessCentralDirEntry` on it under ASan and confirm a heap-buffer-overflow (read) report.

Note: I was not able to fully view `InputZipFile::Open()` / `CalculateOutputLength()` (where `central_dir_` and `central_dir_current_` are initialized and where the central directory's own extent is bounded) due to the final-iteration cutoff, so I cannot confirm with certainty whether any outer-level bound on the central directory buffer itself (separate from `EnsureRemaining` calls inside `ProcessCentralDirEntry`) exists that would partially mitigate this. This should be verified before treating the analog as fully proven.

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

**File:** third_party/ijar/zip.cc (L493-542)
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
```

**File:** third_party/ijar/common.h (L43-72)
```text
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

**File:** src/tools/singlejar/zip_headers_test.cc (L1-54)
```text
// Copyright 2016 The Bazel Authors. All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//    http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "src/tools/singlejar/zip_headers.h"

#include <cstdint>

#include "googletest/include/gtest/gtest.h"

namespace {

  const uint8_t kPoison = 0xFB;

  TEST(ZipHeadersTest, LocalHeader) {
    uint8_t bytes[256];
    memset(bytes, kPoison, sizeof(bytes));
    LH* lh = reinterpret_cast<LH*>(bytes);

    // Simple fields.
    lh->signature();
    EXPECT_TRUE(lh->is());
    lh->version(123);
    EXPECT_EQ(123, lh->version());
    lh->bit_flag(0xCAFE);
    EXPECT_EQ(0xCAFE, lh->bit_flag());
    lh->compression_method(8);
    EXPECT_EQ(8, lh->compression_method());
    lh->last_mod_file_time(0xBACD);
    EXPECT_EQ(0xBACD, lh->last_mod_file_time());
    lh->last_mod_file_date(0xCDEF);
    EXPECT_EQ(0xCDEF, lh->last_mod_file_date());
    lh->crc32(0xEF015423);
    EXPECT_EQ(0xEF015423, lh->crc32());
    lh->compressed_file_size32(1234);
    EXPECT_EQ(1234UL, lh->compressed_file_size32());
    EXPECT_EQ(1234UL, lh->compressed_file_size());
    lh->uncompressed_file_size32(3421);
    EXPECT_EQ(3421UL, lh->uncompressed_file_size32());
    EXPECT_EQ(3421UL, lh->uncompressed_file_size());
    lh->file_name("foobar", 6);
    EXPECT_EQ(6UL, lh->file_name_length());
    EXPECT_EQ(0, strncmp("foobar", lh->file_name(), 6));
    EXPECT_EQ("foobar", lh->file_name_string());
```
