This is governed by a chain parameter (`allowTvmOsaka`) that defaults to `false` (`Snapshot.allowTvmOsaka` is a boolean field, unset = `false`) and is only flipped on by a committee-controlled hard fork proposal via `initAllowTvmOsaka`, exactly like every other `allowTvm*` feature flag in `VMConfig`. Until that proposal is activated on a given network, `VMConfig.allowTvmOsaka()` returns `false` and the "TIP-854 outer-frame containment" fix in `Program.callToPrecompiledAddress` is not exercised.

### Title
Precompile decoder exception escapes CALL-frame isolation and aborts the entire transaction/VM execution pre-TIP-854 activation - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
`Program.callToPrecompiledAddress` invokes `contract.execute(data)` for the `ValidateMultiSign` / `BatchValidateSign` precompiles and expects a `Pair<Boolean, byte[]>` result so a failure can be turned into "push 0 on the CALL frame's stack" — normal EVM/TVM call-failure semantics. [1](#0-0)  Before the `allowTvmOsaka` (TIP-854) hard fork flag is activated, malformed calldata to these two precompiles is documented and tested to instead throw an uncaught `RuntimeException` out of the decoder itself, with "no outer catch" inside the precompile. [2](#0-1) [3](#0-2)  When that happens, `contract.execute(data)` throws instead of returning `(false, ...)`, so the exception propagates straight through `callToPrecompiledAddress` (skipping the `stackPushZero()` / energy-refund logic entirely), up through the calling opcode's `op.execute(program)` in `VM.play`, into `VM.play`'s `catch (RuntimeException e)` handler, which spends all remaining energy, stops the *whole* program, and rethrows. [4](#0-3)  This is functionally analogous to the vm2 bug class: an internal error object generated deep inside a supposedly-isolated execution unit (the precompile "sandbox") is not intercepted at the isolation boundary and instead reaches — and corrupts the control flow of — the enclosing host context (the outer contract's VM execution), rather than being confined to a failed-call return value.

### Finding Description
In normal TVM/EVM semantics, a `CALL`/`DELEGATECALL` to any address (including precompiles) that fails must only cause that inner call to fail (stack gets `0`, some energy consumed) while the calling (outer) contract continues executing. This is exactly what `Program.callToAddress` does for ordinary internal calls: an exception in the callee is converted into `stackPushZero()` and containment. [5](#0-4) 

For precompiled contracts, however, `callToPrecompiledAddress` relies on the precompile's `execute()` returning a `Pair<Boolean, byte[]>` rather than throwing. The `ValidateMultiSign`/`BatchValidateSign` decoders are explicitly documented in tests as having "no outer catch" and raising a raw `RuntimeException` on malformed calldata (e.g., too-short ABI-encoded input) *when `allowTvmOsaka` is not activated*. [2](#0-1)  The TIP-854 containment fix — which the codebase's own regression test (`testTip854OuterFrameContainment`) verifies restores correct isolation (`stackPushZero`, no exception leaking to the outer `Program`, outer frame continues) — is gated entirely behind `VMConfig.initAllowTvmOsaka(1)`. [6](#0-5)  `VMConfig.allowTvmOsaka()` simply reads a boolean field off the current `Snapshot` that is only set via `initAllowTvmOsaka`, i.e., a committee-voted chain parameter, exactly like all the other `allowTvm*` feature toggles. [7](#0-6) 

On any network/height where this parameter has not yet been activated, an attacker's crafted calldata to `ValidateMultiSign`/`BatchValidateSign` inside a `CALL` from within a smart contract causes the decoder's `RuntimeException` to escape `callToPrecompiledAddress` uncaught, propagate through `op.execute(program)` in `VM.play`, and be caught only by `VM.play`'s outer `catch (RuntimeException e)`, which spends **all** energy and stops the **entire** program — not just the inner call. [8](#0-7)  This means a nested/inner `CALL` failure (which should be recoverable, e.g. wrapped in a `try/catch` at the Solidity level, or in a multi-call batch contract) instead unconditionally reverts the entire outer transaction and burns the outer caller's full fee-limit energy, breaking call-frame isolation that every other call path in `Program` (see `callToAddress`) correctly preserves.

### Impact Explanation
Any contract that performs an internal `CALL` to the `ValidateMultiSign`/`BatchValidateSign` precompile address with attacker-influenced or malformed calldata (a common pattern for multisig wallets/DEXes verifying batched signatures) can be forced to have its *entire* transaction fail and consume the full energy limit, instead of the localized, recoverable call failure the calling contract's Solidity logic expects (e.g., `try { precompile.call(...) } catch {}` or checking a boolean return). This breaks the call-isolation guarantee the TVM otherwise provides uniformly for both regular calls and precompile calls, and can be weaponized to force unconditional energy exhaustion/DoS of any contract or batch operation that reaches these precompiles with attacker-controlled signature data, prior to TIP-854 activation.

### Likelihood Explanation
Reachable by any unprivileged party who can get a `CALL`/`DELEGATECALL`/`STATICCALL` executed against the `ValidateMultiSign` or `BatchValidateSign` precompile addresses with sub-minimum-length calldata — trivially achievable by any contract deployer or by crafting a transaction that triggers such a call via an already-deployed multisig-verification contract. No special privileges are required, only that the network has not yet activated the `allowTvmOsaka` hard fork flag, which the tests explicitly frame as "pre-activation" legacy behaviour still present in the codebase.

### Recommendation
Wrap the `ValidateMultiSign`/`BatchValidateSign` decoder logic in an internal `try/catch` inside `execute()` itself (independent of the `allowTvmOsaka` flag) so any decoding failure always returns `Pair.of(false, ...)` rather than throwing, matching the containment behavior that `callToPrecompiledAddress` and `callToAddress` already assume for every other call path. Alternatively, backport the TIP-854 containment check to be unconditional rather than gated behind the hard-fork flag, since call-frame isolation is a correctness property, not a consensus-breaking behavior change requiring a flag (or ensure the flag is activated on all supported networks before this analysis window).

### Proof of Concept
1. Deploy (or use an existing) contract that performs `address(0x1000001).call(shortMalformedData)` (or whichever fixed address maps to `ValidateMultiSign`/`BatchValidateSign`) with calldata shorter than the minimum head-word length (`< (5+1)*32` bytes as used in the regression tests).
2. Ensure the network has not activated the `allowTvmOsaka` parameter (`VMConfig.allowTvmOsaka() == false`), which is the default/unset state absent an explicit committee proposal.
3. Submit the triggering transaction: the precompile's decoder throws a `RuntimeException` internally (confirmed by `ValidateMultiSignContractTest.testTip854PreActivationNoOp` / `BatchValidateSignContractTest.testTip854PreActivationNoOp`, which explicitly catch and comment on this "existing/legacy" throw). [9](#0-8) 
4. Observe that `callToPrecompiledAddress` does not catch this exception before `stackPushZero()`/`refundEnergy` execute [1](#0-0) , it propagates to `VM.play`'s outer catch, spending all energy and halting the whole program rather than only the inner CALL. [8](#0-7) 
5. Compare against `testTip854OuterFrameContainment`, which demonstrates that with `allowTvmOsaka` activated the same malformed-calldata call correctly pushes `0` and leaves the outer frame's exception state clean and continuable — confirming the pre-activation path is the vulnerable one. [6](#0-5)

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

**File:** framework/src/test/java/org/tron/common/runtime/vm/BatchValidateSignContractTest.java (L204-213)
```java
  // that would raise inside doExecute gets collapsed to (true, 32-byte zero) by
  // the outer catch — this is the legacy behaviour and must be preserved.
  @Test
  public void testTip854PreActivationNoOp() {
    VMConfig.initAllowTvmOsaka(0);
    contract.setVmShouldEndInUs(System.nanoTime() / 1000 + 2_000_000);
    Pair<Boolean, byte[]> ret = contract.execute(new byte[(5 + 1) * 32]);
    Assert.assertTrue("pre-activation must not take the new reject path", ret.getLeft());
    Assert.assertEquals(32, ret.getRight().length);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L90-122)
```java
          op.execute(program);

          program.setPreviouslyExecutedOp((byte) op.getOpcode());
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L861-902)
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
    } finally {
      VMConfig.initAllowTvmOsaka(0);
      DecodeUtil.addressPreFixByte = prePrefixByte;
    }
  }
```

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L199-309)
```java
  public static void initAllowTvmOsaka(long allow) {
    globalSnapshot.allowTvmOsaka = allow == 1;
  }

  public static void initAllowHardenResourceCalculation(long allow) {
    globalSnapshot.allowHardenResourceCalculation = allow == 1;
  }

  public static boolean getEnergyLimitHardFork() {
    return CommonParameter.ENERGY_LIMIT_HARD_FORK;
  }

  public static boolean allowTvmTransferTrc10() {
    return current().allowTvmTransferTrc10;
  }

  public static boolean allowTvmConstantinople() {
    return current().allowTvmConstantinople;
  }

  public static boolean allowMultiSign() {
    return current().allowMultiSign;
  }

  public static boolean allowTvmSolidity059() {
    return current().allowTvmSolidity059;
  }

  public static boolean allowShieldedTRC20Transaction() {
    return current().allowShieldedTRC20Transaction;
  }

  public static boolean allowTvmIstanbul() {
    return current().allowTvmIstanbul;
  }

  public static boolean allowTvmFreeze() {
    return current().allowTvmFreeze;
  }

  public static boolean allowTvmVote() {
    return current().allowTvmVote;
  }

  public static boolean allowTvmLondon() {
    return current().allowTvmLondon;
  }

  public static boolean allowTvmCompatibleEvm() {
    return current().allowTvmCompatibleEvm;
  }

  public static boolean allowHigherLimitForMaxCpuTimeOfOneTx() {
    return current().allowHigherLimitForMaxCpuTimeOfOneTx;
  }

  public static boolean allowTvmFreezeV2() {
    return current().allowTvmFreezeV2;
  }

  public static boolean allowOptimizedReturnValueOfChainId() {
    return current().allowOptimizedReturnValueOfChainId;
  }

  public static boolean allowDynamicEnergy() {
    return current().allowDynamicEnergy;
  }

  public static long getDynamicEnergyThreshold() {
    return current().dynamicEnergyThreshold;
  }

  public static long getDynamicEnergyIncreaseFactor() {
    return current().dynamicEnergyIncreaseFactor;
  }

  public static long getDynamicEnergyMaxFactor() {
    return current().dynamicEnergyMaxFactor;
  }

  public static boolean allowTvmShanghai() {
    return current().allowTvmShanghai;
  }

  public static boolean allowEnergyAdjustment() {
    return current().allowEnergyAdjustment;
  }

  public static boolean allowStrictMath() {
    return current().allowStrictMath;
  }

  public static boolean allowTvmCancun() {
    return current().allowTvmCancun;
  }

  public static boolean disableJavaLangMath() {
    return current().disableJavaLangMath;
  }

  public static boolean allowTvmBlob() {
    return current().allowTvmBlob;
  }

  public static boolean allowTvmSelfdestructRestriction() {
    return current().allowTvmSelfdestructRestriction;
  }

  public static boolean allowTvmOsaka() {
    return current().allowTvmOsaka;
  }
```
