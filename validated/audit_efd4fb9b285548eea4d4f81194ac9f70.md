Based on my investigation, I found a plausible analog in the batch signature verification precompile, though I was not able to trace the full call path within budget to `BatchValidateSign`/`ValidateMultiSign` `execute()` to fully confirm exploitability, so I flag the remaining uncertainty explicitly below.

### Title
Unvalidated attacker-controlled array length in precompile ABI decoding leads to `NegativeArraySizeException`/OOM DoS - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The CVE-2015-8396 analog is an integer/length field taken directly from attacker-supplied input and used to size a buffer without bounds validation, causing a crash. In java-tron, `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java` read a length word directly from calldata (`words[offset].intValueSafe()`) and immediately use it to allocate an array (`new byte[len][]`) or compute a byte offset (`(bytesOffset + offset + 2) * WORD_SIZE`) before touching the underlying `data` buffer, with no upper bound or non-negative check on `len` prior to allocation. [1](#0-0) [2](#0-1) 

### Finding Description
These helpers are used to decode ABI-encoded dynamic arrays (`bytes32[]`, `bytes[]`, signature arrays) passed as calldata to the `BatchValidateSign` (address `0x...09`) and `ValidateMultiSign` (address `0x...0a`) precompiled contracts, which are reachable by any contract call from an unprivileged transaction once `VMConfig.allowTvmSolidity059()` is active. [3](#0-2) 
The `len` value comes from `words[offset].intValueSafe()` — a 256-bit `DataWord` reduced to an `int` — fully attacker-controlled and unchecked, then used directly as an array-allocation size: [4](#0-3) 
A crafted value can make `len` negative (throwing `NegativeArraySizeException`) or extremely large (attempting a huge `byte[][]` allocation, causing `OutOfMemoryError`). Additionally, `extractBytesArray`'s offset arithmetic `(bytesOffset + offset + 2) * WORD_SIZE` can integer-overflow for large offset words before it reaches `extractBytes`'s `Arrays.copyOfRange`, producing unpredictable negative offsets and further uncontrolled exceptions. [5](#0-4) [6](#0-5) 

### Impact Explanation
Unlike the native GDCM buffer overflow (memory corruption/RCE), Java's bounds-checked arrays convert this integer-overflow class of bug into an uncaught runtime exception (`NegativeArraySizeException`/`OutOfMemoryError`) during TVM execution. Whether this actually crashes the node or is safely caught and reverted as a normal contract-execution failure depends on the exception-handling wrapper around `PrecompiledContract.execute()` in the `Program`/`Runtime` call path, which I was not able to fully verify within the available investigation budget.

### Likelihood Explanation
Reachability is high in principle: any account can submit a `TriggerSmartContract` transaction that calls the `BatchValidateSign` or `ValidateMultiSign` precompile address with crafted calldata containing an out-of-range length word — no special privilege is required, only the relevant `VMConfig` feature flag being enabled on the network. However, I could not confirm from the excerpts retrieved whether the exception is caught by an enclosing try/catch (as seen elsewhere in this same file, e.g., `ECRecover.execute()`'s `catch (Throwable any)`), which would downgrade this from a node-crash risk to a simple transaction revert. [7](#0-6) 

### Recommendation
Add explicit bounds validation (`len >= 0` and `len <= reasonable max`, e.g., bounded by `words.length` or a configured cap) before allocating `bytesArray`/`bytes32Array` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, and validate the computed `bytesOffset`/`(bytesOffset + offset + 2) * WORD_SIZE` against `data.length` before calling `extractBytes`, rejecting the precompile call (`Pair.of(false, ...)`) instead of allowing an unchecked exception to propagate.

### Proof of Concept
Not fully verified. A conceptual PoC would involve constructing calldata to `BatchValidateSign`/`ValidateMultiSign` where the ABI-encoded array-length word at the expected offset is `0xFFFFFFFF...` (interpreted as a negative `int` via `intValueSafe()`) or a very large positive value, then invoking the precompile via a `TriggerSmartContract` transaction and observing whether the resulting `NegativeArraySizeException`/`OutOfMemoryError` is caught internally or propagates to crash the TVM execution/node — confirming this requires locating and reading the `BatchValidateSign`/`ValidateMultiSign.execute()` bodies and their callers in `Program`/`Runtime`, which I was unable to retrieve before running out of tool budget.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L254-259)
```java
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(validateMultiSignAddr)) {
      return validateMultiSign;
    }
```

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L608-623)
```java
      try {
        System.arraycopy(data, 0, h, 0, 32);
        System.arraycopy(data, 32, v, 0, 32);
        System.arraycopy(data, 64, r, 0, 32);

        int sLength = data.length < 128 ? data.length - 96 : 32;
        System.arraycopy(data, 96, s, 0, sLength);

        SignatureInterface signature = SignUtils.fromComponents(r, s, v[31]
            , CommonParameter.getInstance().isECKeyCryptoEngine());
        if (validateV(v) && signature.validateComponents()) {
          out = new DataWord(SignUtils.signatureToAddress(h, signature
              , CommonParameter.getInstance().isECKeyCryptoEngine()));
        }
      } catch (Throwable any) {
      }
```
