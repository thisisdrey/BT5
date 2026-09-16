### Title
Unchecked array-index/length fields in `PrecompiledContracts.extractBytesArray` cause out-of-bounds read / crash when decoding `batchvalidatesign`/`validatemultisign` precompile input - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
CVE-2022-47069 describes a p7zip out-of-bounds read caused by trusting length/offset fields taken directly from attacker-supplied archive data when computing where to read next, without validating those values against the actual buffer size. The same bug class — deriving array indices and read lengths from unvalidated, attacker-controlled words and using them directly to index/allocate arrays — is present in `PrecompiledContracts.extractBytesArray`, which is used to decode the dynamic `bytes[]` arguments passed to the TVM precompiled contracts (`batchvalidatesign`, `validatemultisign`) that any contract-calling transaction can invoke.

### Finding Description
`extractBytesArray` decodes a Solidity ABI-encoded `bytes[]` parameter from raw call data words: [1](#0-0) 

It only checks `offset > words.length - 1` before reading `words[offset]` for the array length (`len`). After that, `len` (attacker-controlled, up to `Integer.MAX_VALUE` since it comes straight from `intValueSafe()` on a `DataWord`) is used to allocate `new byte[len][]`, and the loop indexes `words[offset + i + 1]` and `words[offset + bytesOffset + 1]` with no bounds check against `words.length`. `bytesOffset` is likewise derived from an attacker-controlled word divided by `WORD_SIZE`, and can be an arbitrary index. This is structurally the same defect class as `FindCd`: a length/offset value read from untrusted input is used to compute further byte-array accesses without validating it against the real data bounds, in contrast to the sibling helper `extractBytes32Array` (lines 390-397) which has the same missing check, and to hardened routines elsewhere in the codebase such as `ByteUtil.parseBytes` (`common/src/main/java/org/tron/common/utils/ByteUtil.java:344-353`) and `RLP.calcLength`'s `verifyLength` calls (`framework/src/main/java/org/tron/core/capsule/utils/RLP.java:372-397`), which explicitly bound-check length values before use.

The `words` array itself is built from the raw precompile input `data` by `DataWord.parseArray`/similar decoding used before `extractBytesArray` is invoked in `batchvalidatesign`/`validatemultisign` (`VMConfig.allowTvmSolidity059()` gated addresses in `getContractForAddress`, lines 254-259), so its length is fixed by the size of the actual call data, while `len`, `bytesOffset`, and `bytesLen` are fully attacker-controlled 256-bit words truncated via `intValueSafe()`.

### Impact Explanation
Because `offset + i + 1` and `offset + bytesOffset + 1` are not bounds-checked against `words.length`, a crafted call to `batchvalidatesign`/`validatemultisign` can trigger `ArrayIndexOutOfBoundsException` inside the precompile execution path, or (depending on how the exception propagates through `execute()`) an uncontrolled exception during EVM opcode dispatch. Uncaught runtime exceptions surfacing from inside the precompiled-contract execution during block application can, in the worst case, crash node processing of a valid block (chain halt on that node) rather than being converted into a normal "contract execution reverted" result, which is the standard TVM failure semantics for out-of-bounds/malformed input.

### Likelihood Explanation
Reachability is high: any account can broadcast a `TriggerSmartContract` transaction that calls the `batchvalidatesign` (or `validatemultisign`) precompiled address with crafted call data, provided `allowTvmSolidity059` is enabled (which is the case on current networks). No special privileges, signer cooperation, or SR/witness access are required — this is directly reachable from the "contract deployer / caller" attack surface named in scope.

### Recommendation
Add explicit bounds checks in `extractBytesArray` (and `extractBytes32Array`) before each array access: validate `len >= 0`, `offset + i + 1 < words.length`, and `offset + bytesOffset + 1 < words.length` before dereferencing, returning an execution failure (`Pair.of(false, EMPTY_BYTE_ARRAY)`) instead of throwing an unchecked exception, mirroring the pattern already used in `ByteUtil.parseBytes` and `RLP.verifyLength`.

### Proof of Concept
1. Craft `TriggerSmartContract` call data targeting the `batchvalidatesign` precompile address (`0x...16` per `VMConfig.allowTvmSolidity059()` gating).
2. Encode the `bytes[]` parameter's length word (`len`) as a small legitimate value, but set the per-element offset word (consumed as `bytesOffset`) to a large value such that `offset + bytesOffset + 1` exceeds `words.length` (the actual decoded word array length derived from the real call data size).
3. Broadcast the transaction; `extractBytesArray` executes `words[offset + bytesOffset + 1].intValueSafe()`, throwing `ArrayIndexOutOfBoundsException` from inside precompile execution, which is not part of the normal TVM revert path.

*Note:* I could not fully trace the exact call chain from `batchvalidatesign`'s `execute(byte[] data)` into `extractBytesArray` within the available search budget (i.e., I did not view the `BatchValidateSign`/`ValidateMultiSign` inner classes' bodies), so the precise argument layout and whether an outer `try/catch` in `Program`/`VM` already converts such exceptions into a safe revert could not be verified with certainty. This should be confirmed by reading those classes and the opcode dispatch exception handling before treating this as a confirmed crash bug rather than a defense-in-depth gap.

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
