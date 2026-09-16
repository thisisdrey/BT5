### Title
Unbounded array allocation from attacker-controlled length in `ValidateMultiSign` precompile causes uncaught `OutOfMemoryError` - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` TVM precompiled contract (address `0x0a`, reachable via `validatemultisign(address,uint256,bytes32,bytes[])`) decodes a caller-supplied ABI length word and uses it directly as a Java array size (`new byte[len][]`) with no upper bound, unless the `allowTvmSelfdestructRestriction` feature is active. This mirrors the Helm `strvals` bug class: an untrusted length field from attacker input is used to allocate a huge data structure, causing an unrecoverable out-of-memory condition.

### Finding Description
`extractBytesArray`/`extractSigArray`/`extractBytes32Array` read a length directly from calldata via `DataWord.intValueSafe()` and allocate an array of that size without any cap: [1](#0-0) 

`DataWord.intValueSafe()` clamps any value that occupies more than 4 bytes (or decodes negative) to `Integer.MAX_VALUE`, so a single crafted 32-byte word trivially yields `len = Integer.MAX_VALUE`: [2](#0-1) 

In `ValidateMultiSign.execute`, the size guard (`sigArraySize > MAX_SIZE`) is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is enabled; on the legacy branch `extractBytesArray` is invoked directly with the unbounded length, and this call sits outside any try/catch in the method: [3](#0-2) 

`new byte[Integer.MAX_VALUE][]` requests roughly 8–17 GB for the reference array alone and immediately throws `OutOfMemoryError`, which is an `Error`, not an `Exception`. The only surrounding try/catch in this method wraps the later permission-checking block and would not catch a failure that occurs during array construction before that block is reached: [4](#0-3) 

By contrast, the sibling `BatchValidateSign` precompile wraps its whole execution in a `catch (Throwable t)`, which does absorb this class of failure: [5](#0-4) 

`ValidateMultiSign` has no equivalent outer `Throwable` catch around the array-extraction call, so if `allowTvmSelfdestructRestriction` is not active (e.g., not yet activated by committee on a given network, or in any deployment where this proposal was never turned on), the uncaught `OutOfMemoryError` propagates out of the precompile and up through TVM execution.

### Impact Explanation
An uncaught `OutOfMemoryError` thrown mid-transaction-execution is a JVM `Error`, which is generally not handled by the ordinary `Exception`-based revert/catch logic used for TVM execution failures. Depending on how far up the call stack it is finally caught (if at all), this can crash the transaction-processing/block-application thread, and because heap pressure from the failed huge allocation can also destabilize the whole JVM, it creates a node crash / denial-of-service risk reachable from an ordinary signed transaction or a `TriggerSmartContract` call that reaches the `0x0a` precompile address (directly or via a deployed contract that `CALL`s it). This matches the “node crash or halt” impact class called out in the validation criteria.

### Likelihood Explanation
The attack requires only a single, cheaply constructed transaction: a `bytes[]` ABI parameter whose length word is set to a large value (e.g., all-`0xFF` or any value with more than 4 significant bytes). No special privileges, staking, or witness/SR status are needed — any account able to broadcast a transaction or trigger a contract call can reach this path. The main uncertainty is whether `allowTvmSelfdestructRestriction` is active on the target network at the time of the attack; where it is inactive (or on any fork/testnet/private chain that has not activated this specific TIP), the code path is exercised exactly as shown with no length cap and no surrounding `Throwable` guard.

### Recommendation
- Add an explicit upper bound check on the decoded array length (matching the existing `MAX_SIZE` semantics already applied to `BatchValidateSign` and to the `allowTvmSelfdestructRestriction`-enabled branch of `ValidateMultiSign`) unconditionally, not gated behind a feature flag.
- Wrap `ValidateMultiSign.execute()` in a `Throwable`-based guard consistent with `BatchValidateSign`, so any allocation failure degrades gracefully (e.g., returns `DATA_FALSE`) instead of propagating an `Error`.
- Audit `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` to always bound `len` (e.g., cap at `MAX_SIZE` or a small constant) before allocation, independent of any governance-activated feature switch.

### Proof of Concept
1. Construct calldata for `validatemultisign(address,uint256,bytes32,bytes[])` where the ABI-encoded `bytes[]` length word (the word at the array offset) is set to a value that occupies more than 4 significant bytes, e.g. `0xFFFFFFFFFFFFFFFF...` (32 bytes of `0xFF`), rather than a legitimate small count.
2. Send this as calldata in a `TriggerSmartContract` transaction targeting a contract that forwards the call to precompile address `0x0a`, or directly interact through any code path that reaches `PrecompiledContracts.ValidateMultiSign.execute`.
3. On a network/build where `allowTvmSelfdestructRestriction` has not been activated, `words[offset].intValueSafe()` returns `Integer.MAX_VALUE`, `extractBytesArray` executes `new byte[Integer.MAX_VALUE][]`, and the resulting `OutOfMemoryError` is uncaught within `ValidateMultiSign.execute`, propagating out of the precompile.

Note: I could not fully trace whether an outer layer of `Program.java`/TVM interpreter catches generic `Error`/`Throwable` around precompile invocation before this reaches the block-application thread — this could not be confirmed within the available search budget, so the ultimate blast radius (isolated transaction failure vs. full node crash) carries some residual uncertainty and should be verified directly in a running node/test harness.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1078)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1118)
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

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
```java
  /**
   * In case of int overflow returns Integer.MAX_VALUE otherwise works as #intValue()
   */
  public int intValueSafe() {
    int bytesOccupied = bytesOccupied();
    int intValue = intValue();
    if (bytesOccupied > 4 || intValue < 0) {
      return Integer.MAX_VALUE;
    }
    return intValue;
  }
```
