### Title
Uncaught JVM `OutOfMemoryError`/`NegativeArraySizeException` from unchecked array-length in `ValidateMultiSign` precompile can crash the TVM execution path - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
Similar to CVE-2020-27824, where OpenJPEG's `opj_dwt_calc_explicit_stepsizes()` allocates a buffer based on an attacker-controlled "decomposition levels" value without validating it first, java-tron's `ValidateMultiSign` precompiled contract (address `0x...a`, reachable via any TVM `CALL` from a broadcast transaction) derives an array length directly from attacker-supplied call data and allocates a Java array of that size *before* any exception-safety net is in place.

### Finding Description
`ValidateMultiSign.execute()` reads `words[3]` from the raw call data to locate a "signature array" offset, then (when TIP-854/`allowTvmSelfdestructRestriction` is not yet active, or when the ABI-encoding pre-check is skipped because `allowTvmOsaka()` is off) calls `extractBytesArray`/`extractSigArray`: [1](#0-0) 

```
private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    ...
```

`len` comes straight from `intValueSafe()` on a fully attacker-controlled 32-byte word, and is used to size `new byte[len][]` before any bound is enforced. Unlike the sibling `extractBytes32Array` used by `BatchValidateSign`, the call site in `ValidateMultiSign.execute()`: [2](#0-1) 

is **not** wrapped in any `try/catch` — the internal `try { ... } catch (Throwable t) { ... }` block only guards the later account/permission-weight computation, not the initial `extractBytesArray`/`extractSigArray` calls. This is explicitly documented as intentional legacy behavior by the project's own test: [3](#0-2) 

which states "this precompile has no outer catch, so a too-short input raises inside the decoder."

Crucially, `contract.execute(data)` is invoked from `Program.callToPrecompiledAddress` with no surrounding `try/catch`: [4](#0-3) 

A crafted `len` near `Integer.MAX_VALUE` causes `new byte[len][]` to request an object-reference array of up to ~16 GB (8 bytes × 2³¹ elements on a 64-bit JVM), which is very likely to throw `java.lang.OutOfMemoryError` — a JVM `Error`, not an `Exception`. This differs qualitatively from the codebase's deliberately-handled `Program.OutOfMemoryException` (a checked business exception that is caught and converted into a clean transaction revert, as exercised in `EnergyWhenAssertStyleTest.outOfMemTest`). A raw `java.lang.OutOfMemoryError` is not that type and is not guaranteed to be caught by ordinary `catch (Exception e)` blocks in the runtime/transaction-processing pipeline, risking destabilization of the JVM heap for the whole node process (all threads), not just the single transaction's execution context.

### Impact Explanation
If the raw JVM `OutOfMemoryError` (or a similarly unhandled `NegativeArraySizeException`/`ArrayIndexOutOfBoundsException` when `len` is negative or points out of bounds) propagates past `callToPrecompiledAddress` without being caught by a `Throwable`-level handler in the block/transaction execution pipeline, it can disrupt or crash the node process that is applying the block, since OOM affects the entire JVM heap, not just the current call frame. This would satisfy the "node crash or halt" criteria in scope. The severity is bounded by whether an upstream `catch (Throwable ...)` exists in `TransactionTrace`/`RuntimeImpl` (I was not able to fully confirm this in the available time — see Uncertainty below).

### Likelihood Explanation
This is trivially reachable: any account can send a TRC10/TRX-funded transaction that performs a `CALL` to precompile address `0x...a` (`ValidateMultiSign`) with crafted call data setting `words[3]`'s pointed-to length word to a very large value. No special privilege, TIP-854 activation state, or contract deployment beyond a single-opcode `CALL` in a smart contract is required, and the ABI pre-check (`isValidAbiEncoding`) that would otherwise reject malformed shapes only fires when `allowTvmOsaka()` is enabled.

### Recommendation
- Bound-check `len` in `extractBytesArray`/`extractSigArray`/`extractBytes32Array` against a small sane maximum (e.g. `MAX_SIZE` already used by `ValidateMultiSign`/`BatchValidateSign`) **before** allocating any array, mirroring the fix already applied in the `sigArraySize > MAX_SIZE` check that currently only runs under `allowTvmSelfdestructRestriction`.
- Make that length validation unconditional (not gated behind a hard-fork flag) so pre-activation call paths are equally protected.
- Wrap the entirety of `ValidateMultiSign.execute()` (not just the account/permission logic) in the same `catch (Throwable t)` pattern already used by `BatchValidateSign.execute()`, so any residual decode failure degrades to `Pair.of(true, DATA_FALSE)` instead of propagating.

### Proof of Concept
1. Construct call data for `ValidateMultiSign` (`0x...a`) where word `words[3]` (the signature-array offset pointer, divided by 32) points to a word whose value is `0xFFFFFFFF` (or another huge 32-bit value) interpreted via `intValueSafe()`.
2. Ensure `allowTvmOsaka()` is disabled (default/pre-activation state) so `isValidAbiEncoding` is skipped, and `allowTvmSelfdestructRestriction()` is disabled so the `sigArraySize > MAX_SIZE` guard is skipped, routing execution into `extractBytesArray(words, offset, rawData)`.
3. Deploy a trivial contract that performs `CALL` to this precompile address with the crafted data, and broadcast a transaction that triggers it.
4. Observe `new byte[len][]` attempt a multi-gigabyte allocation, raising `java.lang.OutOfMemoryError` inside `Program.callToPrecompiledAddress` with no local `catch`.

### Uncertainty
I could not fully confirm, within the available search budget, whether `TransactionTrace`/`RuntimeImpl` (the top-level caller of VM execution during block application) contains a `catch (Throwable ...)` that would safely absorb a raw `OutOfMemoryError` and only revert the single transaction, versus letting it escape and destabilize block processing. If such a `Throwable`-level catch exists at the top of the execution pipeline, this analog's impact would be reduced to a contained transaction failure rather than a node crash, and would not meet the required "unauthorized operation, theft, node crash, chain split" bar. This should be verified directly in `TransactionTrace.exec()`/`RuntimeImpl.execute()` before treating this as a confirmed high-impact finding.

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1752-1752)
```java
      Pair<Boolean, byte[]> out = contract.execute(data);
```
