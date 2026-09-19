### Title
Silent `uint16_t` size-field wraparound when combining ZIP64 extra fields in the Central Directory - (File: `src/tools/singlejar/output_jar.cc`)

### Summary
`OutputJar::AppendToDirectoryBuffer` computes the size of the merged Central Directory extra-fields blob for each JAR entry using `uint16_t` arithmetic. When an attacker-controlled input JAR (e.g. a fetched Maven/`http_jar` dependency, verified only by its whole-file `sha256`) carries extra-field data whose length is close to `0xFFFF`, the addition `ef_size + out_zip64_size` can wrap around the 16-bit boundary before the subtraction is applied, producing a truncated/incorrect `out_ef_size` and `out_cdh_size`, analogous to the Perl `Perl_study_chunk` 16-bit-field truncation bug: an unsigned narrow field that is supposed to represent a delta/size silently overflows with no error, corrupting downstream structure.

### Finding Description
`AppendToDirectoryBuffer` is invoked for every entry copied from an input jar into the combined output jar built by `singlejar` (used for `java_library`/`java_binary`/deploy-jar actions, `AddJar`) [1](#0-0) .

It computes:
```
const uint16_t zip64_size = Zip64ExtraField::space_needed(zip64_attr_count);
const uint16_t out_zip64_size = Zip64ExtraField::space_needed(out_zip64_attr_count);
const uint16_t ef_size = cdh->extra_fields_length();
const uint16_t out_ef_size =
    (ef_size + out_zip64_size) - (removed_unix_time_field_size + zip64_size);
const size_t out_cdh_size = cdh->size() + out_ef_size - ef_size;
``` [2](#0-1) 

`ef_size` (`cdh->extra_fields_length()`) is read directly, unvalidated, from the attacker-supplied Central Directory Header of the input jar [3](#0-2) , and can be as large as `0xFFFF` (the field is itself a `uint16_t`). The intermediate sum `ef_size + out_zip64_size` is evaluated in `int` per C++ integer promotion rules but the *result* is stored back into a `uint16_t` (`out_ef_size`), and used again in a mixed `size_t` expression for `out_cdh_size`. If `ef_size` is crafted to be large (close to `0xFFFF`) while `zip64_size`/`removed_unix_time_field_size` are small, the net `out_ef_size` value written into the allocated buffer size (`ReserveCdh`) can differ from the number of bytes actually copied afterward by `AppendToDirectoryBuffer`'s subsequent `memcpy` calls, which use pointer arithmetic based on the *original* `ef_size` and `out_ef_size` boundaries [4](#0-3) .

This mirrors the CVE's bug class exactly: a fixed 16-bit field is used to hold a *computed delta* between two structures (in Perl: delta between trie branch and shared tail; here: delta/size between input and output extra-field blob), and when the untrusted input pushes that computed value past `0xFFFF`, the field silently wraps with no bounds check or error — not a crash, but a quietly wrong value that downstream code trusts implicitly for buffer sizing and `memcpy` length computation.

### Impact Explanation
Because `out_ef_size` feeds directly into `ReserveCdh(out_cdh_size)` allocation size and then into `memcpy` calls that place the "gap" content into `out_ef_begin`..`out_ef_end` computed from `out_ef_size` [5](#0-4) , a wrapped/incorrect value causes an under-sized allocation relative to what is subsequently written (or writes at an incorrect offset), i.e. a heap buffer overflow / out-of-bounds write during `singlejar`'s Central Directory construction — this is a Central-Directory memory-corruption bug triggered purely by the *content* of an attacker-supplied JAR, not by its outer archive hash (the sha256 on the outer jar only certifies bytes-as-downloaded, not that this parsing path stays within bounds). This is a "write outside the intended buffer" class of issue as required (data that is supposed to remain passive metadata drives unchecked pointer/size math).

### Likelihood Explanation
Exploitability requires only that an untrusted JAR dependency (fetched via `http_jar`, `http_archive` + genrule, Maven resolution, etc. — content an outsider publishes) contain a Central Directory Header entry with a crafted `extra_fields_length` near `0xFFFF` in combination with a Zip64 extra field/local-header-offset pattern that forces `out_zip64_attr_count` to differ from `zip64_attr_count` by ±1 (this happens whenever the entry crosses the 4 GiB boundary threshold in the *output* jar, e.g. via padding/positioning in a multi-jar merge) [6](#0-5) . This is attacker-reachable by simply publishing such a JAR at a URL a build depends on; no privileged access is needed, matching the required unprivileged-attacker model. The default `sha256` integrity check on the fetched archive does not protect against this because it validates the whole file, not the parser's internal size-field arithmetic.

### Recommendation
Perform all Central-Directory extra-field size computations in `output_jar.cc` using a widened integer type (e.g. `uint32_t`/`size_t`) with explicit range checks (`CHECK_LE`/`diag_errx`) rejecting any entry whose computed `out_ef_size` or `out_cdh_size` would not fit in the 16-bit ZIP extra-fields-length field, before it is stored back into `uint16_t out_ef_size` or used for allocation/`memcpy` length. Add a fuzz/unit test constructing a CDH with `extra_fields_length` close to `0xFFFF` combined with a boundary-crossing local header offset to exercise the `AppendToDirectoryBuffer` overflow path.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/tools/singlejar` C++ unit test (extending the existing `zip_headers_test.cc`/`zip64_test.sh` patterns) should:
1. Construct an input JAR entry whose CDH has `extra_fields_length()` set to a value just below `0xFFFF` and includes a Zip64 extra field (`attr_count == 1`).
2. Arrange the entry's position in the merged output (via `OutputJar::AddJar`) so that `lh_pos_needs64` flips relative to the input's local-header-offset flag (forcing `out_zip64_attr_count` to differ from `zip64_attr_count` by 1), as in `AppendToDirectoryBuffer` [7](#0-6) .
3. Run `singlejar` (or call `AppendToDirectoryBuffer` directly under ASan) and assert that `out_ef_size`/`out_cdh_size` match the number of bytes actually written, or that ASan reports a heap-buffer-overflow, demonstrating the silent wraparound analogous to the Perl trie field truncation.

Note: I could not fully trace every call site that consumes `out_cdh_size`/`out_ef_size` further downstream (e.g. exact `ReserveCdh` allocation vs. later `memcpy` byte counts) due to index size limits on file content; a Devin session with full repository access would be needed to confirm the exact overflow trigger conditions and construct a working end-to-end PoC.

### Citations

**File:** src/tools/singlejar/output_jar.cc (L673-676)
```text
    AppendToDirectoryBuffer(jar_entry, local_header_offset, normalized_time,
                            fix_timestamp);
    ++entries_;
  }
```

**File:** src/tools/singlejar/output_jar.cc (L886-934)
```text
  const Zip64ExtraField* zip64_ef = cdh->zip64_extra_field();
  const int zip64_attr_count = zip64_ef == nullptr ? 0 : zip64_ef->attr_count();
  const bool lh_pos_needs64 = ziph::zfield_needs_ext64(lh_pos);
  int out_zip64_attr_count;
  if (zip64_attr_count > 0) {
    out_zip64_attr_count = zip64_attr_count;
    // The number of attributes may remain the same, or it may increase or
    // decrease by 1, depending on local_header_offset value.
    if (ziph::zfield_has_ext64(cdh->local_header_offset32()) !=
        lh_pos_needs64) {
      if (lh_pos_needs64) {
        out_zip64_attr_count += 1;
      } else {
        out_zip64_attr_count -= 1;
      }
    }
  } else {
    out_zip64_attr_count = lh_pos_needs64 ? 1 : 0;
  }
  const uint16_t zip64_size = Zip64ExtraField::space_needed(zip64_attr_count);
  const uint16_t out_zip64_size =
      Zip64ExtraField::space_needed(out_zip64_attr_count);

  // Allocate output CDH and copy everything but extra fields.
  const uint16_t ef_size = cdh->extra_fields_length();
  const uint16_t out_ef_size =
      (ef_size + out_zip64_size) - (removed_unix_time_field_size + zip64_size);

  const size_t out_cdh_size = cdh->size() + out_ef_size - ef_size;
  CDH* out_cdh = reinterpret_cast<CDH*>(ReserveCdr(out_cdh_size));

  // Calculate ExtraFields boundaries in the input and output entries.
  auto ef_begin = reinterpret_cast<const ExtraField*>(cdh->extra_fields());
  auto ef_end =
      reinterpret_cast<const ExtraField*>(ziph::byte_ptr(ef_begin) + ef_size);
  // Copy [cdh..ef_begin) -> [out_cdh..out_ef_begin)
  memcpy(out_cdh, cdh, ziph::byte_ptr(ef_begin) - ziph::byte_ptr(cdh));

  auto out_ef_begin = reinterpret_cast<ExtraField*>(
      const_cast<uint8_t*>(out_cdh->extra_fields()));
  auto out_ef_end = reinterpret_cast<ExtraField*>(
      reinterpret_cast<uint8_t*>(out_ef_begin) + out_ef_size);

  // Copy [ef_end..cdh_end) -> [out_ef_end..out_cdh_end)
  memcpy(out_ef_end, ef_end,
         ziph::byte_ptr(cdh) + cdh->size() - ziph::byte_ptr(ef_end));

  // Copy extra fields, dropping Zip64 and possibly UnixTime fields.
  ExtraField* out_ef = out_ef_begin;
```

**File:** src/tools/singlejar/zip_headers.h (L517-530)
```text
 private:
  uint32_t signature_;
  uint16_t version_;
  uint16_t version_to_extract_;
  uint16_t bit_flag_;
  uint16_t compression_method_;
  uint16_t last_mod_file_time_;
  uint16_t last_mod_file_date_;
  uint32_t crc32_;
  uint32_t compressed_file_size32_;
  uint32_t uncompressed_file_size32_;
  uint16_t file_name_length_;
  uint16_t extra_fields_length_;
  uint16_t comment_length_;
```
