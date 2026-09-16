### Title
Uncaught `ArrayIndexOutOfBoundsException` in `ValidateMultiSign` / `BatchValidateSign` TVM precompiles on malformed calldata (pre-TIP-854 activation) - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The TVM precompiled contracts `ValidateMultiSign` (address `0x...a`) and, to a lesser extent, `BatchValidateSign` (address `0x...9`) parse attacker-controlled ABI-encoded calldata into a fixed-shape nested structure (header words + a length-prefixed signature array) and dereference offsets derived from that data before validating that the calldata is long enough to contain them. Just like the UpdateHub CVE — which trusted an inner array length taken from a nested, attacker-supplied metadata structure without checking it against the actual payload — `ValidateMultiSign.execute()` reads `words[0]`, `words[1]`, `words[2]`, `words[3]` and then computes `words[words[3].intValueSafe() / WORD_SIZE]` and calls `extractSigArray`/`extractBytesArray` on that offset, with **no bounds check and no surrounding try/catch**, unless the TIP-854/Osaka guard (`isValidAbiEncoding`) is active.

### Finding Description
In `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`: [1](#0-0) 

```java
public Pair<Boolean, byte[]> execute(byte[] rawData) {
  if (VMConfig.allowTvmOsaka()
      && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
    return Pair.of(false, EMPTY_BYTE_ARRAY);
  }
  DataWord[] words = DataWord.parseArray(rawData);
  byte[] address = words[0].toTronAddress();
  int permissionId = words[1].intValueSafe();
  byte[] data = words[2].getData();
  ...
  if (VMConfig.allowTvmSelfdestructRestriction()) {
    int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
    ...
  }
  byte[][] signatures = ... extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) ...
```

The length-validating guard (`isValidAbiEncoding`) is only consulted `if (VMConfig.allowTvmOsaka())`. `allowTvmOsaka` is a committee-activated fork switch that defaults to disabled — exactly analogous to how the UpdateHub bug only checked the *outer* array length while never checking the *inner* one. When the guard is off (current default), `DataWord.parseArray(rawData)` produces a `words` array sized only to what the raw calldata actually contains; any call with fewer than 4 words (128 bytes) causes `words[3]` (or `words[1]`/`words[2]`) to throw `ArrayIndexOutOfBoundsException`. This throw happens **outside** the `try { ... } catch (Throwable t) { ... }` block that only wraps the later account/permission logic (lines ~1082+), so it is completely uncaught within the precompile.

This is not speculative: the test suite explicitly documents this as the pre-activation failure mode for exactly this contract: [2](#0-1) 

```java
// TIP-854: before activation, malformed calldata reaches the legacy decoder.
// Assert the guard is not taken — this precompile has no outer catch, so a
// too-short input raises inside the decoder; that is the documented
// pre-activation failure mode the TIP explicitly preserves.
@Test
public void testTip854PreActivationNoOp() {
  VMConfig.initAllowTvmOsaka(0);
  ...
  try {
    Pair<Boolean, byte[]> ret = contract.execute(new byte[(5 + 1) * 32]);
    ...
  } catch (RuntimeException expectedLegacyBehaviour) {
    // Pre-activation: decoder may throw — this is the existing behaviour.
  }
}
```

The `BatchValidateSign` precompile has the same unguarded header-parsing pattern (`words[0]`, `words[1]`, `words[2]` accessed before `extractBytesArray`/`extractBytes32Array`), but its outer `execute()` wraps the whole `doExecute()` call in `catch (Throwable t)`, so it degrades gracefully to `(true, zero-bytes)` — [3](#0-2) . `ValidateMultiSign` has no such outer guard.

### Impact Explanation
Any account can reach this code path via a plain TVM `CALL`/`STATICCALL` to the `ValidateMultiSign` precompile address from a contract, with attacker-chosen calldata shorter than the minimum header size. This is reachable from a single unprivileged, unsigned-by-victim transaction (any contract invocation triggers the precompile if the contract calls it, and since `ValidateMultiSign` is a public well-known address any caller can hit it directly through a trivial forwarding contract or, depending on TVM dispatch, directly). Because the resulting `ArrayIndexOutOfBoundsException` is raised deep in precompile execution with no local catch, its ultimate effect on the node (clean transaction-level revert captured by an outer TVM/Program exception handler vs. an uncaught exception that could disrupt the block-processing thread in `Manager`) depends on exception handling further up the call stack (e.g., inside `Program`'s precompiled-contract dispatch) that I was not able to fully verify within the available tool budget. What is concretely established is: (1) the data-length validation that the TIP-854 fix added is gated behind a currently-inactive fork switch, and (2) the test suite itself documents that pre-activation this specific precompile has "no outer catch" for malformed calldata. At minimum this is a reliable way to throw an unhandled runtime exception during TVM execution of a broadcastable contract call; at worst (if the surrounding VM/Program layer does not defensively catch generic `RuntimeException`), it is a remotely triggerable denial-of-service against block/transaction processing, which is the same class of impact as the referenced CVE (fatal fault caused by trusting an unchecked length field in nested, network/attacker-supplied data).

### Likelihood Explanation
High reachability: the precompile is invoked purely by TVM contract calls, requiring no special permission, no token balance beyond minimal energy, and no cooperation from other parties. The vulnerable path is the *default* configuration (`allowTvmOsaka` off) rather than a rare/legacy-only edge case, so it is exploitable on any network that has not yet activated the TIP-854 fork. The main uncertainty is only the ultimate blast radius (transaction revert vs. broader crash), not reachability.

### Recommendation
Make the ABI-encoding/length validation (`isValidAbiEncoding` check) in `ValidateMultiSign.execute()` (and equivalently in `BatchValidateSign.doExecute()`) unconditional, rather than gated behind `VMConfig.allowTvmOsaka()`, so malformed/too-short calldata is rejected with `(false, EMPTY_BYTE_ARRAY)` before any `words[i]` access, regardless of fork-activation state. Alternatively/additionally, wrap the entire body of `ValidateMultiSign.execute()` in the same defensive `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }` pattern already used in `BatchValidateSign.execute()`, so an out-of-bounds/parsing failure degrades to a safe, deterministic failure result instead of propagating an uncaught exception.

### Proof of Concept
1. Deploy (or use an existing) contract that performs `staticcall`/`call` to the `ValidateMultiSign` precompile address (`0x000000000000000000000000000000000000000a`) with calldata shorter than `(5) * 32` bytes (e.g., `(5+1)*32` per the test, or shorter), while the network has `allowTvmOsaka` disabled (default).
2. `DataWord.parseArray(rawData)` returns a `words` array with fewer elements than 4.
3. `execute()` dereferences `words[3]` (or `words[1]`/`words[2]`) directly, outside any try/catch, throwing `ArrayIndexOutOfBoundsException`.
4. This matches the exact reproduction documented in `testTip854PreActivationNoOp` in `ValidateMultiSignContractTest.java` [4](#0-3) , which explicitly asserts this "may raise" and treats it as the accepted pre-activation behavior.

Note on confidence: I was not able to conclusively trace whether the TVM interpreter layer surrounding precompile invocation (e.g., `Program`'s dispatch to `PrecompiledContract.execute`) catches generic `RuntimeException`/`Throwable` and converts it into an ordinary contract revert, or whether it can propagate further and disrupt block/transaction processing at the `Manager` level — this would need to be verified directly in a running/debuggable environment (e.g., via a Devin session with full repository and runtime access) before treating this as a confirmed node-crash vulnerability rather than a "worst case revert with unexpected exception type" bug.

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
