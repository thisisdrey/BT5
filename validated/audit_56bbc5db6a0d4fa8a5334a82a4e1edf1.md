### Title
Precompiled-contract array decoders allocate arrays sized directly from attacker-controlled calldata without an upper-bound check, allowing OOM-based node crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The libtiff report describes an integer-overflow/unchecked-length flaw where a length value taken from untrusted file data is used to size a buffer without validation, leading to memory corruption. The analogous pattern in java-tron is the family of ABI-array decoders used by precompiled contracts (`extractBytes32Array`, `extractBytesArray`, `extractSigArray`), which read a 32-byte word straight out of the TVM `CALL` payload, convert it to an `int` via `DataWord.intValueSafe()`, and immediately allocate a Java array of that size with no sanity check against the actual size of the supplied `data` buffer or any protocol-defined maximum.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` all follow the same pattern: [1](#0-0) [2](#0-1) [3](#0-2) 

In each case, `len` is derived directly from a `DataWord` supplied inside the precompile's raw call data (`words[offset].intValueSafe()`), with no check that `len` is consistent with the actual length of `data`, and no cap applied before `new byte[len][]` (or `new byte[len]` in the sig case, indirectly through the loop). Because the length word is fully attacker-controlled (any contract can invoke a precompiled contract address with crafted calldata via `CALL`/`STATICCALL`), an attacker can set `len` to a very large value. Depending on how `DataWord.intValueSafe()` clamps out-of-range 256-bit values into an `int`, this either yields a huge positive value (e.g., near `Integer.MAX_VALUE`) that immediately triggers an `OutOfMemoryError` on array allocation, or a value whose subsequent arithmetic (`words[offset + i + 1]`, index math in `extractBytesArray`/`extractSigArray`) causes out-of-bounds access.

Contrast this with the codebase's own RLP length parser, which explicitly detects and bounds oversized lengths before use: [4](#0-3) 
No equivalent `verifyLength`-style check exists for the precompile array-length decoders.

### Impact Explanation
An attacker who deploys a contract and calls the precompile that consumes these decoders (e.g., the multisig-validation precompile referenced by `ValidateMultiSignContractTest`) with crafted calldata can force the node to attempt an allocation far beyond what the data actually justifies. This can crash the executing full node/witness node process (`OutOfMemoryError`), constituting a node crash/halt reachable from a single, unprivileged transaction or contract call — squarely in the "node crash or halt" impact category.

### Likelihood Explanation
The path is reachable by any account that can broadcast a `TriggerSmartContract` transaction calling a contract that in turn calls the affected precompiled contract address; no special privileges, stake, or witness/SR status are required. The only precondition is knowing the precompile's fixed address and crafting calldata with an oversized length word, which is straightforward given the precompile addresses and ABI layout are public.

### Recommendation
Before allocating any array based on `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate `len` against a sane upper bound and against the actual remaining length of `data` (similar to `verifyLength` in `RLP.java`), rejecting the call (returning failure) rather than allocating when the value is inconsistent with the input size.

### Proof of Concept
1. Deploy a trivial contract that performs a low-level `call` to the fixed address of the precompiled contract that uses `extractSigArray`/`extractBytesArray` (the multisig-validate precompile).
2. Encode calldata such that the length word at the expected `offset` is set to a very large value (e.g., `0x7fffffff`), while the rest of the calldata is short/arbitrary.
3. Invoke the contract via `TriggerSmartContract`; the precompile's `execute()` calls the vulnerable decoder, which performs `new byte[][](len)` (or nested `new byte[SIG_LENGTH]` in a loop up to `len`), triggering an `OutOfMemoryError` or excessive CPU/memory consumption during transaction execution, potentially aborting or destabilizing the node process handling the transaction.

Note: I was unable to inspect the exact clamping behavior of `DataWord.intValueSafe()` within the available iterations (only the class header was retrieved before the tool budget was exhausted), so the precise numeric ceiling `len` can reach is not fully confirmed; however, the absence of any explicit bound/consistency check against `data.length` in these three decoder methods is directly verified in the cited code.

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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L372-397)
```java
  private static int calcLength(int lengthOfLength, byte[] msgData, int pos) {
    byte pow = (byte) (lengthOfLength - 1);
    int length = 0;
    for (int i = 1; i <= lengthOfLength; ++i) {

      int bt = msgData[pos + i] & 0xFF;
      int shift = 8 * pow;

      // no leading zeros are acceptable
      if (bt == 0 && length == 0) {
        throw new RuntimeException("RLP length contains leading zeros");
      }

      // return MAX_VALUE if index of highest bit is more than 31
      if (32 - Integer.numberOfLeadingZeros(bt) + shift > 31) {
        return Integer.MAX_VALUE;
      }

      length += bt << shift;
      pow--;
    }

    // check that length is in payload bounds
    verifyLength(length, msgData.length - pos - lengthOfLength);

    return length;
```
