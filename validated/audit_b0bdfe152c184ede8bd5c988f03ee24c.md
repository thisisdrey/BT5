## Finding

### Title
Unbounded attacker-controlled length used to allocate byte-array slices in `ValidateMultiSign` precompile - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The free5gc CVE-2025-56394 bug class is: an externally supplied length/identity field is used to size or index an internal slice without validating it against the real buffer bounds, leading to an out-of-bounds/overflow condition. The same pattern exists in java-tron's `ValidateMultiSign` TVM precompiled contract (address `0x66`), which is reachable from any smart-contract call (i.e., any unprivileged transaction that triggers a contract, including a `TriggerSmartContract`).

### Finding Description
`ValidateMultiSign.execute()` reads a raw ABI-encoded byte array supplied by the caller and derives array sizes directly from attacker-controlled words without validating them against the actual data length: [1](#0-0) 

`extractBytesArray` computes `len = words[offset].intValueSafe()` from caller-supplied `data` and immediately allocates `new byte[len][]`, then indexes `words[offset + i + 1]` and `words[offset + bytesOffset + 1]` for `i` up to `len`, without ever checking `len` (or the derived indices) against `words.length`: [1](#0-0) 

The sibling helper `extractSigArray`, used on the "restricted" code path, has the identical shape — it only guards the *starting* offset (`offset > words.length - 1`), not the loop bound derived from `len`: [2](#0-1) 

`ValidateMultiSign.execute()` calls one of these two helpers based on a feature flag, and the call happens *before* the method's only `try/catch` block, which wraps just the account/permission-weight computation: [3](#0-2) 

Because `intValueSafe()` can return values up to `Integer.MAX_VALUE`, a crafted call can force `new byte[len][]` (in `extractBytesArray`) to attempt to allocate on the order of `2^31` reference slots, or force `bytesOffset`/`offset + i + 1` indices in the `words` array far past `words.length`, throwing `OutOfMemoryError` or `ArrayIndexOutOfBoundsException` outside of the guarded try-block, i.e., unhandled inside `execute()` itself. This mirrors the free5gc root cause: an untrusted length field used to size a slice/array without bound-checking against the real backing buffer.

### Impact Explanation
Any account can invoke this precompile via a `TriggerSmartContract` that performs a low-level `CALL`/`STATICCALL` to address `0x66` with crafted calldata. If the resulting `OutOfMemoryError` or unhandled `ArrayIndexOutOfBoundsException` is not fully absorbed by higher-level exception handling in the TVM `CALL` opcode dispatch, it can crash or destabilize the executing node (or at minimum force it into an inconsistent state while processing an otherwise ordinary transaction), which the report criteria treats as a node-crash-class impact. Even in the case where the JVM-level `Error`/exception is caught somewhere higher up the call stack, this is a clear robustness/DoS gap that deviates from the surrounding code's convention of bounding array-derived lengths (as seen, e.g., in `RLP.verifyLength`).

### Likelihood Explanation
The precompile is reachable by any unprivileged account issuing a normal smart-contract transaction — no special permissions, staking, or witness/committee status are required. The attack requires only crafting ABI calldata with an oversized `len` word, which is trivial. This significantly raises likelihood versus vulnerabilities that require validator/witness collusion.

### Recommendation
Bound-check `len` (and the derived `offset + i + 1`/`bytesOffset` indices) against `words.length` before allocating `new byte[len][]` or indexing `words[...]` in both `extractBytesArray` and `extractSigArray`, mirroring the `verifyLength`-style bounds checks used elsewhere in the codebase (e.g., in `org.tron.core.capsule.utils.RLP`). Reject with `Pair.of(false, EMPTY_BYTE_ARRAY)` instead of throwing when the computed length/offset exceeds available `words`/`data` bounds, and move the signature-extraction calls inside the existing `try/catch` in `ValidateMultiSign.execute()` as defense in depth.

### Proof of Concept
Conceptually (exact PoC would need to be validated in a live/test environment, which I could not execute here):
1. Deploy or use any contract that issues a low-level `CALL` to precompile address `0x66` (`ValidateMultiSign`).
2. Craft calldata following the header layout expected by `execute()`, but set the word at the signature-array-length offset (`words[offset]`, consumed as `len` in `extractBytesArray`/`extractSigArray`) to a very large value such as `0x7FFFFFFF`.
3. Submit this as a `TriggerSmartContract` transaction.
4. Observe that `new byte[len][]` in `extractBytesArray` (or the equivalent indexing in `extractSigArray`) throws `OutOfMemoryError`/`ArrayIndexOutOfBoundsException` before the method's `try/catch` block is reached, which is not part of the intended "return false on malformed input" contract behavior implemented elsewhere in `PrecompiledContracts`.

Note: I was not able to trace how far up the TVM call stack (`Program`/`OperationActions` for the `CALL` opcode) this exception ultimately propagates or gets caught in this snapshot, so the exact severity (revert-only vs. actual node crash) could not be fully confirmed with the tools available; a live PoC run against a test node would be needed to confirm the terminal impact.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1080)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }

      AccountCapsule account = this.getDeposit().getAccount(address);
```
