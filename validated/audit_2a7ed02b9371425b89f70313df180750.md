## Analysis

The CVE-2017-14858 bug class — a crafted input driving an under-validated length field into a heap allocation/copy, causing a crash — has a concrete analog in java-tron's ABI-array extraction helpers used by TVM precompiles reachable from any contract call.

### Title
Unbounded, attacker-controlled array-length in `ValidateMultiSign`/`BatchValidateSign` precompiles causes uncaught `OutOfMemoryError` (Denial of Service) — (`File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.extractBytesArray` and `extractSigArray` read an array-length word directly from attacker-supplied `data`/`words` (via `words[offset].intValueSafe()`) and immediately allocate `new byte[len][]` before any sanity bound is enforced in some code paths. [1](#0-0) [2](#0-1) 

### Finding Description
In `ValidateMultiSign.execute`, the `sigArraySize` bound check (`MAX_SIZE`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [3](#0-2) 

When that feature flag is not active, `extractBytesArray(words, offset, rawData)` is invoked directly, with `len` taken unchecked from `words[offset].intValueSafe()` — a 32-byte word fully controlled by the calling contract's calldata — and used to size a `byte[len][]` allocation: [1](#0-0) 

`intValueSafe()` can return a value up to `Integer.MAX_VALUE`, so a crafted `TRIGGERSMARTCONTRACT` call that reaches this precompile with such a word forces an extremely large `byte[][]` allocation. Critically, this call in `ValidateMultiSign.execute` sits **outside** any try/catch (the `try` block only wraps the later permission-weight logic at lines 1082–1117), so the resulting `OutOfMemoryError` propagates uncaught out of the precompile call. [4](#0-3) [5](#0-4) 

This is directly analogous to the Exiv2 `l2Data` bug: a length field taken from untrusted input is trusted to size a buffer/array without an upper bound, leading to a crash from crafted input.

### Impact Explanation
An uncaught `OutOfMemoryError` during transaction execution can destabilize the JVM heap for the entire node process (other threads may fail allocations concurrently), not just the calling transaction/thread. This matches the "node crash or halt" acceptance criterion — an unprivileged contract caller can trigger it purely through a `CALL`/`TRIGGERSMARTCONTRACT` reaching the `ValidateMultiSign` (or `BatchValidateSign`, which has a similar unguarded path before its own `MAX_SIZE` check when the flag is off) precompile address.

### Likelihood Explanation
Reachability requires only a normal signed transaction invoking the precompiled contract address for `ValidateMultiSign`/`BatchValidateSign` with crafted calldata — no special privileges. The severity is gated by whether `VMConfig.allowTvmSelfdestructRestriction()` (the TIP-854-related flag) is currently active on the target network; where it is active, the `MAX_SIZE` check at lines 1066–1071 mitigates this specific path. I could not verify from the indexed code whether this flag is enabled by default on mainnet at the current block height — this needs confirmation via chain parameter state, which is outside what the index can show.

### Recommendation
Enforce the `MAX_SIZE` bound check unconditionally in `extractBytesArray`/`extractSigArray` (or in their callers) before allocating `new byte[len][]`, regardless of the `allowTvmSelfdestructRestriction()` flag state, and wrap the entire `ValidateMultiSign.execute`/`BatchValidateSign.doExecute` bodies (not just the inner block) in a catch that also handles `OutOfMemoryError`/`Throwable` at the allocation site itself.

### Proof of Concept
Craft calldata for `ValidateMultiSign` (or `BatchValidateSign`) where the ABI header word pointing to the signature array's length field is set to a very large value (e.g., `0x7fffffff`), and submit it via a normal `TriggerSmartContract` transaction calling the precompile address, on a network/height where `allowTvmSelfdestructRestriction()` is not yet active. The call to `extractBytesArray`/`extractSigArray` allocates a huge `byte[len][]` before any size guard, raising an uncaught `OutOfMemoryError` from `PrecompiledContracts.execute`. [6](#0-5)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1077)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1119)
```java
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
