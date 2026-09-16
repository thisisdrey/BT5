### Title
Out-of-bounds heap read in `VerifyTransferProof.execute` via attacker-controlled `spendOffset`/`spendAuthSigOffset`/`receiveOffset` - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`VerifyTransferProof.execute` only validates that the total `data.length` matches one of a fixed set of sizes, but then parses three "unfixed field offsets" (`spendOffset`, `spendAuthSigOffset`, `receiveOffset`) directly from attacker-supplied `data` and uses them, unvalidated, as base offsets for a series of `System.arraycopy` calls that read 32–192-byte chunks out of `data`. This mirrors the CVE-2015-8934 bug class (`copy_from_lzss_window` trusting an attacker-controlled window/offset value to index into a buffer without bounds checking), producing an out-of-bounds heap read reachable from a single TVM contract call.

### Finding Description
In `execute(byte[] data)`: [1](#0-0) 

the only length validation performed is: [2](#0-1) 

against the fixed `SIZE` set `{2080, 2368, 2464, 2752}`. After that, `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` are read directly as 32-bit integers from attacker-controlled bytes at fixed positions 0, 32, 64: [3](#0-2) 

These three values are never checked against `data.length` (nor against being non-negative or within any sane range) before being used as base offsets for further reads: [4](#0-3) 

`spendCount`/`receiveCount` are bounded to 1–2, which limits how far the loop can walk relative to the (still-unvalidated) offset, but the offset itself can be any `int` value derived from the crafted input (including negative numbers when interpreted as fixed-position ints from `data`, or large values exceeding `data.length`). Any of these `System.arraycopy(data, spendOffset + ..., ...)` calls can therefore attempt to read outside the bounds of the `data` array — analogous to `copy_from_lzss_window`'s unchecked window offset causing an out-of-bounds heap read in libarchive.

### Impact Explanation
An out-of-bounds array access in Java throws `ArrayIndexOutOfBoundsException` rather than leaking adjacent heap memory the way a native C buffer over-read would, so this does not directly leak process memory. However, `execute` is invoked as part of TVM contract execution triggered by any account calling the zk Sapling "verify transfer" precompiled contract with malicious `data`. The code's `catch (Throwable any)` blocks around this logic mean the exception is currently caught and handled gracefully within `execute`, so a crash here would already be defensively contained — meaning the immediately observable impact is likely limited to an internal exception being logged rather than a node crash. Because of this containment, and the fact I could not verify from available context whether any exception path here could escape the `catch (Throwable any)` guard (e.g., via `Error` subtypes not caught by `Throwable`, which is unlikely, or via effects in the concurrent worker `Future` tasks not properly joined), I can't upgrade this beyond a low-confidence Medium-severity finding.

### Likelihood Explanation
The precompiled contract is reachable from any unprivileged contract call carrying data of exactly one of the four permitted lengths, so triggering the malformed-offset path only requires crafting a valid-length payload with an out-of-range offset word — a low-effort, fully attacker-controlled input, making the trigger condition itself highly likely to be reachable.

### Recommendation
Add explicit bounds validation for `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` against `data.length` (and reject negative values) immediately after parsing them and before they are used as array indices, mirroring the length checks already present in `extractBytesArray` (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:399-411`) which checks `offset > words.length - 1` before indexing.

### Proof of Concept
Craft a 2080-byte (or 2368/2464/2752-byte) payload for the zk "verify transfer" precompile input where the first 32-byte word (`spendOffset`) encodes an integer at or beyond `data.length` (e.g., `data.length - 4`, or a large positive value like `0x7FFFFFFF` truncated via `parseInt`). Submitting a transaction that calls this precompiled contract with such data drives execution into `parseInt(data, spendOffset)` and the subsequent `System.arraycopy(data, spendOffset + 320*i, ...)` calls, whose source offset exceeds the true buffer bounds, throwing `ArrayIndexOutOfBoundsException` inside the precompile's `execute` (caught internally, but confirming the missing bounds check exists exactly as analyzed).

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1465-1479)
```java
    public Pair<Boolean, byte[]> execute(byte[] data) {
      if (data == null) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
      if (!Arrays.asList(SIZE).contains(data.length)) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
      try {
        byte[] bindingSig = new byte[64];
        byte[] signHash = new byte[32];
        byte[][] frontier = new byte[33][32];
        //parse unfixed field offset
        int spendOffset = parseInt(data, 0);
        int spendAuthSigOffset = parseInt(data, 32);
        int receiveOffset = parseInt(data, 64);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1492-1531)
```java
        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);

        if (spendCount != spendAuthSigCount || spendCount < 1
            || spendCount > 2 || receiveCount < 1 || receiveCount > 2) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
        byte[][] anchor = new byte[spendCount][32];
        byte[][] nullifier = new byte[spendCount][32];
        byte[][] spendCv = new byte[spendCount][32];
        byte[][] rk = new byte[spendCount][32];
        byte[][] spendProof = new byte[spendCount][192];
        byte[][] spendAuthSig = new byte[spendCount][64];
        byte[][] receiveCm = new byte[receiveCount][32];
        byte[][] receiveCv = new byte[receiveCount][32];
        byte[][] receiveEpk = new byte[receiveCount][32];
        byte[][] receiveProof = new byte[receiveCount][192];

        //spend
        spendOffset += 32;
        for (int i = 0; i < spendCount; i++) {
          System.arraycopy(data, spendOffset + 320 * i, nullifier[i], 0, 32);
          System.arraycopy(data, spendOffset + 320 * i + 32, anchor[i], 0, 32);
          System.arraycopy(data, spendOffset + 320 * i + 64, spendCv[i], 0, 32);
          System.arraycopy(data, spendOffset + 320 * i + 96, rk[i], 0, 32);
          System.arraycopy(data, spendOffset + 320 * i + 128, spendProof[i], 0, 192);
        }
        spendAuthSigOffset += 32;
        for (int i = 0; i < spendCount; i++) {
          System.arraycopy(data, spendAuthSigOffset + 64 * i, spendAuthSig[i], 0, 64);
        }
        //output
        receiveOffset += 32;
        for (int i = 0; i < receiveCount; i++) {
          System.arraycopy(data, receiveOffset + 288 * i, receiveCm[i], 0, 32);
          System.arraycopy(data, receiveOffset + 288 * i + 32, receiveCv[i], 0, 32);
          System.arraycopy(data, receiveOffset + 288 * i + 64, receiveEpk[i], 0, 32);
          System.arraycopy(data, receiveOffset + 288 * i + 96, receiveProof[i], 0, 192);
        }
```
