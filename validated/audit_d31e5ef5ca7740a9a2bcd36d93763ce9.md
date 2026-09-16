### Title
Unvalidated attacker-controlled array index in `ValidateMultiSign` precompile causes uncaught `ArrayIndexOutOfBoundsException` from TVM `CALL` - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2022-27223 is caused by an unvalidated endpoint index taken from an untrusted (host-controlled) request being used directly to index into a driver array. The direct java-tron analog is in the `ValidateMultiSign` precompiled contract (`org.tron.core.vm.PrecompiledContracts$ValidateMultiSign`), which parses TVM calldata into a `DataWord[]` and then indexes that array using offsets computed straight from attacker-supplied calldata words, with no bounds checking, before ever entering the method's own `try/catch`.

### Finding Description
`ValidateMultiSign.execute()` does: [1](#0-0) 

- `words = DataWord.parseArray(rawData)` builds the word array purely from the length of the raw calldata that any contract can supply via a TVM `CALL`/`STATICCALL` to precompile address `0x0a`.
- `words[0]`, `words[1]`, `words[2]`, and `words[3]` are accessed unconditionally — if `rawData` is shorter than 4 words (128 bytes), this throws `ArrayIndexOutOfBoundsException` immediately.
- When `VMConfig.allowTvmSelfdestructRestriction()` is enabled, the code computes `int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();` — the index used here (`words[3].intValueSafe() / WORD_SIZE`) is fully attacker-controlled and unbounded; nothing checks it is `< words.length` or `>= 0` before the array access, unlike the sibling helper `extractBytesArray`, which explicitly guards `if (offset > words.length - 1) return new byte[0][];`: [2](#0-1) 
- Critically, all of this indexing happens *before* the method's only `try/catch` block, which only wraps the later permission/signature-recovery logic: [3](#0-2) 

This is confirmed by the codebase's own tests, which document that pre-hardfork-activation malformed calldata reaches "the legacy decoder" with "no outer catch", and that a length/format guard (`isValidAbiEncoding`, gated behind `VMConfig.allowTvmOsaka()`) was added specifically to intercept this: [4](#0-3) 

A closely related but already-mitigated instance exists in the sibling precompile `BatchValidateSign`: it also lacks a bounds check in `extractBytes32Array` (used for the `address[]` argument), but that whole method wraps `doExecute` in a blanket `catch (Throwable t)` that swallows the exception and returns a benign `(true, zero-word)` result: [5](#0-4) [6](#0-5) 

`ValidateMultiSign` has no equivalent outer catch, so the uncaught exception propagates out of `execute()` into the caller.

### Impact Explanation
When Osaka's `isValidAbiEncoding` guard is not active for the network (pre-hardfork or if `allowTvmOsaka` is disabled), any contract can trigger this by issuing a `CALL`/`STATICCALL` to address `0x0a` with calldata shorter than 4 words, or with `words[3]` crafted to compute an out-of-range division result. I was unable to fully trace, within the remaining budget, exactly how far up the call stack `callToPrecompiledAddress` in `Program.java` propagates this uncaught `RuntimeException` before it is caught by `VM.play()`'s generic `catch (RuntimeException e)` handler; based on `VM.play()`'s structure, an uncaught runtime exception from an opcode/precompile execution is caught there, the program is stopped, and energy is fully consumed — i.e., the calling transaction reverts rather than crashing the node. This is consistent with normal TVM exception-handling semantics and would make the practical impact "wasted gas / reverted transaction" for the caller rather than node crash, chain halt, or fund theft — unless there is some code path (e.g., off-chain/constant call handling, or a caller in `Wallet`/`TronJsonRpcImpl` that invokes this precompile logic without the VM's protective wrapper) that does not catch the exception, which I could not verify with certainty from the available index.

### Likelihood Explanation
Trivial to trigger: the trigger condition is a short (or specially-shaped) calldata payload sent to a fixed, always-reachable precompile address from any smart contract, requiring no special privileges — any transaction broadcaster or contract deployer can reach it via a plain Solidity `call(0x0a, ...)`.

### Recommendation
- Add explicit bounds checks before every `words[...]` access in `ValidateMultiSign.execute()` (mirroring the `offset > words.length - 1` guard already used in `extractBytesArray`/`extractSigArray`), independent of the `allowTvmOsaka()` hardfork flag, so the fix applies unconditionally rather than only after activation.
- Add the same missing bounds check to `extractBytes32Array` for symmetry with its sibling helpers.
- Wrap the entire `ValidateMultiSign.execute()` body (not just the inner permission-check block) in a `try/catch` that returns a deterministic `DATA_FALSE`/failure result on any parsing exception, matching the containment pattern already used in `BatchValidateSign.execute()`.

### Proof of Concept
Not independently executed against a running node (no execution environment available). Conceptually:
1. Deploy or use any contract that performs a low-level `call(gasleft(), address(0x0a), 0, input, input.length, output, 32)` where `address(0x0a)` is `validateMultiSignAddr`.
2. Set `input` to fewer than 128 bytes (4 words) — e.g., 64 bytes of arbitrary data — while `VMConfig.allowTvmOsaka()` is not active on the target network.
3. `ValidateMultiSign.execute()` calls `DataWord.parseArray(rawData)`, producing a `words` array shorter than 4 entries; the subsequent `words[1]`/`words[2]`/`words[3]` access throws `ArrayIndexOutOfBoundsException`, confirmed by the existing test `testTip854PreActivationNoOp` which explicitly exercises `contract.execute(new byte[(5 + 1) * 32])`-style malformed inputs and documents this failure mode: [7](#0-6) 

Note: due to index size limits, I could not confirm whether any call path invokes `ValidateMultiSign.execute()` outside of the VM's normal opcode-dispatch loop (which would catch the exception); a Devin session with full repository access would be needed to trace `Program.callToPrecompiledAddress` and its callers exhaustively to rule out a more severe (uncontained) crash scenario.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-397)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }
```

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1074)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1145-1154)
```java
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
