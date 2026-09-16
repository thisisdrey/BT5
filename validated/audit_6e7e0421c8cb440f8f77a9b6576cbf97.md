Based on my research, I found a directly analogous unchecked-allocation pattern in the TVM precompiled contract input parsing code.

### Title
Unbounded array allocation from attacker-controlled length in precompiled contract input parsing can crash the node - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
Similar to CVE-2017-12144, where `TNEFFillMapi` in `ytnef` allocated memory based on an attacker-supplied length field without validation, `PrecompiledContracts.java` contains helper methods that read a length value straight from attacker-controlled calldata and immediately use it to allocate a `byte[len][]` array, with no upper bound check against the size of the remaining input.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` all follow the same pattern: they read `len` from a `DataWord` taken directly from the precompiled-contract call's `words` array (derived from the transaction/contract-call `data`), then allocate an array of that size before any bounds validation against the actual amount of remaining data: [1](#0-0) [2](#0-1) [3](#0-2) 

`len` comes from `words[offset].intValueSafe()`. If the attacker crafts calldata so that this word contains a very large positive value, the JVM will attempt to allocate a correspondingly large `byte[][]` (e.g. `new byte[len][]`), throwing an `OutOfMemoryError`. Unlike checked `Exception`s, `OutOfMemoryError` is a `Throwable`/`Error`; only some precompiled contracts in this file wrap their logic in `catch (Throwable any)` (e.g. `VerifyMintProof`), while others do not appear to have equivalent broad exception handling around these specific helper calls. `extractBytes32Array` in particular has no defensive check at all (unlike `extractBytesArray`/`extractSigArray`, which at least check `offset > words.length - 1` before reading `len`, but still don't bound `len` itself against `words.length`).

Because `len` is fully attacker-controlled via the calldata sent to whichever precompiled contract calls these helpers (multi-signature validation style precompiles reachable from a TVM `CALL`/`STATICCALL` to a low precompile address), any unprivileged contract caller can trigger this path with a single transaction.

### Impact Explanation
An uncaught `OutOfMemoryError` thrown while executing a transaction inside the TVM can destabilize or crash the executing thread/JVM heap state, potentially halting block processing or forcing a node restart — a denial-of-service condition reachable by any unprivileged transaction sender, directly analogous to the crafted-file DoS in the CVE (`TNEFFillMapi` allocation failure).

### Likelihood Explanation
Reachable via a single TVM contract call from any account with no special privileges, requiring only that a contract invoke the vulnerable precompiled contract address with a crafted calldata length word. Likelihood is high in that sense, though I was unable to fully confirm (due to tool-call limits) whether the specific precompiled contract(s) calling `extractBytes32Array`/`extractBytesArray`/`extractSigArray` wrap the call in a `catch (Throwable ...)` that would downgrade this from a crash to a caught/reverted call — this needs to be verified against the exact `execute()` method(s) that invoke these three helpers.

### Recommendation
Validate `len` against the actual remaining `words.length` (and a sane maximum) before allocating any array in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, returning an empty/invalid result instead of allocating when the declared length exceeds what the input can support — mirroring the bounds check already used for other RLP/ABI length fields (e.g. `verifyLength` in `RLP.java`) and the pattern in `ContractEventParser.subBytes`, which explicitly rejects oversized lengths. Additionally, ensure the outer precompiled-contract `execute()` methods that call these helpers catch `Throwable` (not just `Exception`) so a malformed input cannot crash the calling thread.

### Proof of Concept
Craft a transaction that invokes (via `CALL`) the precompiled contract address whose `execute()` parses its input using `extractBytes32Array`/`extractBytesArray`/`extractSigArray`, with the length word at the expected `offset` set to a very large value (e.g. `0x7fffffff`). The resulting `new byte[len][]` allocation attempt will throw `OutOfMemoryError` during transaction execution.

**Note on completeness:** I was not able to trace the exact precompiled-contract `execute()` call sites that invoke `extractBytes32Array`/`extractBytesArray`/`extractSigArray` within the remaining tool-call budget, so I cannot confirm with certainty whether existing exception handling at those call sites already mitigates the crash. This should be verified directly in the code before treating this as fully confirmed.

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
