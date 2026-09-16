### Title
Unvalidated attacker-controlled offsets in `VerifyTransferProof.execute()` cause out-of-bounds array access on the precompile input buffer - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The zk-SNARK shielded-transfer precompile `VerifyTransferProof` (reachable via a `CALL`/`STATICCALL` from any smart contract to its TVM precompile address) parses `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` directly from attacker-supplied `data` and then uses them, unvalidated, as base offsets for further reads (`parseInt`) and multi-chunk `System.arraycopy` operations against the same fixed-length `data` buffer, exactly mirroring the CVE-2026-64449 pattern of trusting a user-controlled offset/length against a bounded buffer without clamping.

### Finding Description
`VerifyTransferProof.execute(byte[] data)` first checks only that `data.length` is one of four fixed sizes (`{2080, 2368, 2464, 2752}`): [1](#0-0) 

It then parses three offsets straight out of the (attacker-controlled) input without any bound relative to `data.length`: [2](#0-1) 

Those unvalidated offsets are immediately used to index into `data` again to derive `spendCount` / `receiveCount`: [3](#0-2) 

and then used as the base for a series of `System.arraycopy` calls that read 32–320 byte chunks from `data` at `spendOffset + 320*i + k`, `spendAuthSigOffset + 64*i`, and `receiveOffset + 288*i`, with no check that these attacker-chosen offsets plus the fixed chunk sizes stay within `data.length`: [4](#0-3) 

This is the same bug class as the kernel CVE: a fixed-size backing buffer (`data`, capped to one of 4 known lengths) is accessed using offsets/lengths that are only validated against unrelated constraints (`data.length` equality check, `spendCount`/`receiveCount` range check) but never checked against `data.length` for the actual read positions (`spendOffset`, `spendAuthSigOffset`, `receiveOffset`, and the derived per-item strides). A crafted `spendOffset` (e.g. a large or out-of-range 32-bit value from `parseInt`) causes `parseInt(data, spendOffset)` or a subsequent `System.arraycopy` to run outside `data`'s bounds.

### Impact Explanation
Because `parseInt`/`System.arraycopy` on a byte array will throw `ArrayIndexOutOfBoundsException` (or `IndexOutOfBoundsException`) rather than silently truncate, any transaction that calls this precompile with a malformed `data` payload can trigger an unchecked runtime exception deep inside TVM execution. Depending on how the surrounding `Program`/precompile dispatch handles unexpected runtime exceptions (as opposed to the expected `BytecodeExecutionException`/`Program.Exception` hierarchy), this can propagate as an unhandled exception during block/transaction processing — an availability impact (transaction/precompile call that "the node can no longer serve" reliably), or, if handling differs between implementations/versions, a source of non-deterministic execution results between nodes.

### Likelihood Explanation
Likelihood is high for reachability: `VerifyTransferProof` is a public precompiled contract callable by any account via a normal `CALL`/`STATICCALL` from a deployed contract, requiring no special privilege — matching the "unprivileged transaction broadcaster / contract deployer" threat model. The only gate is `data.length` being one of the 4 fixed sizes; the internal offsets `spendOffset`/`spendAuthSigOffset`/`receiveOffset` are fully attacker-chosen 32-bit values within that fixed-size buffer and are never range-checked before being used as array indices for both `parseInt` and `System.arraycopy`.

### Recommendation
Before using `spendOffset`, `spendAuthSigOffset`, `receiveOffset` (and all derived per-item offsets `spendOffset + 320*i + k`, `spendAuthSigOffset + 64*i`, `receiveOffset + 288*i + k`) to index into `data`, validate that each computed offset plus its access size is within `[0, data.length]`, returning the existing `Pair.of(true, DataWord.ZERO().getData())` failure result (consistent with the other validation branches in this method) instead of letting `parseInt`/`System.arraycopy` throw. This mirrors the fix pattern in the report: clamp/validate offset+length against the buffer's actual size before every read.

### Proof of Concept
1. Deploy a contract that performs a `STATICCALL` to the `VerifyTransferProof` precompile address with a 2080-byte input (one of the accepted `SIZE` values).
2. Set the first 32-byte word (`spendOffset`) to a large value such as `0x7FFFFFFF` (or any value causing `spendOffset` (or `spendOffset + 320*i + 128`, etc.) to exceed `data.length`), while keeping `spendAuthSigOffset`/`receiveOffset` and the `leafCount` check passable.
3. Broadcast the transaction; `parseInt(data, spendOffset)` at line 1492 (or the subsequent `System.arraycopy` calls) throws `ArrayIndexOutOfBoundsException`, which is not one of the expected `Program.Exception` types the surrounding TVM dispatch is designed to catch, exercising the same "offset unchecked against buffer size" bug class as the referenced kernel CVE.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1477-1479)
```java
        int spendOffset = parseInt(data, 0);
        int spendAuthSigOffset = parseInt(data, 32);
        int receiveOffset = parseInt(data, 64);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1492-1499)
```java
        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);

        if (spendCount != spendAuthSigCount || spendCount < 1
            || spendCount > 2 || receiveCount < 1 || receiveCount > 2) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1512-1531)
```java
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
