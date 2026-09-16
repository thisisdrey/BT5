Based on my research, I found a viable analog: uncontrolled, attacker-supplied length values used directly as array-allocation sizes inside the TVM precompiled-contract input parsers, reachable from any contract call to the `BatchValidateSign` / `ValidateMultiSign` precompiles.

### Title
Unbounded array allocation from attacker-controlled length field in precompiled-contract input parsing - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` in `PrecompiledContracts.java` read a `len` value straight out of caller-supplied call data (`words[offset].intValueSafe()`) and immediately use it to allocate a two-dimensional array (`new byte[len][]`) with no upper bound or sanity check before the loop that populates it. [1](#0-0) 

### Finding Description
These helper methods decode ABI-style dynamic arrays out of the raw `data` byte array passed to a precompiled contract's `execute()` method. The `len` field comes directly from the transaction/contract call data (`words[offset].intValueSafe()`), and is used unchecked to size a `byte[][]` before any bound is enforced: [2](#0-1) 

This is structurally analogous to the CVE-2021-20308 class of bug: an integer/length value taken from untrusted input is used to size a memory allocation without validating it is within a sane, expected range first. Here, a contract caller (or contract deployer whose bytecode invokes these precompiles) fully controls the calldata delivered to the precompile, and therefore fully controls `len`. Depending on the magnitude accepted by `DataWord.intValueSafe()` (which clamps very large `DataWord` values into the `int` range rather than rejecting them), this can result in extremely large array allocations, `NegativeArraySizeException`, or `OutOfMemoryError` during precompile execution inside energy-metered TVM opcodes.

By contrast, other similar precompile paths in the same file (e.g., the shielded `VerifyTransferProof.execute`) do bound the analogous counts before allocating: `spendCount`/`receiveCount` are validated to be between 1 and 2 before any array is sized. [3](#0-2)  The `extractBytesArray`/`extractBytes32Array`/`extractSigArray` helpers used by the multisig-validation precompiles lack this equivalent guard.

### Impact Explanation
If `len` can be driven to a very large or negative value, the resulting allocation attempt can throw an unhandled `OutOfMemoryError` or `NegativeArraySizeException` inside TVM execution. Depending on whether the surrounding precompile `execute()` try/catch scope (as seen in other precompiles such as `VerifyMintProof`, which wraps execution in `try { ... } catch (Throwable any) { ... }`) also wraps these code paths, this can either be gracefully absorbed (mere revert/no-op, low impact) or propagate up and destabilize node execution of that transaction/block (denial of service). I could not fully confirm from available context whether the call sites of `extractBytesArray`/`extractSigArray` in the multisig precompiles are wrapped by an outer `catch (Throwable)` the way `VerifyMintProof` is, so the ability to actually crash node processing (vs. merely fail the single transaction) is not fully verified within this investigation.

### Likelihood Explanation
Reachability is high: any account can construct a transaction that calls a smart contract invoking the `BatchValidateSign`/`ValidateMultiSign` precompile addresses with crafted calldata, making the `len` field arbitrary within the range representable via `intValueSafe()`. No special privilege (SR, witness, committee) is required — this is exactly the "contract deployer / order placer" class of caller this scan is scoped to include.

### Recommendation
Add explicit upper-bound (and non-negative) validation on `len` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` before allocating the `byte[][]` array — mirroring the `spendCount`/`receiveCount` range checks already used in `VerifyTransferProof.execute`. Additionally, verify that any exception thrown while parsing precompile input is caught and converted into a normal execution failure (energy exhaustion / revert) rather than propagating as an unhandled runtime error.

### Proof of Concept
Exact reproduction was not completed within this investigation because I could not confirm the precise `DataWord.intValueSafe()` clamping behavior or verify whether the multisig precompile's `execute()` wraps `extractBytesArray`/`extractSigArray` calls in a catch-all handler — both are necessary to determine whether the effect is a contained transaction failure or an actual node-crashing DoS. This would require further static tracing of `DataWord.intValueSafe()` in `common/src/main/java/org/tron/common/runtime/vm/DataWord.java` and full inspection of the `BatchValidateSign`/`ValidateMultiSign` precompile `execute()` bodies, which the index did not fully surface in this session — a Devin session with full repo access is recommended to complete this verification and construct a concrete crafted-calldata proof of concept.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

  private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      int bytesLen = words[offset + bytesOffset + 1].intValueSafe();
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          bytesLen);
    }
    return bytesArray;
  }

  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1496-1505)
```java
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
```
