Found: `ValidateMultiSign.execute()` has no outer try/catch, and its guard against malformed calldata (`isValidAbiEncoding`) only applies when `VMConfig.allowTvmOsaka()` is active. Confirmed by the codebase's own test `testTip854PreActivationNoOp`, which documents: "before activation, malformed calldata reaches the legacy decoder... this precompile has no outer catch, so a too-short input raises inside the decoder." [1](#0-0) 

### Title
Unbounded length field from precompile calldata drives out-of-bounds array access in `ValidateMultiSign`/legacy `extractBytesArray` path - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`ValidateMultiSign.execute()` parses the ABI-encoded calldata into 32-byte words with `DataWord.parseArray(rawData)`, then reads a length field (`sigArraySize`/array offset) directly out of attacker-controlled calldata and uses it to index into the `words[]` array and to allocate/index nested byte arrays, mirroring the CVE-2020-13910 pattern: "a field of an incoming network packet is directly used as a length field without any bounds check."

### Finding Description
`ValidateMultiSign.execute()` [2](#0-1)  computes an offset/length purely from calldata words and, when `VMConfig.allowTvmSelfdestructRestriction()` is false, calls `extractBytesArray(words, offset, rawData)` [3](#0-2) , which does:
```
int len = words[offset].intValueSafe();
byte[][] bytesArray = new byte[len][];
for (int i = 0; i < len; i++) {
  int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
  int bytesLen = words[offset + bytesOffset + 1].intValueSafe();
  bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen);
}
```
`len` and `bytesLen` are taken straight from attacker-supplied calldata with no bound checks before being used for array allocation (`new byte[len][]`), array indexing (`words[offset + i + 1]`), and `Arrays.copyOfRange(data, offset, offset + len)` in `extractBytes` [4](#0-3) . A crafted `len`/`bytesLen`/`bytesOffset` can push `offset` past `words.length` or `offset+len` past `data.length`, throwing `ArrayIndexOutOfBoundsException` / `NegativeArraySizeException`.

Unlike `BatchValidateSign`, whose `execute()` wraps `doExecute()` in a `try { ... } catch (Throwable t) { return Pair.of(true, new byte[WORD_SIZE]); }` [5](#0-4) , `ValidateMultiSign.execute()` has no such top-level catch around the array-parsing logic [6](#0-5) ; only the later signature-recovery loop (`account.getPermissionById...`) is wrapped in `try/catch (Throwable t)` [7](#0-6) . The `isValidAbiEncoding` guard that would reject malformed calldata is gated behind `VMConfig.allowTvmOsaka()` [8](#0-7) , and the repository's own test explicitly documents that pre-activation this "legacy decoder" path can raise an uncaught `RuntimeException` [1](#0-0) .

### Impact Explanation
An uncaught `RuntimeException`/`ArrayIndexOutOfBoundsException` thrown from inside `extractBytesArray`/`extractBytes` during precompile execution propagates up out of `contract.execute(data)` in `Program.callToPrecompiledAddress` [9](#0-8) . Depending on how far up the call stack this is caught (or not) during transaction execution/block application, this can manifest as an unhandled exception during contract execution reachable by any account issuing a `TriggerSmartContract` transaction that CALLs address `0x...0a` (`validateMultiSignAddr`) with crafted calldata, when `allowTvmSelfdestructRestriction` is disabled and `allowTvmOsaka` is not yet active. If not gracefully converted into a reverted/failed transaction result somewhere in the trace/exception-handling layer, this risks node instability during block processing (a full crash is not proven here — this needs runtime verification, since `TransactionTrace`/`Runtime` layers may still catch generic `RuntimeException`).

### Likelihood Explanation
Reachability requires only `VMConfig.allowTvmSolidity059()` (gating `validateMultiSign` availability) to be active while `allowTvmSelfdestructRestriction` and `allowTvmOsaka` (the TIP-854 fix) are not yet activated — a plausible historical/hard-fork-window configuration on some networks, as the codebase's own tests were written specifically to document and later fix this exact gap (`testTip854PreActivationNoOp`, `testTip854RejectsMalformedCalldata`). Constructing calldata with an out-of-range offset/length requires no special privilege beyond broadcasting a transaction that CALLs the precompile address.

### Recommendation
Add an unconditional bounds/shape validation (equivalent to `isValidAbiEncoding`) at the top of `ValidateMultiSign.execute()` independent of `VMConfig.allowTvmOsaka()`, and wrap the entire `execute()` body (not just the signature-recovery loop) in a `try/catch(Throwable)` that returns `Pair.of(true, DATA_FALSE)` on any parsing failure, matching the containment already implemented in `BatchValidateSign.execute()`. Also add explicit bounds checks in `extractBytesArray`/`extractBytes32Array`/`extractSigArray` before using calldata-derived lengths/offsets to index `words[]` or slice `data`.

### Proof of Concept
Send a `TriggerSmartContract` transaction whose EVM bytecode performs a `CALL` to precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` (`validateMultiSign`) with `allowTvmSelfdestructRestriction` disabled and `allowTvmOsaka` disabled, using calldata shaped like the ABI header `(address, uint256, bytes32, bytes[])` but with the offset word for the `bytes[]` array (word index 3) pointing to a `sigArraySize` value, or an inner signature length/offset value, that is negative or larger than `Integer.MAX_VALUE / WORD_SIZE`, causing `extractBytesArray` to throw `NegativeArraySizeException` or `ArrayIndexOutOfBoundsException` uncaught by `ValidateMultiSign.execute()`. This is exactly the scenario asserted (and only fixed post-TIP-854) in `testTip854PreActivationNoOp` [10](#0-9) .

### Citations

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L244-260)
```java
  // TIP-854: before activation, malformed calldata reaches the legacy decoder.
  // Assert the guard is not taken — this precompile has no outer catch, so a
  // too-short input raises inside the decoder; that is the documented
  // pre-activation failure mode the TIP explicitly preserves.
  @Test
  public void testTip854PreActivationNoOp() {
    VMConfig.initAllowTvmOsaka(0);
    contract.setRepository(RepositoryImpl.createRoot(StoreFactory.getInstance()));
    try {
      Pair<Boolean, byte[]> ret = contract.execute(new byte[(5 + 1) * 32]);
      // If the decoder happened to handle it without raising, we must not have
      // taken the post-activation reject path (false, empty).
      Assert.assertNotSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
    } catch (RuntimeException expectedLegacyBehaviour) {
      // Pre-activation: decoder may throw — this is the existing behaviour.
    }
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1118)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1154)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1752-1766)
```java
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
        returnDataBuffer = out.getRight();
        deposit.commit();
      } else {
        // spend all energy on failure, push zero and revert state changes
        this.refundEnergy(0, CALL_PRE_COMPILED);
        this.stackPushZero();
        if (Objects.nonNull(this.result.getException())) {
          throw result.getException();
        }
      }
```
