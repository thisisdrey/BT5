### Title
CALL to a precompiled contract can still propagate an internal exception to the outer execution frame instead of returning failure - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
`Program.callToPrecompiledAddress()` is supposed to emulate standard EVM `CALL` semantics for a call to a TVM precompile: on failure it should push `0` onto the caller's stack and let the caller's execution continue, exactly like a call to a normal (non-precompiled) contract does. However, after pushing the failure flag it unconditionally re-throws any exception that the precompile set on the shared `ProgramResult`, which unwinds the *entire current execution frame* instead of just failing the single `CALL`. This is the same failure-handling asymmetry described in the external report: a "callee" (here, a TVM precompiled contract instead of a Starknet contract) can panic in a way that the caller cannot gracefully catch, escalating a local failure into a halt of the whole transaction.

### Finding Description
Compare the two call paths in `Program.java`:

- Regular contract-to-contract `CALL` (`callToAddress`) swallows the callee's exception and continues the caller: it pushes `0` and simply `return`s without rethrowing. [1](#0-0) 

- `CALL`/`STATICCALL`/`DELEGATECALL` to a **precompiled** address (`callToPrecompiledAddress`) instead pushes `0` to mimic failure, but then checks whether the shared `this.result` carries an exception and, if so, re-throws it: [2](#0-1) 

Because precompiled contracts execute inline in the *same* `Program`/call frame (unlike a nested contract call, which runs `VM.play()` in a fresh `Program` object), this `throw result.getException();` is not caught locally — it propagates out of `op.execute(program)` inside the opcode-dispatch loop, where it is caught by `VM.play()`'s outer `catch (RuntimeException e)`, which stops the program and marks the whole transaction/frame with `setRuntimeFailure(e)`: [3](#0-2) 

For a top-level call this means a caller contract that performed a defensive `CALL` (expecting `0`/`1` return semantics, à la `ExcessivelySafeCall`) has that "safe" call instead abort its whole transaction, because the exception object set inside a precompile's `execute()` is re-thrown after the stack has already been mutated to reflect "failure."

This exact bug class was previously acknowledged in the codebase: TIP-854 explicitly patched `ValidateMultiSign` and `BatchValidateSign` so that malformed calldata is caught *inside* the precompile and turned into `(false, empty)` with no exception set, so the outer frame is not torn down: [4](#0-3) 

The regression test for the pre-fix behavior on `ValidateMultiSign`/similar decoders even documents that malformed input used to "raise inside the decoder" as "the documented pre-activation failure mode": [5](#0-4) 

The underlying re-throw at `Program.callToPrecompiledAddress` (lines 1763-1765) was not removed by TIP-854 — only two specific precompiles were hardened to avoid setting `result.getException()` on bad input. Any other precompiled contract in `PrecompiledContracts.java` (e.g. `ModExp`, `BN128*`, `Blake2F`, `P256Verify`, `VoteCount`/`RewardBalance`/vote-related precompiles, zk-proof verifiers, etc.) that can still throw an uncaught `RuntimeException`/`NumberFormatException`/arithmetic error/`ZksnarkException` etc. during `execute()` on adversarial input will hit the same escalation path: the caller's defensive `CALL` "succeeds" in pushing a failure flag but the whole frame is torn down anyway.

### Impact Explanation
Any account can construct a `CALL`/`STATICCALL` to a TVM precompiled-contract address with crafted/malformed calldata designed to trigger an internal exception rather than a controlled validation failure. If the target precompile is not one of the two explicitly hardened by TIP-854, the calling contract's entire execution frame (and, if it is the outermost call, the whole transaction) aborts with an exception instead of receiving a `0` on the stack and continuing — breaking the fundamental EVM guarantee that `CALL` isolates failures of the callee from the caller. This defeats "excessively-safe call" patterns used by wallets/bridges/relays to sandbox external calls, and can be used to deny expected functionality/availability for any contract logic that depends on continuing after a failed defensive call to one of these precompiles — mirroring the Medium-severity impact accepted in the original Kakarot finding.

### Likelihood Explanation
Reachable directly by any unprivileged transaction broadcaster: simply craft a `TriggerSmartContract` transaction (or an internal `CALL` from any deployed contract) targeting a known precompiled address with malformed input. No special permissions, SR/witness role, or privileged actor is required — this is a pure TVM opcode/precompile reachability issue.

### Recommendation
Ensure `Program.callToPrecompiledAddress()` never re-throws an exception captured from a precompile's `execute()` once the failure has been signaled via `stackPushZero()`. Instead, clear/ignore `result.getException()` for this call (as done for `ValidateMultiSign`/`BatchValidateSign` after TIP-854) so any precompile failure — regardless of cause — degrades to a standard `CALL` failure return, consistent with `callToAddress()`. Additionally, audit all precompiled contracts in `PrecompiledContracts.java` to guarantee `execute()` cannot let an uncaught runtime exception escape on malformed/adversarial input, applying the same defensive parsing pattern used for `ValidateMultiSign`/`BatchValidateSign` uniformly.

### Proof of Concept
1. Deploy a caller contract that performs a raw `CALL` (e.g. via inline assembly, "excessively safe call" style) to a TVM precompiled-contract address that is *not* `ValidateMultiSign`/`BatchValidateSign` (e.g. a vote-count/reward-balance precompile or a zk-proof-verification precompile), passing calldata crafted to trigger an internal exception (e.g., malformed proof bytes, out-of-range numeric input) rather than a normal validation failure.
2. Observe that despite the caller's `CALL` wrapping the sub-call to capture success/failure via the return boolean, `Program.callToPrecompiledAddress()` re-throws the exception set on `this.result` at line 1764, unwinding the outer frame via `VM.play()`'s catch clause, and the whole transaction reverts/fails with a `RuntimeException` instead of the caller contract observing `success == false` and continuing execution.
3. Contrast with an equivalent `CALL` to a normal (non-precompiled) contract that reverts internally, which correctly returns `0` and lets the caller continue, per `Program.callToAddress()` (lines 1168-1180).

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1168-1180)
```java
      if (callResult.getException() != null || callResult.isRevert()) {
        logger.debug("contract run halted by Exception: contract: [{}], exception: [{}]",
            Hex.toHexString(contextAddress),
            callResult.getException());
        internalTx.reject();

        callResult.rejectInternalTransactions();

        stackPushZero();

        if (callResult.getException() != null) {
          return;
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1752-1766)
```java
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
        returnDataBuffer = out.getRight();
        deposit.commit();
      } else {
        // spend all energy on failure, push zero and revert state changes
        this.refundEnergy(0, CALL_PRE_COMPILED);
        this.stackPushZero();
        if (Objects.nonNull(this.result.getException())) {
          throw result.getException();
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L93-122)
```java
        } catch (RuntimeException e) {
          logger.info("VM halted: [{}]", e.getMessage());
          if (!(e instanceof TransferException)) {
            program.spendAllEnergy();
          }
          //program.resetFutureRefund();
          program.stop();
          throw e;
        } finally {
          program.fullTrace();
        }
      }

      if (allowDynamicEnergy) {
        program.addContextContractUsage(energyUsage);
      }

    } catch (JVMStackOverFlowException | OutOfTimeException e) {
      throw e;
    } catch (RuntimeException e) {
      // https://openjdk.org/jeps/358
      // https://bugs.openjdk.org/browse/JDK-8220715
      // since jdk 14, the NullPointerExceptions message is not empty
      if (e instanceof NullPointerException || StringUtils.isEmpty(e.getMessage())) {
        logger.warn("Unknown Exception occurred, tx id: {}",
            Hex.toHexString(program.getRootTransactionId()), e);
        program.setRuntimeFailure(new RuntimeException("Unknown Exception"));
      } else {
        program.setRuntimeFailure(e);
      }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L861-897)
```java
  // TIP-854 outer-frame containment: a CALL to validateMultiSign or
  // batchValidateSign with malformed calldata must (a) push 0 onto the outer
  // stack, (b) leave the outer frame free of any propagated exception, and
  // (c) allow the outer frame to continue executing afterwards.
  @Test
  public void testTip854OuterFrameContainment() throws ContractValidateException {
    byte prePrefixByte = DecodeUtil.addressPreFixByte;
    DecodeUtil.addressPreFixByte = Constant.ADD_PRE_FIX_BYTE_MAINNET;
    VMConfig.initAllowTvmOsaka(1);
    try {
      for (PrecompiledContracts.PrecompiledContract contract :
          new PrecompiledContracts.PrecompiledContract[]{
              new PrecompiledContracts.ValidateMultiSign(),
              new PrecompiledContracts.BatchValidateSign()}) {
        invoke = new ProgramInvokeMockImpl();
        InternalTransaction interTrx = new InternalTransaction(
            Protocol.Transaction.getDefaultInstance(),
            InternalTransaction.TrxType.TRX_UNKNOWN_TYPE);
        program = new Program(new byte[0], new byte[0], invoke, interTrx);
        // inDataSize=0 ⇒ data=[] ⇒ fewer than H=5 head words ⇒ guard rejects.
        MessageCall messageCall = new MessageCall(
            Op.CALL, new DataWord(10000),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), false);
        program.callToPrecompiledAddress(messageCall, contract);

        Assert.assertNull(contract.getClass().getSimpleName()
                + ": outer frame must not inherit an exception",
            program.getResult().getException());
        Assert.assertEquals(contract.getClass().getSimpleName() + ": inner CALL pushes 0",
            DataWord.ZERO(), program.getStack().pop());
        // Outer frame continues: another stack op works without throwing.
        program.stackPush(new DataWord(1));
        Assert.assertEquals(new DataWord(1), program.getStack().pop());
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
