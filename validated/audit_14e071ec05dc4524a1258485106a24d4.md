## Title
Unbounded array-length field in TVM precompiled-contract ABI decoding permits out-of-memory allocation - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.java` contains three ABI-array-decoding helpers — `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` — that read an attacker-controlled "array length" word directly from smart-contract call data and immediately use it to allocate a Java array, with no upper bound placed on the value before allocation.

### Finding Description
Each helper reads the declared element count from the call data and allocates a reference array of that size before doing any bounds validation against the actual size of the supplied data: [1](#0-0) [2](#0-1) [3](#0-2) 

In all three, `len = words[offset].intValueSafe()` is taken from a single 32-byte word inside the raw precompile input and passed straight into `new byte[len][]` (or `new byte[len]` in `extractBytes32Array`). Unlike the RLP decoder in this same codebase, which explicitly checks a declared length against the remaining buffer size before trusting it (`verifyLength` in `RLP.java`), and unlike `ContractEventParser.parseDataBytes`/`TronJsonRpcImpl.tryDecodeRevertReason`, which the codebase's own tests show were hardened to reject declared lengths that exceed the actual payload, these `extract*Array` helpers perform no such comparison between the declared `len` and the number of words actually available (`words.length`). The only guard present, `offset > words.length - 1`, validates the *offset* of the length word, not the *value* it decodes to.

Because `intValueSafe()` derives an `int` from an arbitrary 256-bit word supplied in a transaction's contract call data, an attacker can encode a length field of up to `Integer.MAX_VALUE` while keeping the surrounding call data tiny. This is the same bug class as the referenced `bep/imagemeta` advisory: a small, well-formed-looking payload declares an implausibly large structure count, and the parser allocates memory proportional to the attacker-declared count rather than the actual payload size.

### Impact Explanation
Reaching one of these decode paths with an oversized length value causes an immediate large-scale array allocation attempt on the node processing the transaction/call, which can throw `OutOfMemoryError` or otherwise induce significant GC/memory pressure — a denial-of-service condition against the node executing or validating the transaction. This satisfies the "node crash or halt" impact bar for this class of resource-exhaustion bug.

### Likelihood Explanation
The trigger is a single, unprivileged smart-contract call, deployment, or transaction whose data reaches one of the affected precompiled-contract dispatch paths in `PrecompiledContracts.java` — no special privileges, staking, or witness/committee role is required, only the ability to submit a transaction that invokes the corresponding precompile. The likelihood otherwise depends on the exact energy/gas cost charged for the call *before* the array is allocated, which I could not fully confirm within the available investigation (I could not verify the specific precompiled-contract dispatcher and `getEnergyForData` cost function that invoke each of `extractBytes32Array`/`extractBytesArray`/`extractSigArray`, since the search results for their call sites returned only the defining file). This should be verified before treating the finding as fully confirmed.

### Recommendation
Add an explicit bound check on `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` — reject (or cap) decoding when `len` is negative, unreasonably large, or when `offset + len` exceeds `words.length`, mirroring the length-vs-available-data check already used in `RLP.verifyLength` and the hardened `ContractEventParser`/`TronJsonRpcImpl` length checks elsewhere in the codebase.

### Proof of Concept
Construct calldata for whichever precompiled contract invokes `extractBytesArray`/`extractSigArray` (batch/array-based signature or bytes-array precompile) such that the word at the declared array-length offset is set to a large value (e.g., `0x7FFFFFFF`) while the remaining calldata is minimal. Submitting this as a `TriggerSmartContract` (or equivalent) transaction causes the helper to execute `new byte[0x7FFFFFFF][]`, attempting to allocate an outsized array before any per-element bounds validation occurs.

**Note on confidence:** I was not able to identify, within the available search results, the exact precompiled contract dispatcher(s) that call `extractBytesArray` / `extractSigArray` (the grep for these method names only surfaced their definitions in `PrecompiledContracts.java`, not distinct call-site line numbers), so the precise energy-cost gating in front of this allocation is unconfirmed. This should be verified in the actual source to confirm exploitability end-to-end before treating this as fully validated.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-397)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-412)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-426)
```java
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
