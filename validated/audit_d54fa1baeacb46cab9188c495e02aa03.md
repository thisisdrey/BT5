### Title
Missing bounds validation on attacker-controlled offset fields in Sapling ZK precompile input parsing leads to out-of-bounds read - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`VerifyTransferProof.execute()` (the Sapling "verify transfer proof" precompiled contract, reachable from any TVM contract call via `staticcall`/`call` to the precompile address) reads three offset fields directly from the untrusted `data` byte array supplied by the caller and then immediately uses them to index back into the same array, without validating that the resulting offsets/lengths stay inside the buffer bounds.

### Finding Description
`VerifyTransferProof.execute(byte[] data)` validates only that `data.length` matches one of a fixed set of sizes, then parses three offset values straight out of the payload: [1](#0-0) 

`spendOffset`, `spendAuthSigOffset` and `receiveOffset` are fully attacker-controlled 32-bit integers taken from `parseInt(data, 0/32/64)`. They are then passed straight back into `parseInt(data, spendOffset)` etc. to derive `spendCount`/`receiveCount`, and subsequently into `System.arraycopy(data, spendOffset + 320*i, ...)` loops: [2](#0-1) 

There is no check that `spendOffset`, `spendAuthSigOffset`, or `receiveOffset` are within `[0, data.length)` before they are used as array indices. A crafted contract call can set these offsets to arbitrary values (including negative numbers or values far beyond `data.length`), causing `parseInt`/`System.arraycopy` to read outside the intended region of the byte array. The equivalent sibling function `extractBytesArray`/`extractSigArray` in the same file explicitly guards against this with `if (offset > words.length - 1) return new byte[0][];`, but `extractBytes32Array` and the offset-parsing logic inside `VerifyTransferProof`/`VerifyMintProof` do not apply the same discipline to the raw `data` buffer indices, which is the same missing-length-validation root cause as the NimBLE HCI "Number of Completed Packets" bug: a count/offset field taken from untrusted input is trusted and used directly to index a buffer.

### Impact Explanation
Because `execute()` wraps the parsing logic in `try { ... } catch (Throwable any) { ... }` and returns `Pair.of(true, DataWord.ZERO().getData())` on any exception, an out-of-range `ArrayIndexOutOfBoundsException` is swallowed at the call site. This limits the practical impact to a caught exception and an incorrect (always-false) verification result rather than a JVM crash or leaked heap memory — there is no way to distinguish an intentionally-invalid proof from an out-of-bounds access from the return value, and no memory disclosure occurs since the exception aborts the arraycopy before any data can be returned to the caller. This mirrors the original NimBLE advisory's own characterization ("this issue requires broken/bogus input and thus severity is considered low").

### Likelihood Explanation
Trivial to trigger: any account can call `TriggerSmartContract` invoking the Sapling `verifyTransferProof` precompile address with a payload of the correct fixed length but with corrupted offset words. No special privileges, contract deployment, or additional preconditions are needed.

### Recommendation
Add explicit bounds checks before using `spendOffset`, `spendAuthSigOffset`, and `receiveOffset` (and the equivalent field in `VerifyMintProof`) to index into `data`, e.g. reject if `offset < 0 || offset + requiredLength > data.length`, mirroring the guard already present in `extractBytesArray`/`extractSigArray`. Apply the same bound check to `extractBytes32Array`, which currently lacks the `offset > words.length - 1` guard that its siblings have.

### Proof of Concept
1. Craft a TVM contract that calls the Sapling `verifyTransferProof` precompiled contract address with `data.length` equal to one of the valid sizes in `SIZE = {2080, 2368, 2464, 2752}` (see `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1448`).
2. Set the 32-byte word at offset 0 (`spendOffset`) to a large or negative value that, when used as `parseInt(data, spendOffset)` / `System.arraycopy(data, spendOffset + 320*i, ...)`, falls outside `[0, data.length)`.
3. Broadcast the transaction; the resulting `ArrayIndexOutOfBoundsException` is caught internally and the call returns `DataWord.ZERO()`, confirming the unchecked out-of-bounds array access occurs before validation of the offset against the buffer size.

Note: I could not fully trace every caller/consumer of `extractBytes32Array` within the available indexed context, so I cannot confirm whether that specific helper is reachable with an out-of-range offset in the current codebase state; the `VerifyTransferProof`/`VerifyMintProof` finding above is directly confirmed from the file content examined.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1476-1494)
```java
        //parse unfixed field offset
        int spendOffset = parseInt(data, 0);
        int spendAuthSigOffset = parseInt(data, 32);
        int receiveOffset = parseInt(data, 64);
        System.arraycopy(data, 96, bindingSig, 0, 64);
        System.arraycopy(data, 160, signHash, 0, 32);
        //parse value
        long value = parseLong(data, 192);
        for (int i = 0; i < 33; i++) {
          System.arraycopy(data, i * 32 + 224, frontier[i], 0, 32);
        }
        long leafCount = parseLong(data, 1280);
        if (leafCount >= TREE_WIDTH - 1) {
          return Pair.of(true, DataWord.ZERO().getData());
        }

        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1511-1531)
```java
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
