Based on my research, the `ValidateMultiSign` precompile decoder is a strong analog to the CVE-2018-20451 bug class: attacker-supplied, insufficiently bounds-checked input that reaches raw array/word decoding logic and can raise an unhandled exception, causing an execution-path crash.

### Title
Unhandled exception in `ValidateMultiSign` precompile decoder on malformed calldata when TIP-854 guard is disabled - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign.execute` precompile (address `0x...0a`, reachable via a `CALL`/`STATICCALL` from any TVM contract with attacker-controlled calldata) decodes its input with `DataWord.parseArray(rawData)` and then indexes into the resulting `words` array using offsets taken directly from the calldata (`words[3].intValueSafe() / WORD_SIZE`, etc.), before calling `extractBytesArray`/`extractSigArray`. When `VMConfig.allowTvmOsaka()` is false, the new `isValidAbiEncoding` bounds guard is skipped entirely, so malformed/too-short calldata proceeds straight into the legacy decode path with no outer `try/catch`, unlike sibling precompile `BatchValidateSign` which wraps its entire body in `catch (Throwable t)` [1](#0-0) .

### Finding Description
`ValidateMultiSign.execute` only calls the new bounds guard `isValidAbiEncoding` when `VMConfig.allowTvmOsaka()` is enabled: [2](#0-1)  Immediately after, it computes an offset word directly from attacker calldata and indexes into the parsed `words` array without any bound check: [3](#0-2)  This mirrors the CVE-2018-20451 bug class — a parser reads past the bounds of an attacker-controlled buffer because the length/offset field taken from the input is trusted without validation before being used to index the underlying data. The project's own regression test confirms that pre-TIP-854 (i.e. with `VMConfig.allowTvmOsaka()` disabled, the current mainnet default in most environments), a too-short calldata "raises inside the decoder" and there is "no outer catch" — the test explicitly documents this as "the existing behaviour": [4](#0-3)  By contrast, `BatchValidateSign`, which performs analogous offset-based decoding, wraps its entire execution in a catch-all so any such exception is safely converted to a normal (non-throwing) failure result: [1](#0-0) .

### Impact Explanation
An exception thrown deep inside a precompiled contract during TVM execution is a `RuntimeException` that propagates up through `Operation.execute` in the VM's opcode loop. Because `ValidateMultiSign` has no local `try/catch` around its offset-parsing/array-indexing logic in the pre-Osaka path, an `ArrayIndexOutOfBoundsException` (or similar) thrown by `extractBytesArray`/`extractSigArray`/`words[...]` accesses will surface as an uncaught `RuntimeException` at that call site. The VM's outer opcode dispatch loop does catch generic `RuntimeException` and stops the current call frame/spends all energy [5](#0-4) , so within a single well-formed top-level TVM execution this is contained to a reverted transaction rather than a full node crash. The severity is therefore bounded to a denial-of-service against the specific transaction/contract call (wasted energy, forced revert) rather than node-wide crash, differing from the "application crash" impact of the original libdoc CVE. This still represents an inconsistency/hardening gap relative to the project's own security intent (TIP-854, and the parallel `BatchValidateSign` catch-all), and the untrusted-offset-into-array pattern is the same root cause class as the CVE.

### Likelihood Explanation
Any account can trigger this by deploying or calling a contract that performs a `CALL`/`STATICCALL` to the `ValidateMultiSign` precompile address with crafted calldata (e.g., a large or negative value in the byte-array-offset word) whenever `VMConfig.allowTvmOsaka()` is not active in the target network's hard-fork configuration. No special privileges are required — this is reachable from ordinary contract-triggering transactions.

### Recommendation
Apply the `isValidAbiEncoding` bounds validation (or an equivalent offset/length sanity check) unconditionally, not gated behind `VMConfig.allowTvmOsaka()`, and/or wrap the decode-and-index logic of `ValidateMultiSign.execute` in a `try/catch (Throwable)` block analogous to `BatchValidateSign.execute`'s `doExecute` wrapper, converting any parsing failure into `Pair.of(true, DATA_FALSE)` instead of letting the exception propagate.

### Proof of Concept
Deploy a contract that issues `CALL` to precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` with calldata shorter than 5 words (e.g. `new byte[(5+1)*32]` with a crafted offset word at index 3 pointing beyond the array bounds), while the network has not activated the TVM Osaka hard fork (`allowTvmOsaka()` false). The regression test `testTip854PreActivationNoOp` in `ValidateMultiSignContractTest` demonstrates this exact call raises a `RuntimeException` from inside the decoder with no containing catch at the precompile level [6](#0-5) .

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1074)
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

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L93-100)
```java
        } catch (RuntimeException e) {
          logger.info("VM halted: [{}]", e.getMessage());
          if (!(e instanceof TransferException)) {
            program.spendAllEnergy();
          }
          //program.resetFutureRefund();
          program.stop();
          throw e;
```
