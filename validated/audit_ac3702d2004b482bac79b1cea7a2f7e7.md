### Title
Unhandled exception in `ValidateMultiSign` precompile from unvalidated calldata offsets - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract, reachable via a TVM `CALL`/`STATICCALL` from any deployed smart contract, parses length/offset fields directly out of attacker-supplied calldata and indexes into a `DataWord[]` array without validating that the derived index is in bounds — mirroring the CVE-2026-21494 bug class in `CIccTagLut8::Validate()`, where a `Validate()` routine trusted an untrusted size/offset field and read/wrote outside the allocated buffer.

### Finding Description
`ValidateMultiSign.execute(byte[] rawData)` only performs a length/shape check (`isValidAbiEncoding`) when `VMConfig.allowTvmOsaka()` is enabled: [1](#0-0) 

When that feature flag is off (i.e. before the corresponding hardfork activates), execution proceeds straight to `DataWord.parseArray(rawData)` and then unconditionally reads `words[0]`, `words[1]`, `words[2]`, `words[3]`, and, when `allowTvmSelfdestructRestriction()` is enabled, computes `words[words[3].intValueSafe() / WORD_SIZE]` — an index entirely derived from attacker-controlled calldata with no bound check against `words.length`. Unlike its sibling `BatchValidateSign`, whose `execute()` wraps the equivalent logic in a `try { doExecute(data); } catch (Throwable t) { ... }` block, `ValidateMultiSign.execute()` has no such wrapper around this parsing/indexing logic — only the later permission-weight loop is guarded by a `try/catch(Throwable t)`: [2](#0-1) 

Sibling precompile `BatchValidateSign`, by contrast, explicitly catches everything from its equivalent parsing step: [3](#0-2) 

The project's own tests confirm this asymmetry and document it as a known "pre-activation" hazard fixed only behind the TIP‑854 flag: [4](#0-3) 

### Impact Explanation
A malformed/too-short calldata payload to the `validateMultiSign` precompile (e.g. fewer than 5 words, or fewer words than `words[3]/32` implies) causes `DataWord.parseArray` or the subsequent `words[n]` accesses to throw an unchecked `ArrayIndexOutOfBoundsException`, with no local `catch` to convert this into a normal EVM revert. Depending on how far up the exception propagates (through `Program.callToPrecompiledAddress` and transaction execution in the actuator layer), this can manifest as inconsistent transaction execution behavior across nodes, an unexpected propagation of a runtime exception out of transaction processing, or a crash/halt during block application if the exception is not caught by a higher-level generic handler. This is directly analogous to the referenced CVE's heap-buffer-overflow-via-unvalidated-length pattern, translated to Java's fail-fast bounds-checked arrays (index-out-of-range exception instead of memory corruption, but with the same untrusted-length root cause).

### Likelihood Explanation
`ValidateMultiSign` is a public precompiled contract address invokable by any account via a simple `CALL` from a deployed contract — no special privilege, deployment rights, or SR/witness status required. Triggering the malformed-input path only requires crafting calldata shorter than the expected ABI header, which is trivial for any contract deployer or transaction broadcaster to construct. The condition is gated by `VMConfig.allowTvmOsaka()`, so it is exploitable specifically while that TIP is not yet active on the target network.

### Recommendation
Wrap the entire body of `ValidateMultiSign.execute()` (not just the signature-verification loop) in the same defensive `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }` pattern used by `BatchValidateSign.execute()`, and/or make the `isValidAbiEncoding` shape check unconditional rather than gated behind `allowTvmOsaka()`, so malformed calldata is rejected deterministically regardless of hardfork activation state.

### Proof of Concept
1. Deploy a contract that performs a low-level `call` to the `ValidateMultiSign` precompile address with calldata shorter than `5 * 32` bytes (or with `words[3]` pointing past the end of the parsed `DataWord[]` array), while the network has not yet activated the TIP-854/Osaka flag.
2. Observe that `DataWord.parseArray(rawData)` / subsequent `words[n]` access throws an uncaught `ArrayIndexOutOfBoundsException` inside `ValidateMultiSign.execute()`, unlike the guarded `BatchValidateSign` path, matching the behavior explicitly exercised by `testTip854PreActivationNoOp` in `ValidateMultiSignContractTest.java`. [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1075)
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
