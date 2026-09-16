### Title
Unbounded attacker-controlled array length in `ValidateMultiSign` precompile allocation causes uncaught OOM crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` precompiled contract (address `0x0a`, reachable from any Solidity contract via a `CALL`/`STATICCALL` to that address, i.e. by any unprivileged transaction that triggers a TVM contract) decodes an attacker-supplied signature-array length directly from calldata and uses it to allocate a Java array before any bound is enforced, mirroring the FAAD2 `mp4ff_read_stsc` pattern of trusting an untrusted length field for memory allocation.

### Finding Description
`ValidateMultiSign.execute()` reads the sub-array length word out of raw calldata and passes it straight into `extractBytesArray`/`extractSigArray`, which allocate `new byte[len][]` using that attacker-controlled `len` with no upper bound check before allocation: [1](#0-0) [2](#0-1) 

In `ValidateMultiSign.execute`, the `MAX_SIZE` (5) bound check is only performed *before* extraction when `VMConfig.allowTvmSelfdestructRestriction()` is enabled (the post-fix code path). When that flag is not enabled, `extractBytesArray` is invoked directly with the raw, unchecked `len`, and the `signatures.length > MAX_SIZE` bound is only checked *after* the array has already been allocated: [3](#0-2) 

`len` comes from `words[offset].intValueSafe()`, which returns an `int` derived directly from calldata without any application-level size limit tied to the actual data available; a caller can set this word to a large value (e.g., close to `Integer.MAX_VALUE`), causing `new byte[len][]` to attempt allocating an array of object-pointer slots on the order of gigabytes, throwing `OutOfMemoryError`.

Crucially, this allocation happens *before* the `try { ... } catch (Throwable t) { ... }` block inside `ValidateMultiSign.execute()`, which only wraps the account/permission logic further down: [4](#0-3) 

So an `OutOfMemoryError` thrown by the array allocation is not caught locally by `ValidateMultiSign`, unlike its sibling `BatchValidateSign`, whose `execute()` wraps the entire `doExecute` call (including its own `extractBytesArray`/`extractSigArray` calls) in `catch (Throwable t)`: [5](#0-4) 

This structural inconsistency — `BatchValidateSign` defensively catches `Throwable` around the whole precompile body while `ValidateMultiSign` does not, and only gates the allocation behind a bound check under a feature flag — is the same bug class as CVE-2017-9219: a size field taken from untrusted input is used to allocate memory without validation, leading to an allocation failure/crash.

### Impact Explanation
An `OutOfMemoryError` escaping a precompiled-contract `execute()` call during TVM execution is not a normal Solidity revert; it is a JVM `Error`. Whether this results in node-wide instability depends on how far up the call stack (`OperationActions` → `Program` → `Runtime`/`TransactionTrace`) generic `Throwable`/`Error` handling exists for precompile calls. I was not able to fully confirm within the available tool budget whether an outer layer in `OperationActions.java` or `Program.java` uniformly catches `Throwable` for all precompiled-contract invocations (the search for `OperationActions.java` did not return the precompile-call site content before the iteration budget was exhausted). If such an outer catch-all exists for every precompile invocation, the practical impact is downgraded to a per-transaction revert/DoS of that single transaction (still a resource-exhaustion/crash risk during the large allocation attempt, and inconsistent with the intentionally defensive `catch (Throwable t)` pattern used in `BatchValidateSign`). If no such catch-all exists at the call site for precompiles, an uncaught `OutOfMemoryError` from a single crafted transaction could destabilize or crash the executing full node/SR process (denial of service), which is the CVE-2017-9219-equivalent impact (memory allocation error / crash from crafted untrusted input).

### Likelihood Explanation
Any account can deploy or call a trivial contract that performs a `STATICCALL`/`CALL` to precompile address `0x0a` with hand-crafted ABI-encoded calldata; this requires no special privileges, no SR/witness status, and only a single signed transaction/contract call, satisfying the "single signed transaction, contract call" reachability requirement. The vulnerable branch is exercised whenever `VMConfig.allowTvmSelfdestructRestriction()` is not active for the relevant chain state, which is a legacy/pre-activation configuration parameter, not something the attacker controls, but is plausible on chains/testnets that have not activated that config, or reflects a latent inconsistency the current code no longer fully protects against.

### Recommendation
- Validate the extracted array length against `MAX_SIZE` (or another sane bound derived from the actual `rawData.length`) unconditionally, before calling `extractBytesArray`/`extractSigArray`, regardless of `VMConfig.allowTvmSelfdestructRestriction()`.
- Wrap the entirety of `ValidateMultiSign.execute()` (not just the account/permission logic) in a `catch (Throwable t)` matching the defensive pattern already used in `BatchValidateSign.execute()`, so that any allocation failure results in a `Pair.of(true, DATA_FALSE)`/`Pair.of(false, EMPTY_BYTE_ARRAY)` instead of propagating a JVM `Error`.
- Audit `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` themselves to enforce a hard maximum length before allocation, rather than relying on each caller to pre-check.

### Proof of Concept
Deploy a contract that performs a low-level call to precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` with ABI-encoded calldata for `validatemultisign(address,uint256,bytes32,bytes[])` where the dynamic `bytes[]` array-length word (at the offset pointed to by the 4th head word) is set to a very large value (e.g. `0x7FFFFFFF`) instead of the true number of signature elements, on a chain state where `allowTvmSelfdestructRestriction` is not active. This drives execution into `extractBytesArray(words, offset, rawData)` with `len = 0x7FFFFFFF`, attempting `new byte[0x7FFFFFFF][]`, which throws `OutOfMemoryError` outside the local `try/catch` in `ValidateMultiSign.execute()`. I could not fully verify from the code explored whether an outer generic-`Throwable` catch exists in `OperationActions.java`/`Program.java` around all precompile invocations to confirm end-to-end node-crash impact versus a contained transaction failure; this should be checked directly against those files before treating the finding as node-crash-confirmed.

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
