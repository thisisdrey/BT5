### Title
Integer overflow in `SemVer` numeric parsing/comparison causes incorrect `.bazelrc` version-gated directive evaluation - (File: `src/main/cpp/sem_ver.cc`)

### Summary
`SemVer::Compare` and `SemVer::ComparePrerelease` in `src/main/cpp/sem_ver.cc` compute version ordering using plain `int` subtraction and `int`-typed component fields, mirroring the `boostPrice` bug class: an arithmetic operation on attacker-influenced numeric input can silently produce a wrapped/incorrect result that is then used in a security- or trust-relevant boolean decision (analogous to the `newBoostPrice > boostUpperPriceBuy` check in the report). [1](#0-0) 

### Finding Description
`SemVer::Parse` extracts `major`, `minor`, `patch` as plain `int` via `RE2::FullMatch`, from a regex that permits arbitrarily long digit sequences (`0|[1-9]\d*`), with no bounds/overflow checking: [2](#0-1) 

`SemVer::Compare` then determines ordering using signed integer subtraction of these fields:

```cpp
auto major_diff = major_ - other.major_;
if (major_diff != 0) {
  return major_diff;
}
``` [1](#0-0) 

If a version string contains a numeric component large enough to overflow `int` (e.g. a many-digit run parsed by RE2 into an `int` field, or subtraction of two large positive `int`s producing a signed overflow), the sign of `major_diff`/`minor_diff`/`patch_diff` can flip, causing `Compare()` to report the wrong ordering (e.g., a version that should compare "greater" is reported "less than," or vice versa). This is structurally identical to the reported bug: a division/subtraction that loses precision/overflows in a way that flips the sign of a comparison result used downstream for a security-relevant gate.

Separately, in `ComparePrerelease`, numeric identifier parts of the prerelease string are parsed with `absl::SimpleAtoi(part_x, &ix)` into `int`, and then compared as `ix < iy`; extremely large numeric prerelease identifiers can behave inconsistently with `SimpleAtoi`'s overflow-rejection semantics (it returns `false` on overflow, causing the numeric part to be treated as non-numeric, and per the code "numeric identifiers always have lower precedence than non-numeric," inverting the intended precedence for one operand but not necessarily the other if only one side overflows). [3](#0-2) 

`SemVer` is exercised from `src/main/cpp/rc_file.cc`, which is responsible for interpreting `.bazelrc` directives — including version-gated conditional imports/config (min/max Bazel version checks) — as part of Bazel's own startup option processing.

### Impact Explanation
If an attacker can supply a `.bazelrc` (or similar rc directive) with an adversarially crafted version string — for example on an untrusted branch that a victim's CI checks out and builds with a trusted Bazel binary — the version comparison used to gate which directives are applied could be flipped by the overflow, causing rc-file logic intended for "future/incompatible Bazel versions only" or "old Bazel versions only" to be incorrectly applied to the actual running Bazel version. This can lead to unexpected startup-option behavior being silently enabled/disabled contrary to the file's stated intent, an integrity-of-decision failure analogous to the price-check bypass in the original report (a check that should have failed/succeeded reports the opposite due to arithmetic wraparound rather than a genuine logic error).

### Likelihood Explanation
The likelihood is constrained: it requires (a) a `.bazelrc`-like input under attacker control (e.g., unprivileged PR/branch content built by CI with default flags) and (b) a version literal engineered to overflow a 32-bit `int` in exactly the digit run matched by the semver regex, which is a narrow but concretely constructible input (regex `[1-9]\d*` permits unlimited-length digit strings). No existing bounds check in `Parse`, `Compare`, or `ComparePrerelease` prevents this before the arithmetic is performed.

### Recommendation
- Reject version components in `SemVer::Parse` that exceed a sane numeric bound (e.g. via `absl::SimpleAtoi` with explicit range checks) instead of relying on RE2's implicit `int` capture, which can silently truncate/overflow.
- Use unsigned or widened (`int64_t`) arithmetic for `major_diff`/`minor_diff`/`patch_diff` in `SemVer::Compare`, or use direct relational operators (`<`, `>`) instead of subtraction to avoid overflow-driven sign inversion.
- Apply the same overflow-safe comparison in `ComparePrerelease` for numeric identifiers.

### Proof of Concept
A JUnit/`BuildIntegrationTestCase`-style reproduction is not directly applicable since `SemVer` is C++; the equivalent would be a `sem_ver_test.cc`-based unit test (following the existing pattern in `src/test/cpp/sem_ver_test.cc`) asserting:
```cpp
auto huge = SemVer::Parse("99999999999999999999.0.0");  // overflow in RE2 int capture
auto normal = SemVer::Parse("9.0.0");
// Expect huge > normal, but overflow can make Compare() report huge < normal.
EXPECT_TRUE(*huge > *normal);
```
I was unable to execute this against the actual `RE2::FullMatch` int-capture overflow behavior or the `rc_file.cc` version-gating call sites within this session to confirm the exact overflow trigger point (RE2 numeric capture parsing behavior on overflow vs. the subtraction in `Compare`), so this should be verified with a concrete build/test run before treating it as fully confirmed.

### Citations

**File:** src/main/cpp/sem_ver.cc (L29-45)
```text
// Semantic version regex copied verbatim from
// https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
const LazyRE2 kSemverRe = {R"(^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$)"};
}  // namespace

std::optional<SemVer> SemVer::Parse(const std::string& v) {
  int major;
  int minor;
  int patch;
  std::string prerelease;
  std::string buildmetadata;
  if (RE2::FullMatch(v, *kSemverRe, &major, &minor, &patch, &prerelease,
                     &buildmetadata)) {
    return SemVer(major, minor, patch, prerelease, buildmetadata);
  }
  return std::nullopt;
}
```

**File:** src/main/cpp/sem_ver.cc (L55-69)
```text
int SemVer::Compare(const SemVer& other) const {
  auto major_diff = major_ - other.major_;
  if (major_diff != 0) {
    return major_diff;
  }
  auto minor_diff = minor_ - other.minor_;
  if (minor_diff != 0) {
    return minor_diff;
  }
  auto patch_diff = patch_ - other.patch_;
  if (patch_diff != 0) {
    return patch_diff;
  }
  return ComparePrerelease(prerelease_, other.prerelease_);
}
```

**File:** src/main/cpp/sem_ver.cc (L95-122)
```text
    if (part_x != part_y) {
      int ix;
      int iy;
      bool is_numeric_x = absl::SimpleAtoi(part_x, &ix);
      bool is_numeric_y = absl::SimpleAtoi(part_y, &iy);

      if (is_numeric_x != is_numeric_y) {
        // Numeric identifiers always have lower precedence than non-numeric
        // identifiers.
        if (is_numeric_x) {
          return -1;
        }
        return 1;
      }

      if (is_numeric_x) {  // Both parts are numbers.
        if (ix < iy) {
          return -1;
        }
        return 1;
      }

      // Lexicographic ordering.
      if (part_x < part_y) {
        return -1;
      }
      return 1;
    }
```
