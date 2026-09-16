### Title
Unhandled exception (assertion-style crash) in `ValidateMultiSign` precompiled contract on malformed calldata before TIP-854 activation - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2016-9390 describes an assertion failure/crash in JasPer caused by a crafted, malformed input reaching a size-check that was not properly hardened, causing the process to abort. The closest reachable analog in java-tron is the `ValidateMultiSign` (and related `BatchValidateSign`) TVM precompiled contract, whose ABI decoding logic (`DataWord.parseArray`, `words[...]`, `extractSigArray`/`extractBytesArray`) operates directly on attacker-controlled `CALL`/`STATICCALL` data without a length/shape guard, and is not wrapped in a try/catch for the decoding phase.

### Finding Description
`ValidateMultiSign.execute(byte[] rawData)` immediately parses `rawData` into `DataWord[] words` and indexes into it (`words[0]`, `words[1]`, `words[2]`, `words[3]`, and `words[words[3].intValueSafe() / WORD_SIZE]`) before any length validation occurs: [1](#0-0) 

The only outer `try/catch(Throwable t)` in this method wraps the signature-verification/weight-calculation block (lines 1082-1117), not the initial ABI decoding (lines 1057-1074): [2](#0-1) 

A short/misaligned `rawData` payload (e.g., fewer than 5 32-byte words, or a size field that indexes beyond the array bounds) causes `words[...]` array indexing to throw an uncaught `ArrayIndexOutOfBoundsException` or similar `RuntimeException` during decoding, which is not caught by the narrower `try/catch(Throwable t)` block that only guards the later signature-loop.

The repository's own tests confirm this exact failure mode was a known, documented issue addressed by "TIP-854": tests explicitly state that pre-activation malformed calldata "raises inside the decoder" because "this precompile has no outer catch": [3](#0-2) 

Post-TIP-854 (when `VMConfig.allowTvmOsaka()` is enabled), an explicit `isValidAbiEncoding` shape check rejects malformed calldata before decoding occurs, per the test in `ValidateMultiSignContractTest.testTip854RejectsMalformedCalldata`: [4](#0-3) 

However, this guard is gated behind `VMConfig.allowTvmOsaka()`, meaning on any network/height where that TVM upgrade is not yet activated (or has not yet been activated by committee proposal on a given chain), the raw decoder path in `ValidateMultiSign.execute` remains reachable and unguarded: [5](#0-4) 

### Impact Explanation
An uncaught `RuntimeException` thrown from inside a precompiled contract's `execute()` during TVM opcode dispatch (e.g., `CALL`/`STATICCALL` to the `ValidateMultiSign` address) that is not properly caught by the calling VM/opcode-handling layer can propagate up and abort transaction processing unexpectedly, potentially causing inconsistent state handling across nodes, or a crash/halt of the node process depending on how far up the exception propagates before being caught. This maps to the CVE's "denial of service via crafted input triggering an internal assertion/crash" bug class. Any unprivileged party who can deploy or call a smart contract that in turn calls the `ValidateMultiSign` precompiled address with malformed data can trigger this.

### Likelihood Explanation
Likelihood is limited by TIP-854 activation status: on chains where `allowTvmOsaka` is already active, the `isValidAbiEncoding` guard fully closes this path, as confirmed by the passing `testTip854RejectsMalformedCalldata` test. On chains/heights where this TVM feature has not yet been activated, the raw decoder path is fully reachable by any account able to submit a `TriggerSmartContract` transaction invoking the `ValidateMultiSign` address (`0x...09`) with crafted short calldata — no special privilege required, and the reachable path is confirmed by test comments in the repository itself acknowledging "this precompile has no outer catch" as "the existing behaviour."

### Recommendation
Verify at what block height/whether `allowTvmOsaka` (TIP-854) is activated on the target network. If not yet activated on some deployment, backport the `isValidAbiEncoding` shape-validation guard (or wrap the entire decoding block, not just the signature-verification loop, in a `try/catch` that returns `Pair.of(false, EMPTY_BYTE_ARRAY)` on any decoding failure) so that malformed calldata never reaches raw array indexing regardless of feature-flag state. Apply the same broadened try/catch to `BatchValidateSign` and any other precompiled contract that indexes into `DataWord[]` arrays without upfront bounds checking.

### Proof of Concept
1. On a chain/height where `allowTvmOsaka` (TIP-854) has not been activated, deploy a trivial contract that performs a low-level `call`/`staticcall` to the `ValidateMultiSign` precompiled address (address ending in `...09`), forwarding attacker-supplied `bytes` as calldata.
2. Craft calldata shorter than `5 * 32` bytes (fewer than the static ABI header words), e.g., `new byte[64]`.
3. Invoke the contract via a standard `TriggerSmartContractTransaction`. Inside `ValidateMultiSign.execute`, `DataWord.parseArray(rawData)` followed by `words[3]` indexing throws an uncaught `ArrayIndexOutOfBoundsException`, since this code path executes before the outer `try/catch(Throwable t)` block (lines 1082–1117) is entered — matching the documented pre-activation failure mode noted in `ValidateMultiSignContractTest.testTip854PreActivationNoOp`. [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1074)
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L158-192)
```java
  // TIP-854: after activation, validateMultiSign (H=5, I=5) must reject calldata
  // whose byte length is incompatible with the (words - 5) / 5 shape the per-call
  // energy formula already assumes, returning (false, empty).
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
