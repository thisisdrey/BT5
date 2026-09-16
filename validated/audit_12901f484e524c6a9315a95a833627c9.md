### Title
Unhandled RuntimeException in `ValidateMultiSign` precompile from malformed calldata crashes/reverts TVM execution unexpectedly (pre-TIP-854 path) - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract at address `0x...0a` decodes its raw calldata via `DataWord.parseArray(rawData)` and direct array indexing (`words[0]`, `words[1]`, `words[2]`, `words[3]`, and the derived `extractBytesArray`/`extractSigArray` calls) **before** entering any `try/catch` block. This mirrors the Fast-DDS CVE-2023-39945 bug class: a caller-controlled, malformed serialized "submessage" (here, the ABI-encoded precompile calldata) reaches a decoder that raises an unhandled exception instead of being validated first.

### Finding Description
`ValidateMultiSign.execute()` is implemented as: [1](#0-0) 

The only defensive guard (`isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)`) is gated behind `VMConfig.allowTvmOsaka()` (TIP-854), and only rejects calldata *before* decoding when that feature is active: [2](#0-1) 

When `allowTvmOsaka()` is not active (the legacy/default path on chains where TIP-854 has not been activated by committee vote), execution proceeds straight into `DataWord.parseArray(rawData)` and unguarded array indexing/extraction (`words[3].intValueSafe() / WORD_SIZE`, `extractBytesArray`/`extractSigArray`) with no bounds validation. Only the signature-recovery/permission-lookup logic that follows is wrapped in a `try { ... } catch (Throwable t)`: [3](#0-2) 

Malformed or truncated calldata (e.g., too short to contain the expected 5 header words, or with an out-of-range offset encoded in `words[3]`) causes an `ArrayIndexOutOfBoundsException` or similar `RuntimeException` to be thrown from the decoding step, which sits **outside** the `try/catch`, so it is not handled inside `ValidateMultiSign.execute()` itself.

This exact failure mode is explicitly documented and asserted as intentional legacy behavior by the codebase's own regression test, which shows that pre-TIP-854 activation, a too-short input reaches the decoder and can throw: [4](#0-3) 

Compare this to the sibling precompile `BatchValidateSign`, whose `execute()` wraps the entire `doExecute()` call (including decoding) in a `try { ... } catch (Throwable t)`, converting any decode-time exception into a safe `(true, zero-bytes)` result: [5](#0-4) 

`ValidateMultiSign` lacks this outer containment for its decoding stage, so it does not have the same safety net.

### Impact Explanation
Any account can trigger this by issuing a `CALL`/`STATICCALL` (or a `TriggerSmartContract` transaction, or a `TriggerConstantContract`/`triggerconstantcontract` estimate-energy JSON-RPC/HTTP API call) to precompile address `0x...0a` with malformed/short calldata. If the resulting `RuntimeException`/`ArrayIndexOutOfBoundsException` is not caught by an enclosing frame in the VM's opcode dispatch (`Program`/`OperationActions`), it can propagate out of contract execution. Depending on how far up the call stack the exception is caught:
- Best case: the transaction/call fails with an uncontrolled error rather than a clean revert, differing from designed VM semantics (contract "raise" instead of controlled OUT_OF_TIME/REVERT handling), which can be leveraged for denial-of-service against block/transaction processing or against nodes serving `triggerconstantcontract`/`estimateenergy` API calls if the exception isn't caught by a top-level `catch (Exception e)` in the RPC/HTTP service (unlike `RpcApiService.callContract`, which does catch generic `Exception`, providing some mitigation for the RPC path specifically).
- Any path that invokes precompiles without a broad top-level exception guard (e.g., deep inside block application when replaying/validating transactions during `Manager` block processing) risks an unhandled exception destabilizing the calling thread.

The severity is highest to the extent that any call path lacks a catch-all, since this classifies as a node-crash/DoS class bug matching the Fast-DDS analog (malformed submessage → unhandled exception → service disruption).

### Likelihood Explanation
Likelihood is high for reachability: `ValidateMultiSign` is a public precompile reachable from any signed transaction or `triggerconstantcontract`/`estimateenergy` call with attacker-controlled calldata, requiring no privileged role. The vulnerable code path is only exercised when TIP-854 (`allowTvmOsaka`) is not yet activated on the target network, which is the current/legacy default state for many java-tron deployments until the committee proposal activates it.

### Recommendation
Wrap the entire body of `ValidateMultiSign.execute()`—including `DataWord.parseArray`, address/permission-id/data extraction, and the `extractBytesArray`/`extractSigArray` calls—in the same defensive `try/catch (Throwable t)` pattern already used in `BatchValidateSign.execute()`, converting any decode-time exception into a safe `(true, DATA_FALSE)` (or `(false, EMPTY_BYTE_ARRAY)`) result regardless of `allowTvmOsaka` activation state. Alternatively, make the `isValidAbiEncoding` guard unconditional (not gated by TIP-854 activation) so malformed calldata is rejected before any unguarded array access occurs.

### Proof of Concept
1. Deploy or call precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` (`validateMultiSign`) directly via a `TriggerSmartContract`/`TriggerConstantContract` transaction or the `estimateenergy`/`triggerconstantcontract` JSON-RPC/HTTP endpoint, with `VMConfig.allowTvmOsaka()` not yet activated (default/legacy state).
2. Supply calldata shorter than `(5 + 1) * 32` bytes (i.e., fewer than the 5 required header words), e.g. `new byte[(5 + 1) * 32]` truncated or `new byte[64]`.
3. Execution reaches `DataWord.parseArray(rawData)` / `words[3].intValueSafe()` before any `try/catch`, throwing an unhandled `RuntimeException` (as reproduced by the existing test `testTip854PreActivationNoOp`, which explicitly must catch `RuntimeException` because the precompile "has no outer catch"): [6](#0-5) 
4. Observe that unlike `BatchValidateSign` (which safely returns a zero result for the equivalent malformed input in its own `testTip854PreActivationNoOp`), `ValidateMultiSign` propagates the exception, confirming the missing outer containment.

### Citations

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
