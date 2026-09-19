### Title
Integer overflow in ZIP64 central directory bounds check leads to out-of-bounds pointer arithmetic - (File: third_party/ijar/zip.cc)

### Summary
This is the closest Bazel analog to the reported Solidity issue: an unchecked arithmetic operation on attacker-controlled 64-bit values that is meant to serve as a safety bound check, but which can wrap around and silently pass, exactly like the unchecked `_sqrt(totalCredits * 1e18)` overflow in the audit report. In Bazel this occurs in the C++ ZIP parser (`third_party/ijar/zip.cc`), which is compiled into the `zipper`/`unzipper` and `ijar` tools used by Bazel's build actions (e.g., `singlejar`, jar/AAR resource extraction, interface-jar generation) to parse ZIP/JAR files, including files that originate from externally fetched dependencies.

### Finding Description
`FindZip64CentralDirectory` reads the ZIP64 End-Of-Central-Directory record fields directly from file bytes as unchecked 64-bit values: [1](#0-0) 

These attacker-controlled `central_dir_size` / `central_dir_offset` values are copied into the `EndOfCentralDirectoryRecord` struct with no range validation, then used later in `FindZipCentralDirectory`'s sanity check: [2](#0-1) 

`cd.central_dir_offset + cd.central_dir_size` is an addition of two full-range `u8` (`uint64_t`) values that are entirely attacker-controlled (read straight from file bytes via `get_u8le`). If both values are large (e.g. each close to `UINT64_MAX/2` or specifically chosen so the sum wraps modulo 2^64), the addition overflows and wraps back under `in_length`, so the bounds check `> in_length` is bypassed even though the "real" (mathematically correct) sum vastly exceeds the mapped file size.

Once the check is bypassed, the code computes: [3](#0-2) 

`*central_dir = end_of_central_dir - cd.central_dir_size;` performs pointer arithmetic with an attacker-controlled 64-bit subtrahend that was never validated against the actual mapped region, producing a `central_dir` pointer far outside the `mmap`'d input file. Subsequent parsing (`ProcessCentralDirEntry`, `CalculateOutputLength`, `InputJar::LocateCentralDirectory`-style consumers) will dereference this out-of-bounds pointer, corresponding to lines like: [4](#0-3) 

### Impact Explanation
This breaks the invariant that "untrusted content stays data" — the ZIP central directory offset/size fields are treated as trusted integers by the bounds check, but the unchecked 64-bit addition lets a hostile ZIP/JAR (e.g., a jar consumed via `java_import`, `http_jar`, or a maven/AAR dependency processed by `ijar`/`singlejar`) defeat the very sanity check meant to keep parsing inside the mapped file. The result is an out-of-bounds read (and pointer used for further arithmetic/reads), which in the best case crashes the Bazel action worker (denial of the build) and in the worst case reads adjacent process memory, i.e., a genuine memory-safety violation reachable purely from file bytes a hostile origin (or mirror without integrity re-verification of internal structure) can control.

### Likelihood Explanation
Reaching this path requires only that Bazel processes a maliciously crafted ZIP/JAR file with a ZIP64 End-Of-Central-Directory record whose `central_dir_size`/`central_dir_offset` are chosen to overflow the 64-bit sum — a file format detail entirely under attacker control and independent of any sha256/lockfile check on the archive's bytes (a checksum only guarantees byte-for-byte integrity, not that internal offsets are semantically valid). Any build that consumes an externally-supplied jar/zip through `ijar`, `zipper`/`unzipper`, or `singlejar` is exposed.

### Recommendation
Validate `central_dir_size` and `central_dir_offset` against `in_length` individually before adding them (e.g., reject if either exceeds `in_length`), and perform the bounds check using overflow-safe arithmetic (e.g., check `central_dir_offset > in_length - central_dir_size` only after confirming `central_dir_size <= in_length`, or use a widened/saturating-arithmetic comparison) before deriving `central_dir` via pointer subtraction, mirroring how `StarlarkInt.multiply`/`add` in `src/main/java/net/starlark/java/eval/StarlarkInt.java` explicitly detect overflow rather than trusting the wrapped result. [5](#0-4) 

### Proof of Concept
A JUnit/shell reproduction would construct a minimal ZIP64 archive whose ZIP64 EOCD record encodes `central_dir_size` and `central_dir_offset` as large 64-bit values summing to a wrapped value ≤ `in_length` (e.g., `central_dir_size = 2^63`, `central_dir_offset = 2^63 + small_offset`, so the 64-bit sum wraps to a small number), feed it to `FindZipCentralDirectory`/`InputZipFile::Open` (as exercised by `third_party/ijar/zip_main.cc`'s `extract`/list path or the existing `output_jar_simple_test.cc` malformed-zip harness pattern), and assert that parsing proceeds past the bounds check and dereferences memory outside the mapped file (observable via ASan/valgrind heap/segv, or via `output_jar_simple_test.cc`'s `CreateZipWithMalformedExtraField`-style harness extended to the ZIP64 EOCD path): [6](#0-5)

### Citations

**File:** third_party/ijar/zip.cc (L302-311)
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

```

**File:** third_party/ijar/zip.cc (L615-620)
```text
  u4 number_of_this_disk = get_u4be(current);
  u4 disk_with_central_dir = get_u4le(current);
  u8 central_dir_entries_on_this_disk = get_u8le(current);
  u8 central_dir_entries = get_u8le(current);
  u8 central_dir_size = get_u8le(current);
  u8 central_dir_offset = get_u8le(current);
```

**File:** third_party/ijar/zip.cc (L760-769)
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
```

**File:** third_party/ijar/zip.cc (L770-777)
```text

  // Do not change output values before determining that they are OK.
  *offset = cd.central_dir_offset;
  // Central directory start can then be used to determine the actual
  // starts of the zip file (which can be different in case of a non-zip
  // header like for auto-extractable binaries).
  *central_dir = end_of_central_dir - cd.central_dir_size;
  return true;
```

**File:** src/main/java/net/starlark/java/eval/StarlarkInt.java (L521-556)
```java
  /** Returns x * y. */
  public static StarlarkInt multiply(StarlarkInt x, StarlarkInt y) {
    // Fast path for common case: int32 * int32.
    if (x instanceof Int32 && y instanceof Int32) {
      long xl = ((Int32) x).v;
      long yl = ((Int32) y).v;
      return StarlarkInt.of(xl * yl);
    }

    try {
      long xl = x.toLongFast();
      long yl = y.toLongFast();

      // Signed int128 multiplication, using Hacker's Delight 8-2
      // (High-Order Half of 64-Bit Product) extended to 128 bits.
      // TODO(adonovan): use Math.multiplyHigh when Java 9 becomes available.
      long xlo = xl & 0xFFFFFFFFL;
      long xhi = xl >> 32;
      long ylo = yl & 0xFFFFFFFFL;
      long yhi = yl >> 32;
      long zlo = xlo * ylo;
      long t = xhi * ylo + (zlo >>> 32);
      long z1 = t & 0xFFFFFFFFL;
      long z2 = t >> 32;
      z1 += xlo * yhi;

      // high and low arms of result
      long z128hi = xhi * yhi + z2 + (z1 >> 32);
      long z128lo = xl * yl;

      // Check int128 result is within int64 range.
      if (z128hi == (z128lo & Long.MIN_VALUE) >> 63) {
        return StarlarkInt.of(z128lo);
      }

      /* overflow */
```
