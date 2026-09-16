### Title
Out-of-bounds array read in `ValidateMultiSign`/`BatchValidateSign` precompiled contracts due to unvalidated offsets in `extractBytesArray`/`extractSigArray`/`extractBytes32Array` - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2019-9038 is an out-of-bounds read in matio's `ReadNextCell()` caused by trusting an attacker-controlled length/offset field read from the file without validating it against the actual buffer size before indexing into it. The same bug class exists in java-tron's TVM precompiled-contract input decoding helpers `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, which read length and offset fields directly out of attacker-supplied calldata words and use them to index further into the same `DataWord[]`/`byte[]` buffers without verifying the derived index stays within bounds.

### Finding Description
`extractBytes32Array` reads `len` from `words[offset]` and then unconditionally accesses `words[offset + i + 1]` for `i` in `[0, len)` with no check that `offset + len` is within `words.length`: [1](#0-0) 

`extractBytesArray` and `extractSigArray` similarly derive `bytesOffset`/`bytesLen` from attacker-controlled words and pass them straight into `extractBytes`, which does `Arrays.copyOfRange(data, offset, offset + len)` with no bounds validation at all: [2](#0-1) 

These helpers are invoked from `ValidateMultiSign.execute()` (precompile at address `0x...0a`) and `BatchValidateSign.doExecute()` (address `0x...09`), both reachable by any contract call via the `CALL`/`STATICCALL` opcode: [3](#0-2) [4](#0-3) 

Critically, in `ValidateMultiSign.execute()` the call to `extractSigArray`/`extractBytesArray` (lines 1072-1074) happens **before** the surrounding `try { ... } catch (Throwable t)` block that starts at line 1082. Any `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException` thrown while extracting the signature array (e.g., a crafted `bytesLen` that is negative, or a `bytesOffset` that pushes the copy range past `rawData.length`) is therefore **not caught inside the precompile**, and propagates up out of `PrecompiledContract.execute()` uncaught by this method.

The recently added `isValidAbiEncoding()` guard (gated behind `VMConfig.allowTvmOsaka()`) only checks that the total calldata length is a multiple of the expected item/header word size — it does not validate that any specific `bytesOffset`/`bytesLen` value embedded in the calldata actually points within the buffer, so a well-formed-length payload with malicious internal offsets still reaches the unguarded array indexing: [5](#0-4) 

### Impact Explanation
A crafted `bytes[]` signature array with an out-of-range length/offset word causes an uncaught runtime exception (`ArrayIndexOutOfBoundsException`/`NegativeArraySizeException`) during precompiled-contract execution as part of ordinary TVM contract execution. Because this occurs during transaction/block-application processing (`ValidateMultiSign`/`BatchValidateSign` can be called from any deployed contract), an unhandled exception at this layer risks disrupting deterministic transaction execution across nodes (all full nodes execute the same contract call while applying the block), which is a High-severity impact under the rules (node crash/halt or consensus-relevant execution divergence), consistent in class with the CVSS 7.5/AV:N/AC:L "out-of-bounds read causing crash" characterization of CVE-2019-9038.

### Likelihood Explanation
Likelihood is high: `ValidateMultiSign` and `BatchValidateSign` are TVM precompiles reachable by any account that deploys or calls a contract performing a `CALL`/`STATICCALL` to address `0x...09`/`0x...0a` with attacker-fully-controlled calldata — no special privilege, SR/witness status, or governance action is required, only a single transaction.

### Recommendation
Add explicit bounds checks before every indexing operation in `extractBytesArray`, `extractSigArray`, `extractBytes32Array`, and `extractBytes` (verify `offset`, `bytesOffset`, `bytesLen`, and `offset + i + 1` all stay within the respective array lengths, and reject negative lengths), and move the array-extraction calls inside `ValidateMultiSign.execute()`'s try/catch (or wrap them in their own try/catch) so any decoding failure is converted into the existing `Pair.of(true, DATA_FALSE)` failure path rather than propagating an uncaught exception.

### Proof of Concept
Construct calldata for `validatemultisign(address,uint256,bytes32,bytes[])` where the ABI-encoded `bytes[]` array contains a length word for one signature entry (`bytesLen`) set to a very large or negative value (e.g., `0xFFFFFFFF`) while keeping the overall calldata length a multiple of 32 bytes so any coarse length-based guard passes; call this from a simple contract via `CALL` to precompile address `0x000...a`. `extractBytesArray`/`extractSigArray` will compute `bytesOffset + offset + 2) * WORD_SIZE` plus the malicious `bytesLen`, and `Arrays.copyOfRange` in `extractBytes` will throw due to the offset/length exceeding `rawData.length`, uncaught by `ValidateMultiSign.execute()`, since the exception occurs before its enclosing try block at line 1082.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L432-438)
```java
  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1075)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1172-1177)
```java

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
```
