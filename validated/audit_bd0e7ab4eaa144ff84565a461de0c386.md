### Title
Unchecked attacker-controlled array index in `ValidateMultiSign`/`BatchValidateSign` TVM precompiles causes uncaught runtime exception before the TIP-854 guard activates - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The reported Solidity bug is an unchecked loop/array index (`indices[i]` used against `stakingContracts` without a length check). The closest reachable analog in java-tron is the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts, where an attacker-supplied word offset is used directly to index into the `words` array (parsed from raw call data) without any bounds validation, unless the `allowTvmOsaka` (TIP-854) hard-fork flag is active.

### Finding Description
Both precompiles decode call data into a `DataWord[] words` array and then compute an index from attacker-controlled data to read the length of a dynamic array segment:

```java
// ValidateMultiSign.execute
DataWord[] words = DataWord.parseArray(rawData);
byte[] address = words[0].toTronAddress();
int permissionId = words[1].intValueSafe();
byte[] data = words[2].getData();
...
int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
``` [1](#0-0) 

`words[3].intValueSafe() / WORD_SIZE` is fully attacker-controlled (any TVM contract can invoke this precompile with crafted calldata via a `CALL`/`STATICCALL` to the `ValidateMultiSign`/`BatchValidateSign` addresses). If this computed index is negative or exceeds `words.length` (or if `rawData` is too short so `words` has fewer than 4 elements), the array access throws an uncaught `ArrayIndexOutOfBoundsException`.

The only mitigation is `isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)`, which is gated behind `VMConfig.allowTvmOsaka()`: [2](#0-1) 

The project's own regression tests explicitly document that, prior to this hard-fork flag being active, there is no protection at all for `ValidateMultiSign`: [3](#0-2) 

> "before activation, malformed calldata reaches the legacy decoder... this precompile has no outer catch, so a too-short input raises inside the decoder; that is the documented pre-activation failure mode the TIP explicitly preserves."

`BatchValidateSign` has the analogous pattern (`words[words[1].intValueSafe() / WORD_SIZE]`, `words[words[2].intValueSafe() / WORD_SIZE]`) but is at least wrapped by an outer `try { return doExecute(data); } catch (Throwable t) { ... return Pair.of(true, new byte[WORD_SIZE]); }` in `execute()`, so a thrown exception there is swallowed and converted into a benign result: [4](#0-3) 

`ValidateMultiSign.execute`, however, has no such top-level catch around the array indexing/decoding logic — the risky index computation and `words[...]` accesses occur completely outside of any exception guard within the method.

### Impact Explanation
Whether an uncaught `ArrayIndexOutOfBoundsException` thrown from `ValidateMultiSign.execute` propagates to crash node processing (denial of service / node halt) or is caught generically by the surrounding TVM opcode dispatcher (e.g. `Program.callToPrecompiledAddress` / `OperationActions`) and converted into a transaction revert depends on exception handling further up the call stack, which I was not able to fully confirm from the retrieved code. If a generic `catch (Throwable)`/`RuntimeException` exists in the calling opcode handler, the practical effect is limited to a failed/reverted transaction, mirroring the low real-world severity of the original Solidity finding (a revert, not fund loss). If no such generic catch exists at that layer, the uncaught exception could propagate out of block/transaction processing and disrupt node operation, which would be a materially more severe DoS condition. The presence of a dedicated `testTip854OuterFrameContainment` test in `OperationsTest.java` (verifying "no propagated exception" and continued execution of the outer frame, but only after the TIP-854 activation flag is turned on) strongly suggests that before this specific hard fork, the outer TVM frame was not fully protected against this class of exception.

### Likelihood Explanation
Likelihood is high in terms of reachability: any account can deploy a smart contract that calls the `ValidateMultiSign` precompile address with a crafted, short or malformed input, since precompiled contract calls are reachable via ordinary `TriggerSmartContract` transactions. The condition is trivial to trigger (a single `CALL` with an out-of-range offset word). However, exploitability of a *node crash* impact specifically depends on whether the TIP-854 (`allowTvmOsaka`) hard fork has been activated on the target network — if it has, the `isValidAbiEncoding` check closes this gap; if it has not yet activated, the raw decoder path remains exposed exactly as the project's own tests document.

### Recommendation
Apply the same `isValidAbiEncoding` (or equivalent bounds validation on `words[3].intValueSafe() / WORD_SIZE` and the `words.length`) unconditionally rather than gating it behind `VMConfig.allowTvmOsaka()`, or add an explicit try/catch (as already done in `BatchValidateSign.execute`) around the entire body of `ValidateMultiSign.execute` so that any indexing/parsing exception is converted into a safe `Pair.of(true, DATA_FALSE)` result regardless of hard-fork activation status.

### Proof of Concept
1. Deploy a contract that performs a low-level `CALL`/`STATICCALL` to the `ValidateMultiSign` precompiled contract address.
2. Craft calldata such that `words[3]` (the length header of the sig array) encodes an offset/`WORD_SIZE` value that is negative or exceeds the actual length of the parsed `words` array (e.g., supply fewer total words than required, or set the offset field to an out-of-range value).
3. On a network where `allowTvmOsaka` (TIP-854) has not been activated, invoke this contract via a normal `TriggerSmartContract` transaction.
4. Observe that `words[words[3].intValueSafe() / WORD_SIZE]` in `ValidateMultiSign.execute` throws `ArrayIndexOutOfBoundsException` with no enclosing try/catch in that method, consistent with the documented pre-activation failure mode in `ValidateMultiSignContractTest.testTip854PreActivationNoOp`. [5](#0-4) 

**Note on confidence**: I was unable to fully trace whether `Program.callToPrecompiledAddress` (the caller of `PrecompiledContract.execute`) has a generic exception handler that safely contains this exception at the opcode-dispatch level for pre-TIP-854 networks. This is the key open question determining whether the real-world impact is "transaction revert only" (low severity, closely matching the original Solidity report) or "node/DoS impact" (medium/high severity). A Devin session with full repository access would be needed to trace `OperationActions`/`Program.callToPrecompiledAddress` exception handling to close this gap definitively.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1181)
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
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
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
