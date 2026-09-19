## Finding

### Title
Integer overflow in zip64 central-directory bounds check enables out-of-bounds pointer/read from an attacker-supplied archive - (File: `third_party/ijar/zip.cc`)

### Summary
`FindZipCentralDirectory()` in `third_party/ijar/zip.cc` validates the central directory location of a (possibly zip64) archive with an unchecked 64-bit addition, `cd.central_dir_offset + cd.central_dir_size > in_length`. When the zip64 End-Of-Central-Directory record is used, both operands are attacker-controlled 64-bit values read directly from archive bytes via `get_u8le()`, so the sum can wrap around `UINT64_MAX` and pass the bounds check even though the values are nonsensical, leading `central_dir` to be computed as a pointer far outside the mapped archive buffer.

### Finding Description
`ijar` (the interface-jar stripping tool bundled with Bazel) parses untrusted `.jar`/`.zip` files. `FindZipCentralDirectory` locates the End-Of-Central-Directory (EOCD) record and, for zip64 archives, calls `FindZip64CentralDirectory` → `MaybeReadZip64CentralDirectory`, which fills an `EndOfCentralDirectoryRecord` struct with `u8` (64-bit) fields read straight from the file with `get_u8le`: [1](#0-0) 

These fully-attacker-controlled 64-bit values are stored unchecked into the struct: [2](#0-1) 

Back in `FindZipCentralDirectory`, the only sanity check before trusting these fields is: [3](#0-2) 

`cd.central_dir_offset` and `cd.central_dir_size` are both `u8` (`unsigned long long`, `third_party/ijar/common.h:32`). If an attacker crafts a zip64 EOCD with, e.g., `central_dir_offset = 0xFFFFFFFFFFFFFFF0` and `central_dir_size = 0x20`, the sum wraps to `0x10`, which is trivially `<= in_length`, so the "central directory offset/size is invalid" check is bypassed. The code then computes:

```
*central_dir = end_of_central_dir - cd.central_dir_size;
```

With `central_dir_size` near `UINT64_MAX`, this pointer arithmetic drives `central_dir` far outside the bounds of the memory-mapped/loaded archive buffer, and the caller (`InputZipFile::ProcessCentralDirEntry` and friends) subsequently dereferences it — an out-of-bounds read on attacker-controlled offsets.

This is directly analogous to the referenced Move `move-binary-format/src/check_bounds.rs` hardening (#505), which added overflow checks around index/offset arithmetic used to validate bytecode table bounds before trusting them; here the equivalent bounds-validating addition in `ijar`'s zip64 EOCD parser lacks the same protection.

### Impact Explanation
A malicious archive (e.g., served via an `http_jar`/`http_archive`/`maven_jar`-style dependency, or checked into an untrusted branch that CI builds and runs through `ijar` to produce interface jars) can trigger out-of-bounds reads in the `ijar` process, potentially causing crashes (denial of the build) or leaking adjacent process memory into the emitted interface jar / error output. The bounds check that is supposed to gate "central directory offset/size is invalid" is defeated by the wraparound, so the containment invariant that `central_dir` stays inside `[bytes, bytes+in_length)` is broken.

### Likelihood Explanation
This requires the attacker to control a downloaded/fetched jar/zip that is subsequently processed by `ijar` with a crafted zip64 EOCD record — no additional privileges, credentials, or MITM are needed, only that the victim's build ingests the malicious archive. Since `ijar` unconditionally parses the zip64 EOCD path whenever the locator signature matches, reaching the vulnerable addition only requires producing a well-formed zip64 locator/EOCD with two large 64-bit fields.

### Recommendation
Perform the bounds check using overflow-safe arithmetic, e.g. reject the archive if `central_dir_offset > in_length || central_dir_size > in_length - central_dir_offset`, and additionally validate that `central_dir_offset` and `central_dir_size` individually fit within `in_length` before computing `end_of_central_dir - cd.central_dir_size`. This mirrors the "Minor hardening against arithmetic overflow" pattern applied to `check_bounds.rs` in Move, where offset/size sums used for bounds validation were changed to checked/overflow-safe arithmetic.

### Proof of Concept
Extend `third_party/ijar/test/zip_test.sh` (or a new `zip_test.cc` unit test) with a synthetic zip64 archive whose zip64 EOCD record sets:
- `central_dir_size = 0xFFFFFFFFFFFFFFF0`
- `central_dir_offset = 0x20`

so that `central_dir_offset + central_dir_size` wraps to a value `<= in_length`, then invoke `zipper x` (or directly call `FindZipCentralDirectory`) on this archive built with ASan/UBSan; the process should be observed reading out of the mapped buffer (ASan heap-buffer-overflow / SEGV) instead of failing with "central directory offset/size is invalid".

### Citations

**File:** third_party/ijar/zip.cc (L615-620)
```text
  u4 number_of_this_disk = get_u4be(current);
  u4 disk_with_central_dir = get_u4le(current);
  u8 central_dir_entries_on_this_disk = get_u8le(current);
  u8 central_dir_entries = get_u8le(current);
  u8 central_dir_size = get_u8le(current);
  u8 central_dir_offset = get_u8le(current);
```

**File:** third_party/ijar/zip.cc (L649-656)
```text
  *end_of_central_dir = candidate;
  cd->number_of_this_disk = number_of_this_disk;
  cd->disk_with_central_dir = disk_with_central_dir;
  cd->central_dir_entries_on_this_disk = central_dir_entries_on_this_disk;
  cd->central_dir_entries = central_dir_entries;
  cd->central_dir_size = central_dir_size;
  cd->central_dir_offset = central_dir_offset;
  return true;
```

**File:** third_party/ijar/zip.cc (L760-777)
```text
  if (cd.number_of_this_disk != 0 || cd.disk_with_central_dir != 0 ||
      cd.central_dir_entries_on_this_disk != cd.central_dir_entries) {
    fprintf(stderr, "multi-disk JAR files are not supported\n");
    return false;
  }

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
