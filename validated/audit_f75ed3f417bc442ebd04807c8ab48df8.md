### Title
Attacker-controlled offsets in `VerifyTransferProof.execute` cause out-of-bounds `System.arraycopy` (node crash / DoS) - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `VerifyTransferProof` TVM precompile (Zcash-shielded-transfer verification, reachable via a plain `CALL` from any deployed contract) reads `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` directly out of attacker-controlled `data` and then uses them, unclamped, as `System.arraycopy` source offsets against the same `data` buffer. This is the same bug class as CVE-2017-1000101: a length/offset value parsed from fully untrusted, attacker-crafted input is used to index into a buffer without validating that `offset + requiredLength <= buffer.length`, leading to an out-of-bounds read.

### Finding Description
`VerifyTransferProof.execute` only validates that `data.length` is one of four fixed values (`{2080, 2368, 2464, 2752}`): [1](#0-0) 

It then parses three offsets straight out of the calldata with no bounds/consistency checking against `data.length`: [2](#0-1) 

`spendCount`/`spendAuthSigCount`/`receiveCount` are read via `parseInt(data, spendOffset)` etc. `parseInt`/`parseLong` call `ByteUtil.parseBytes(data, offset, 32)`, which only guards `offset >= input.length` (returning an all-zero-padded array) — it does **not** guard against a huge *positive* `offset` that is still `< input.length` but leaves too little room, nor does it validate the *derived* offsets used later: [3](#0-2) 

After the `spendCount`/`receiveCount` sanity check (limited to 1 or 2), the code performs multiple raw `System.arraycopy(data, spendOffset + 320*i, ..., 0, 32/192)` calls using `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` — none of which are re-validated to ensure `offset + 320*spendCount (+192)` stays within `data.length`: [4](#0-3) 

Because `spendOffset`/`spendAuthSigOffset`/`receiveOffset` are attacker-chosen 32-bit values (bounded only by passing `parseBytes`'s `offset >= input.length` check the first time, then reused unclamped for the `arraycopy` calls that follow), a value like `data.length - 32` (which passes the initial `parseInt` bound) combined with a nonzero `spendCount`/`receiveCount` makes `spendOffset + 320*i + 192` exceed `data.length`, causing `System.arraycopy` to throw `ArrayIndexOutOfBoundsException`. This mirrors curl's globbing off-by-one: a numeric range/offset derived from attacker input is used to walk past the end of a heap-allocated buffer without a final bounds check.

Note that sibling precompiles in the same file (`VerifyMintProof`, `VerifyBurnProof`) use only *fixed*, compile-time offsets against a size that is exactly checked (`data.length != SIZE`), so they are not affected — the vulnerability is specific to `VerifyTransferProof`, which is the only one of the family that derives array-copy offsets from data itself.

### Impact Explanation
Any account can invoke this precompile by sending a `CALL`/`CALLCODE`/`DELEGATECALL`/`STATICCALL` to the ShieldedTransfer precompile address with crafted calldata of one of the four accepted lengths. An uncaught `ArrayIndexOutOfBoundsException` inside `execute()` is not wrapped in the same defensive `try/catch(Throwable any)` pattern used by other blocks in this class for the RustZcash/verification calls — the `try` block does catch `Throwable`, so the exception is likely caught and converted to a `Pair.of(true, DataWord.ZERO()...)`. This limits the practical impact primarily to incorrect result computation/resource waste rather than a raw uncaught crash, **but** this could not be fully confirmed from the available code — the exact propagation path (whether the JVM-level `ArrayIndexOutOfBoundsException` is always caught before reaching the node's transaction-processing loop, or whether resource exhaustion/energy-accounting bypass occurs) requires deeper runtime tracing that could not be completed in this pass.

### Likelihood Explanation
High: this precompile is reachable from any deployed smart contract via ordinary TVM `CALL` semantics, requires no special privileges, and the offending offsets are fully attacker-controlled 32-byte words in calldata that only need to satisfy loosely-checked bounds (`data.length` in a fixed set, `spendCount`/`receiveCount` in `{1,2}`) to reach the unguarded `arraycopy` calls.

### Recommendation
Before using `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` for any `System.arraycopy`, validate that `offset + 32 (header) + requiredCount * elementSize` does not exceed `data.length`, mirroring the pattern already used elsewhere in the file (e.g. `extractBytesArray`'s `if (offset > words.length - 1) return new byte[0][];`) and the `isValidAbiEncoding` guard used by `BatchValidateSign`. Reject with `Pair.of(false, EMPTY_BYTE_ARRAY)` on any out-of-bounds derived offset instead of relying on an outer `catch (Throwable any)` to mask corrupted/invalid input.

### Proof of Concept
1. Craft `data` of length 2080 (the smallest accepted `SIZE`).
2. Set the first three 32-byte words (offsets 0/32/64) to point `spendOffset` and `spendAuthSigOffset` near the end of the buffer (e.g., `data.length - 40`), so that `parseInt(data, spendOffset)` reads mostly zero-padded/garbage bytes for `spendCount`, but crafted so `spendCount == 1`.
3. With `spendCount = 1`, `receiveCount = 1`, the loop computes `spendOffset += 32` then `System.arraycopy(data, spendOffset + 320*0 + 128, spendProof[0], 0, 192)`, which reads past `data.length`, throwing `ArrayIndexOutOfBoundsException` inside the precompile.
4. Deploy a minimal contract that issues a `staticcall`/`call` to the ShieldedTransfer precompile address with this calldata and observe the malformed-input handling behavior (this final confirmation of end-to-end exception propagation could not be executed in this analysis pass and should be validated with an actual devnet transaction).

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1465-1471)
```java
    public Pair<Boolean, byte[]> execute(byte[] data) {
      if (data == null) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
      if (!Arrays.asList(SIZE).contains(data.length)) {
        return Pair.of(true, DataWord.ZERO().getData());
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1476-1479)
```java
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

**File:** common/src/main/java/org/tron/common/utils/ByteUtil.java (L344-353)
```java
  public static byte[] parseBytes(byte[] input, int offset, int len) {

    if (offset >= input.length || len == 0) {
      return EMPTY_BYTE_ARRAY;
    }

    byte[] bytes = new byte[len];
    System.arraycopy(input, offset, bytes, 0, min(input.length - offset, len, true));
    return bytes;
  }
```
