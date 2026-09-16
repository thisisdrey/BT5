### Title
ValidateMultiSign precompile throws uncaught `ArrayIndexOutOfBoundsException` on short calldata when TIP-854 guard is inactive - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The kernel report describes a class of bug where a buffer-processing routine failed to bail out safely on "unreadable"/short input and instead let the negative/garbage result propagate into logic that assumed well-formed data (fixed by adding explicit shape/length checks before further processing). The same bug class exists in java-tron's `ValidateMultiSign` TVM precompile: its ABI-shape guard (`isValidAbiEncoding`) is applied only when the `allowTvmOsaka` (TIP-854) feature flag is active, leaving the pre-activation code path exposed to malformed/short calldata that is used unchecked.

### Finding Description
`ValidateMultiSign.execute(byte[] rawData)` only validates that `rawData` has the expected word-aligned shape when `VMConfig.allowTvmOsaka()` is true: [1](#0-0) 

If `allowTvmOsaka` is not active, the guard is skipped entirely and the code immediately calls `DataWord.parseArray(rawData)` and indexes into `words[0]`, `words[1]`, `words[2]`, `words[3]` without any length check: [2](#0-1) 

`parseArray` sizes the returned array as `data.length / WORD_SIZE`, so any `rawData` shorter than 128 bytes (4 words) yields an array too small to satisfy the subsequent `words[3]` access, and calldata shorter than 32 bytes (or empty) fails on `words[0]` itself — throwing an unguarded `ArrayIndexOutOfBoundsException`. This exception occurs outside the local `try { ... } catch (Throwable t)` block that only wraps the account/permission-weight logic further down: [3](#0-2) 

By contrast, the sibling precompile `BatchValidateSign` wraps its entire body (`doExecute`) in a `try/catch (Throwable t)` that always returns a safe fallback result regardless of the Osaka flag: [4](#0-3) 

`ValidateMultiSign` lacks this outer safety net, so it relies solely on the ABI-shape guard, which is conditionally compiled behind `allowTvmOsaka`. This mirrors the kernel bug class precisely: a length/readability check was added to bail out safely, but it is not applied unconditionally on all code paths, leaving one path to process malformed input as if it were well-formed until it faults.

Additionally, `getEnergyForData` computes `cnt = (data.length / WORD_SIZE - 5) / 5` without a floor at zero; for short calldata this can produce a negative energy charge, which is a secondary consequence of the same missing length validation: [5](#0-4) 

### Impact Explanation
Any account can deploy or call a contract that invokes address `0x0000...0100` region for `validateMultiSign` (reachable via `CALL`/`STATICCALL` from arbitrary Solidity/TVM bytecode) with calldata shorter than 128 bytes. On a node/network where `allowTvmOsaka` (TIP-854) has not been activated, this throws an unguarded `ArrayIndexOutOfBoundsException` inside precompile execution. Depending on how far up the VM/transaction-execution call stack this runtime exception propagates before being caught, this can at minimum abort/blackhole the calling transaction unexpectedly, and if such exceptions are not funneled through the standard `Program.Exception`/energy-exhaustion handling used elsewhere in the VM, it risks an unhandled exception surfacing during block application in `Manager`, which is a node-crash/halt-class impact.

### Likelihood Explanation
Likelihood is high for triggering the exception: it requires only a single unprivileged transaction or a contract call with an intentionally short input to the `validateMultiSign` precompile address — no special permissions, keys, or two-step preconditions are needed. The severity of the ultimate impact (transaction-local revert vs. a wider node/block-processing halt) depends on exception handling further up the executor/VM call chain, which I could not fully trace with the tools available in this session; this is the main open uncertainty in this analog.

### Recommendation
Make the ABI-shape/length validation in `ValidateMultiSign.execute` unconditional (not gated behind `VMConfig.allowTvmOsaka()`), or, at minimum, wrap the entire `execute` method body in a `try/catch (Throwable t)` as already done in `BatchValidateSign.execute`, returning `Pair.of(true, DATA_FALSE)` (or `Pair.of(false, EMPTY_BYTE_ARRAY)`) on any parsing failure. Also clamp `getEnergyForData` to a non-negative minimum for undersized `data`.

### Proof of Concept
1. On a java-tron network where `allowTvmOsaka` has not been activated, deploy a contract that performs a low-level `staticcall`/`call` to the `validateMultiSign` precompile address with calldata of length `0` (or any length `< 128` bytes).
2. Broadcast the transaction invoking this contract call.
3. `ValidateMultiSign.execute` skips the `isValidAbiEncoding` check (flag disabled), calls `DataWord.parseArray(rawData)`, and then accesses `words[0]`/`words[3]`, throwing `ArrayIndexOutOfBoundsException` unguarded by any local `try/catch`, unlike the equivalent `BatchValidateSign` path which safely returns a default value for the same malformed-input class (confirmed by the existing `testTip854RejectsMalformedCalldata` unit tests, which only assert the safe behavior when `VMConfig.initAllowTvmOsaka(1)` is explicitly set). [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1060)
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

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L133-141)
```java
  public static DataWord[] parseArray(byte[] data) {
    int len = data.length / WORD_SIZE;
    DataWord[] words = new DataWord[len];
    for (int i = 0; i < len; i++) {
      byte[] bytes = Arrays.copyOfRange(data, i * WORD_SIZE, (i + 1) * WORD_SIZE);
      words[i] = new DataWord(bytes);
    }
    return words;
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L161-192)
```java
  @Test
  public void testTip854RejectsMalformedCalldata() {
    VMConfig.initAllowTvmOsaka(1);
    try {
      // Bucket 1: 32-aligned head + sub-word trailing bytes (r=1, r=31).
      for (int r : new int[]{1, 31}) {
        byte[] data = new byte[(5 + 5) * 32 + r];
        Pair<Boolean, byte[]> ret = contract.execute(data);
        Assert.assertFalse("non-32-aligned len=" + data.length, ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Bucket 2: fewer than the static head's 5 words.
      for (int bytes : new int[]{0, 32, 64, 96, 128}) {
        Pair<Boolean, byte[]> ret = contract.execute(new byte[bytes]);
        Assert.assertFalse("len=" + bytes + " < 5 words", ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Bucket 3: 32-aligned but tail not a multiple of I=5 words (k = 1..4).
      for (int k = 1; k <= 4; k++) {
        byte[] data = new byte[(5 + k) * 32];
        Pair<Boolean, byte[]> ret = contract.execute(data);
        Assert.assertFalse("aligned bad-tail k=" + k, ret.getLeft());
        Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
      }
      // Null calldata: explicit spec clause.
      Pair<Boolean, byte[]> ret = contract.execute(null);
      Assert.assertFalse("null calldata", ret.getLeft());
      Assert.assertSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
    } finally {
      VMConfig.initAllowTvmOsaka(0);
    }
  }
```
