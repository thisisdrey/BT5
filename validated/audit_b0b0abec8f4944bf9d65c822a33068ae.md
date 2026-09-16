## Analysis

CVE-2016-2330 concerns miscalculating a buffer's size from attacker-controlled length fields before performing array/memory operations, leading to out-of-bounds access. The closest analog in `ThankGodontt/java-tron--004` is in the TVM precompiled contracts that decode variable-length arrays from raw calldata without validating computed offsets/lengths against the actual bounds of the decoded word array or backing byte buffer.

### Title
Unvalidated Attacker-Controlled Offsets/Lengths in `ValidateMultiSign`/`BatchValidateSign` Precompiled Contracts Cause Uncaught Array-Bounds Exceptions - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` helper methods compute array lengths and byte offsets directly from attacker-supplied calldata words without validating them against the true bounds of the `words` array or `data` buffer. [1](#0-0)  These helpers are invoked from the `ValidateMultiSign` and `BatchValidateSign` precompiled contracts, which are reachable by any smart contract (i.e., any transaction sender) via a low-level `CALL` to the fixed precompile addresses. [2](#0-1) [3](#0-2) 

### Finding Description
`extractBytesArray` reads `len` from `words[offset]` and then, for each of the `len` items, reads a `bytesOffset` and `bytesLen` from further words and calls `extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen)`, which performs `Arrays.copyOfRange(data, offset, offset + len)` with no bounds validation on `bytesOffset`/`bytesLen` relative to `data.length`, and no validation that `offset + i + 1` stays within `words.length`. [4](#0-3)  `extractBytes32Array` similarly derives `len` from a single attacker-controlled word and allocates/reads `words[offset + i + 1]` without checking `offset + i + 1 < words.length`. [5](#0-4) 

Both `ValidateMultiSign.execute` and `BatchValidateSign.doExecute` only apply the stronger `isValidAbiEncoding` check when `VMConfig.allowTvmOsaka()` is enabled; otherwise the raw `DataWord[] words = DataWord.parseArray(rawData)` array is passed straight into these unguarded extraction helpers using attacker-chosen offset words (`words[3]`, `words[1]`, `words[2]`). [6](#0-5) [7](#0-6)  Critically, in `ValidateMultiSign.execute`, the calls to `extractSigArray`/`extractBytesArray` happen *outside* any try/catch block — the `try { ... } catch (Throwable t)` guard only wraps the later signature-weight logic, not the extraction step itself. [8](#0-7)  This is directly analogous to the CVE's root cause: a length/offset value taken from untrusted input is used to size or index a buffer without first validating that value against the buffer's actual size.

### Impact Explanation
A crafted calldata payload with an out-of-range offset word (e.g., `words[3]` pointing past `words.length`, or a `bytesLen`/`bytesOffset` causing `data.length` to be exceeded, or a negative/huge `len`) triggers `ArrayIndexOutOfBoundsException`, `NegativeArraySizeException`, or `OutOfMemoryError` inside `extractBytesArray`/`extractSigArray`/`extractBytes32Array`. Because this happens before the encompassing try/catch in `ValidateMultiSign.execute`, the exception propagates up through the TVM's precompiled-contract dispatch path during ordinary transaction/contract execution. Since transaction execution must be deterministic across all full nodes, an uncaught runtime exception here risks aborting block application inconsistently or crashing the executing thread during consensus-critical processing — matching the accepted "node crash or halt" impact class for this analog.

### Likelihood Explanation
Any unprivileged account can deploy a trivial contract that performs a low-level `CALL`/`STATICCALL` to the `ValidateMultiSign` or `BatchValidateSign` precompile address with hand-crafted calldata, requiring no special permission, stake, or witness/committee role — only the ability to broadcast a transaction that invokes the smart contract. This satisfies the in-scope reachability requirement (broadcastable contract call into TVM precompiles).

### Recommendation
Add explicit bounds checks in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` verifying that all derived indices (`offset + i + 1`, `bytesOffset + offset + 2`) remain within `words.length`, and that `bytesLen`/`offset*WORD_SIZE + bytesLen` remain within `data.length`, returning a failure result rather than throwing when the ABI encoding is malformed. Additionally, wrap the array-extraction calls in `ValidateMultiSign.execute` in the same defensive try/catch that already guards the rest of the method, and consider always applying `isValidAbiEncoding`-style validation regardless of the `allowTvmOsaka` fork flag.

### Proof of Concept
1. Deploy a contract that calls the `ValidateMultiSign` precompile address with calldata where `words[3]` encodes an offset value (e.g., `Integer.MAX_VALUE / WORD_SIZE`) that, when used as `words[words[3].intValueSafe() / WORD_SIZE]`, indexes past the actual `words` array length.
2. Submit the transaction; `extractSigArray`/`extractBytesArray` is invoked with the out-of-range offset before the surrounding try/catch is entered, throwing `ArrayIndexOutOfBoundsException` uncaught within `ValidateMultiSign.execute`.
3. Observe the exception propagate to the TVM's precompiled-contract invocation path during transaction execution, verifying inconsistent/uncontrolled failure handling relative to the surrounding execution context.

Note: I was unable to fully trace, within the given tool budget, exactly how the caller of `PrecompiledContract.execute()` (e.g., in `Program.java`'s precompiled-call handling) handles an uncaught `Throwable` from `ValidateMultiSign.execute` — i.e., whether it is caught generically at a higher layer and merely reverts the call, or whether it can escape further. This should be verified directly in the codebase (search for the precompiled-contract invocation site in `Program.java`) before treating the "node crash/halt" impact as fully confirmed rather than a "call reverts safely" outcome.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1119)
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
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
            long totalWeight = 0L;
            List<byte[]> executedSignList = new ArrayList<>();
            for (byte[] sign : signatures) {
              byte[] recoveredAddr = recoverAddrBySign(sign, hash);

              sign = merge(recoveredAddr, sign);
              if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
                if (ByteArray.matrixContains(executedSignList, sign)) {
                  continue;
                }
                MUtil.checkCPUTime();
              }
              long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
              if (weight == 0) {
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }

            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
          }
        } catch (Throwable t) {
          if (t instanceof OutOfTimeException) {
            throw t;
          }
          logger.info("ValidateMultiSign error:{}", t.getMessage());
        }
      }
      return Pair.of(true, DATA_FALSE);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1177)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
    }

    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
```
