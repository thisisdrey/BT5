### Title
Allocation Amplification via Attacker-Controlled Array Length in TVM Precompiled Contract ABI Decoders - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.java` contains internal ABI-decoding helpers (`extractBytesArray`, `extractSigArray`, `extractBytes32Array`) used by TVM precompiled contracts (e.g. multi-signature validation precompiles) that read a length field directly from the calldata word array and immediately allocate a Java array of that length, before validating the length against the actual remaining calldata size or any protocol-level bound on the number of items. [1](#0-0) 

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` each read a 32-byte word from the caller-supplied `data`/`words` and convert it directly to a Java `int` via `intValueSafe()`, then allocate a native array (`new byte[len][]`) sized exactly to that attacker-controlled value: [1](#0-0) 

Because `len` comes straight from an arbitrary 32-byte calldata word (`words[offset].intValueSafe()`), it can be any value up to `Integer.MAX_VALUE` regardless of how much real data was actually supplied in the transaction's calldata. The allocation `new byte[len][]` occurs *before* any check that `len` is consistent with the length of `data`/`words`, or with a sane protocol ceiling on the number of signatures/items a multisig validation precompile is expected to process. This mirrors the reported bug class exactly: a generic/loose deserialization bound (an arbitrary 32-byte length word) is used to size an allocation before the tighter, semantically-correct limit (actual remaining calldata bytes, or a fixed max signature count) is enforced.

This differs from the properly-gated pattern seen elsewhere in the TVM interpreter, where memory-expansion opcodes (`CODECOPY`, `EXTCODECOPY`, etc.) compute and charge energy for the requested size and enforce a hard `MEM_LIMIT` (3 MiB) via `EnergyCost.checkMemorySize` *before* the opcode action executes and allocates the buffer. [2](#0-1) 
The precompiled-contract helpers above have no equivalent pre-allocation size/energy gate tied to the decoded `len` value itself — the contract's energy cost (`getEnergyForData`) is computed from the raw `data.length` (the actual calldata bytes submitted, which is cheap/small), not from the value encoded inside that calldata that drives the allocation size.

### Impact Explanation
An unprivileged account can submit a `TriggerSmartContract` transaction whose calldata targets a precompiled contract address that uses one of these decoders (multi-signature validation precompiles are the known callers of this ABI-extraction family in `PrecompiledContracts.java`). By crafting a small calldata payload whose length-word field encodes a very large value (e.g., close to `Integer.MAX_VALUE`), the node attempts to allocate a correspondingly huge array (`new byte[len][]`), which can throw `OutOfMemoryError` or consume excessive heap, causing node instability/crash — a denial-of-service condition reachable from a single, ordinary signed transaction/contract call, in scope per "TVM opcodes, precompiles and energy metering."

### Likelihood Explanation
The precompile is reachable from any account via a standard `TriggerSmartContract` call — no special privileges, staking, or prior state are required. The attacker only needs to know the calldata word offset that the decoder interprets as `len` and set it to a large value; the actual transaction/calldata size needed to trigger the call remains small (bounded only by the generic message-size limits), so the fee/energy cost paid by the attacker for triggering the allocation is disproportionately low compared to the amplification achieved.

### Recommendation
In `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate the decoded `len` against a sane protocol-level maximum (e.g., the maximum number of signatures/items the specific precompile is designed to accept) and against consistency with the actual remaining `data`/`words` length, before performing the array allocation. Reject with an early, cheap failure (mirroring the `EnergyCost.checkMemorySize`-style pre-check used for VM memory expansion) rather than allocating first and validating per-item afterward.

### Proof of Concept
Not independently executed; based on static analysis of the decode-then-allocate pattern:
1. Construct calldata for a precompiled contract address that invokes `extractBytesArray`/`extractSigArray`/`extractBytes32Array` (the multi-signature validation precompiles in `PrecompiledContracts.java`).
2. Set the word at the offset consumed as the array-length field to a large value (e.g. `0x7fffffff`).
3. Submit a `TriggerSmartContract` transaction calling the precompile with this calldata.
4. The decoder executes `int len = words[offset].intValueSafe(); byte[][] bytesArray = new byte[len][];` before any bound check, attempting a multi-gigabyte allocation from a small, cheap transaction.

Note: I was unable to fully trace, within the available iterations, the exact precompiled-contract class(es) and their `getEnergyForData` implementations that invoke these three helper methods (call sites were only partially confirmed), so the precise energy cost paid by an attacker per call and the exact reachable precompile address(es) should be verified directly in the repository before treating this as fully confirmed.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L572-576)
```java
  private static void checkMemorySize(int op, BigInteger newMemSize) {
    if (newMemSize.compareTo(MEM_LIMIT) > 0) {
      throw Program.Exception.memoryOverflow(op);
    }
  }
```
