Based on my investigation, I found a genuine analog in `third_party/ijar/zip.cc`.

### Title
Unchecked 64-bit integer addition in ZIP64 central directory bounds check allows out-of-bounds pointer computation - (File: `third_party/ijar/zip.cc`)

### Summary
`FindZipCentralDirectory` in ijar's ZIP parser validates the central directory location using `cd.central_dir_offset + cd.central_dir_size > in_length` [1](#0-0) . Both fields are `u8` (`uint64_t`) and, in the zip64 code path, are populated directly from attacker-controlled 8-byte little-endian values read via `get_u8le` with no independent range check [2](#0-1) . Like the Solidity report's unchecked `unsafeAdd`, this is an addition performed without an overflow-safe idiom (e.g. checking `a > SIZE_MAX - b` first), so two large attacker-chosen 64-bit values can wrap the sum around to a small number that passes the `> in_length` bounds check.

### Finding Description
`EndOfCentralDirectoryRecord` defines `central_dir_size` and `central_dir_offset` as 64-bit fields [3](#0-2) . When a zip64 EOCD record is present, `MaybeReadZip64CentralDirectory` reads these two fields straight from file bytes with `get_u8le`, and writes them into `cd` if they are internally self-consistent with the 32-bit EOCD stand-ins — but there is no check that `central_dir_offset` or `central_dir_size` is itself within the file bounds at this point [4](#0-3) . The only bounds check performed afterward is the unchecked-overflow addition:
```
if (cd.central_dir_offset + cd.central_dir_size > in_length) {
  ... "central directory offset/size is invalid" ...
}
*central_dir = end_of_central_dir - cd.central_dir_size;
``` [5](#0-4) 
If an attacker crafts `central_dir_offset` and `central_dir_size` such that their sum overflows `uint64_t` (e.g. `central_dir_offset = 2^64-1`, `central_dir_size = large`), the sum wraps to a value ≤ `in_length`, so the check is bypassed. The subsequent `end_of_central_dir - cd.central_dir_size` pointer arithmetic then computes `central_dir` far outside the mapped file buffer, and callers like `InputZipFile::CalculateOutputLength`/`ProcessCentralDirEntry` dereference that pointer to parse "central directory entries," reading (and, via computed offsets, potentially writing through subsequent decompression logic) out-of-bounds memory relative to the memory-mapped input file. This mirrors the reported bug class precisely: an unchecked arithmetic operation is used as a load-bearing security boundary check, and a crafted value overflow defeats it.

### Impact Explanation
ijar's zip parser (`ZipExtractor`/`InputZipFile`) is exercised whenever Bazel processes a jar-format archive: interface-jar generation for `java_library`/`java_import` targets, and it is reused by Bazel's own client-side self-extraction and archive utilities (`archive_utils.cc` uses `devtools_ijar::ZipExtractor`). An attacker who controls the bytes of a `.jar`/`.zip` consumed during a build (e.g. published as a dependency artifact fetched via `http_archive`/Maven or committed to a branch a CI job builds) can supply a crafted zip64 EOCD record that triggers this overflow. The result is an out-of-bounds pointer used to walk "central directory entries," which is a memory-safety violation (OOB read, and via `ProcessLocalFileEntry`/decompression, potential crash or memory corruption) in the ijar tool process that a normal build spawns. This is a genuine violation of the "containment holds" invariant for parsing untrusted archive content, matching the report's core claim that unchecked arithmetic silently defeats a validation check.

### Likelihood Explanation
The zip64 code path is reached only for large ("huge", >4GB-class) entries or when a zip64 EOCD locator is deliberately present, which an attacker fully controls when crafting the archive bytes; no privileged access is required, only that the crafted file be consumed as an input archive by ijar or a caller like `archive_utils.cc`. The vulnerable check is unconditionally on the parse path for any zip64 archive and is not gated by `sha256`/checksum verification, since checksum verification only confirms the file wasn't tampered with in transit — it does not prevent the file's own internal fields (author-controlled at creation time) from containing overflow-inducing values. Existing `EnsureRemaining` bounds checks exist for other parts of the parser but are not applied to this specific offset+size addition.

### Recommendation
Replace the addition with an overflow-safe comparison, e.g.:
```cpp
if (cd.central_dir_size > in_length ||
    cd.central_dir_offset > in_length - cd.central_dir_size) {
  fprintf(stderr, "central directory offset/size is invalid\n");
  return false;
}
```
Additionally validate `central_dir_offset` and `central_dir_size` independently against `in_length` immediately when they are parsed out of the zip64 EOCD record in `MaybeReadZip64CentralDirectory`, before they are trusted for any further pointer arithmetic.

### Proof of Concept
A `src/test/cpp` or `third_party/ijar` unit test (extending `zip_headers_test.cc`/`input_jar_scan_entries_test.h` style) can construct an in-memory buffer containing:
1. A minimal local file header/data.
2. A zip64 EOCD record (`ZIP64_EOCD_SIGNATURE`) with `central_dir_size = 0x10` and `central_dir_offset = 0xFFFFFFFFFFFFFFF0` (i.e., `UINT64_MAX - 15`), so `central_dir_offset + central_dir_size` wraps to `0` in 64-bit arithmetic.
3. A standard `EOCD_SIGNATURE` record with the 32-bit stand-in fields set to `0xFFFFFFFF` to force the zip64 path.

Then call `devtools_ijar::FindZipCentralDirectory(bytes, in_length, &offset, &central_dir)` directly and assert that it incorrectly returns `true` (bypassing the "central directory offset/size is invalid" check) with `central_dir` pointing outside `[bytes, bytes+in_length)`, demonstrating the overflow bypass of the intended bounds check at [1](#0-0) .

### Citations

**File:** third_party/ijar/zip.cc (L584-591)
```text
struct EndOfCentralDirectoryRecord {
  u4 number_of_this_disk;
  u4 disk_with_central_dir;
  u8 central_dir_entries_on_this_disk;
  u8 central_dir_entries;
  u8 central_dir_size;
  u8 central_dir_offset;
};
```

**File:** third_party/ijar/zip.cc (L611-656)
```text
  get_u8le(current);
  get_u2be(current);  // version made by
  get_u2be(current);  // version needed to extract

  u4 number_of_this_disk = get_u4be(current);
  u4 disk_with_central_dir = get_u4le(current);
  u8 central_dir_entries_on_this_disk = get_u8le(current);
  u8 central_dir_entries = get_u8le(current);
  u8 central_dir_size = get_u8le(current);
  u8 central_dir_offset = get_u8le(current);

  // check for a zip64 EOCD that matches the regular EOCD
  if (number_of_this_disk != cd->number_of_this_disk &&
      cd->number_of_this_disk != U2_MAX) {
    return false;
  }
  if (disk_with_central_dir != cd->disk_with_central_dir &&
      cd->disk_with_central_dir != U2_MAX) {
    return false;
  }
  if (central_dir_entries_on_this_disk !=
          cd->central_dir_entries_on_this_disk &&
      cd->central_dir_entries_on_this_disk != U2_MAX) {
    return false;
  }
  if (central_dir_entries != cd->central_dir_entries &&
      cd->central_dir_entries != U2_MAX) {
    return false;
  }
  if (central_dir_size != cd->central_dir_size &&
      cd->central_dir_size != U4_MAX) {
    return false;
  }
  if (central_dir_offset != cd->central_dir_offset &&
      cd->central_dir_offset != U4_MAX) {
    return false;
  }

  *end_of_central_dir = candidate;
  cd->number_of_this_disk = number_of_this_disk;
  cd->disk_with_central_dir = disk_with_central_dir;
  cd->central_dir_entries_on_this_disk = central_dir_entries_on_this_disk;
  cd->central_dir_entries = central_dir_entries;
  cd->central_dir_size = central_dir_size;
  cd->central_dir_offset = central_dir_offset;
  return true;
```

**File:** third_party/ijar/zip.cc (L766-777)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }

  // Do not change output values before determining that they are OK.
  *offset = cd.central_dir_offset;
  // Central directory start can then be used to determine the actual
  // starts of the zip file (which can be different in case of a non-zip
  // header like for auto-extractable binaries).
  *central_dir = end_of_central_dir - cd.central_dir_size;
  return true;
```
