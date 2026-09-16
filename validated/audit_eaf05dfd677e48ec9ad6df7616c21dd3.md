### Title
Unbounded array indexing in `ValidateMultiSign` precompile causes uncaught `ArrayIndexOutOfBoundsException` on malformed calldata - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE describes an out-of-bounds/invalid memory access in a native getter that a remote caller can trigger with crafted input, causing a crash. The closest reachable analog in java-tron is the `ValidateMultiSign` precompiled contract, whose calldata-parsing path performs unchecked array indexing into a `DataWord[]` derived directly from attacker-supplied calldata, and — unlike the sibling `BatchValidateSign` contract — has no surrounding `try/catch` to contain the resulting exception.

### Finding Description
`ValidateMultiSign.execute()` parses `rawData` into `DataWord[] words = DataWord.parseArray(rawData)` and then immediately indexes into it using offsets taken from the calldata itself: [1](#0-0) 

The extraction helpers `extractBytesArray` / `extractSigArray` compute `words[offset + i + 1]` using a length (`len`) and `offset` fully controlled by the caller, with no bound checking against `words.length`: [2](#0-1) 

The only guard added against malformed calldata (`isValidAbiEncoding`) is conditioned on `VMConfig.allowTvmOsaka()`: [3](#0-2) 

This is corroborated directly by an existing regression test, which documents that pre-activation this exact code path throws an uncaught `RuntimeException` (e.g. `ArrayIndexOutOfBoundsException`) because "this precompile has no outer catch": [4](#0-3) 

By contrast, `BatchValidateSign` wraps the equivalent logic (`doExecute`) in a top-level `try { } catch (Throwable t)`, defensively absorbing exactly this class of bug: [5](#0-4) 

The precompile is reachable from any contract call (`CALL`/`STATICCALL`/`DELEGATECALL`/`CALLCODE`) to its fixed address via `Program.callToPrecompiledAddress`, which invokes `contract.execute(data)` with no surrounding exception handling of its own: [6](#0-5) 

### Impact Explanation
An unhandled `ArrayIndexOutOfBoundsException` thrown out of `contract.execute()` propagates through `Program.callToPrecompiledAddress` into the VM's opcode dispatch loop, which is caught generically at `VM.play()`'s `catch (RuntimeException e)` and ultimately by `VMActuator`'s outer `catch (Throwable e)`: [7](#0-6) 

Because this outer-level `catch (Throwable e)` in `VMActuator` exists, a transaction-triggered call is contained at the transaction level (it fails with energy consumed, not a node crash) rather than propagating out of the JVM. This significantly weakens — though does not eliminate — the impact compared to the referenced CVE's memory-corruption SEGV: the practical effect here is a contained transaction failure, not a demonstrated node crash, unless there exists a separate call path (e.g. a constant-call/estimate-energy RPC handler) that invokes the TVM without an equivalently broad exception boundary. I was not able to confirm within available context whether `Wallet`'s `triggerConstantContract`/`estimateEnergy` RPC/HTTP paths wrap `Runtime.execute()` in an equally comprehensive `catch (Throwable e)`, so I cannot confirm a genuine node-crash or service-denial impact with certainty.

### Likelihood Explanation
The `ValidateMultiSign` precompile is only registered when `VMConfig.allowTvmSolidity059()` is enabled, and the crafted-calldata attack requires that the newer `allowTvmOsaka()` guard has not yet been activated on the target network. Triggering the exception requires only a single crafted contract deployment/call with malformed offset words — no special privilege, cost is limited to the calling transaction's own energy, and the crafted call is easy to construct deterministically (offsets/lengths chosen to index past `words.length`).

### Recommendation
- Wrap `ValidateMultiSign.execute()`'s parsing/extraction logic (from `DataWord.parseArray` through `extractBytesArray`/`extractSigArray`) in a `try/catch(Throwable)` analogous to `BatchValidateSign.doExecute`, converting parse failures into `Pair.of(false, EMPTY_BYTE_ARRAY)` or `Pair.of(true, DATA_FALSE)` instead of letting exceptions escape.
- Make the `isValidAbiEncoding` bounds validation unconditional (not gated behind `allowTvmOsaka()`), or add explicit bounds checks in `extractBytes32Array`/`extractBytesArray`/`extractSigArray` before every array access, independent of hard-fork activation state.
- Audit all RPC/HTTP/gRPC entry points that can invoke TVM execution (e.g. `triggerConstantContract`, `estimateEnergy`) to confirm they always terminate in a top-level `catch (Throwable)` equivalent to `VMActuator`'s, so that a crafted precompile call cannot destabilize a request-handling thread outside the normal transaction-execution error path.

### Proof of Concept
1. On a network where `allowTvmSolidity059` is active but `allowTvmOsaka` is not yet activated (the current/default state for this hard-fork gate), deploy or use an existing contract that performs a `CALL`/`STATICCALL` to the `ValidateMultiSign` precompile address (`0x0000...1006` per `validateMultiSignAddr`).
2. Craft calldata matching the `validatemultisign(address,uint256,bytes32,bytes[])` ABI shape but with the offset word (`words[3]`) or the encoded array-length word chosen so that `words[offset + i + 1]` in `extractBytesArray`/`extractSigArray` indexes beyond `words.length` (e.g., supply a very large length or an offset pointing near the end of the `DataWord[]`).
3. Submit the transaction/`TriggerSmartContract` call; `ValidateMultiSign.execute()` throws `ArrayIndexOutOfBoundsException` from `extractBytesArray`/`extractSigArray`, uncaught within the precompile itself — confirmed by `ValidateMultiSignContractTest.testTip854PreActivationNoOp`, which explicitly asserts this legacy failure mode via `catch (RuntimeException expectedLegacyBehaviour)`. [8](#0-7)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-426)
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1748-1753)
```java
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);

```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L287-302)
```java
    } catch (Throwable e) {
      if (!(e instanceof TransferException)) {
        program.spendAllEnergy();
      }
      result = program.getResult();
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      if (Objects.isNull(result.getException())) {
        logger.error(e.getMessage(), e);
        result.setException(new RuntimeException("Unknown Throwable"));
      }
      if (StringUtils.isEmpty(result.getRuntimeError())) {
        result.setRuntimeError(result.getException().getMessage());
      }
      logger.info("runtime result is :{}", result.getException().getMessage());
    }
```
