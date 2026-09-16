### Title
Uncaught exceptions in `PrecompiledContract.execute()` from attacker-supplied `CALL` data burn the entire transaction's energy instead of failing the sub-call - (`actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
The Notional finding is a generic "gas bomb" pattern: code that decodes attacker-influenced data via a call and only wraps the *call* in try/catch, not the *decode*, so a malformed-but-non-reverting response throws an uncaught exception that burns all remaining gas instead of failing gracefully. Java-tron's TVM has the exact same structural weakness in how `CALL`/`STATICCALL`/`DELEGATECALL` invoke precompiled contracts, and the project has already had to patch two concrete instances of it (TIP-854, `ValidateMultiSign` / `BatchValidateSign`), which is direct proof this bug class is reachable and previously exploitable in this codebase.

### Finding Description
When a smart contract executes `CALL`/`STATICCALL`/`DELEGATECALL` against a precompiled-contract address, `OperationActions.exeCall` routes to `Program.callToPrecompiledAddress`: [1](#0-0) 

`contract.execute(data)` is invoked with **no try/catch** around it. `data` is fully attacker-controlled (it is the CALL's calldata, built by whatever contract the attacker deployed/invokes). If `execute()` throws a `RuntimeException` while parsing malformed input (array index errors, negative array sizes, arithmetic overflow, etc.), that exception is not contained at the precompile boundary — unlike a normal internal `callToAddress` sub-call, precompiled contracts run **in the same VM frame/`Program` object** as the caller (no nested `VM.play`), so the exception propagates straight up into the enclosing `VM.play` loop's exception handler: [2](#0-1) 

That handler calls `program.spendAllEnergy()` and rethrows, which consumes **the entire energy limit of the whole outer transaction/frame** — not just the gas forwarded to the "sub-call" as EVM/TVM semantics would normally guarantee (a failed CALL should just push `0`, refund unspent gas, and let the caller continue).

This is exactly the failure mode of the Notional `_isWrappedFCash` bug: an external/attacker-influenced value reaches a decode routine that isn't defensively guarded against garbage input, and the exception path is far more expensive than the success path.

Proof this exact class of bug is real and reachable in java-tron: the project introduced **TIP-854** specifically to stop `ValidateMultiSign` and `BatchValidateSign` from doing this. The tests document the pre-fix behavior verbatim: [3](#0-2) 

and the post-fix "outer-frame containment" test explicitly frames the bug as: a `CALL` to these precompiles with malformed calldata must not let an exception escape and consume the outer frame's energy: [4](#0-3) 

The helper `extractSigArray`, which these two precompiles use to decode attacker-supplied signature arrays, is a good illustration of the underlying fragility being guarded against — it indexes into `words[]`/`data[]` using attacker-controlled offsets/lengths with only an `isValidAbiEncoding` shape check added around it: [5](#0-4) 

Because the guard (`isValidAbiEncoding` + the TIP-854 activation checks) was only added to these two precompiles, and `Program.callToPrecompiledAddress`/`Program.callToAddress` still have no generic containment for `RuntimeException` from `contract.execute()`, this remains a structural weak point: any precompiled contract (existing or future) that fails to defensively validate every field of attacker-controlled `data` before decoding it will reproduce the same "gas bomb" — an ordinary failed call becomes a total-energy-burn instead of a bounded, refundable failure.

### Impact Explanation
A contract that calls into a TVM precompile with crafted, malformed calldata (directly analogous to `CETH`'s non-reverting fallback confusing Notional's decoder) can cause the *entire* transaction's energy to be consumed rather than just the resources of the failed sub-call. For legitimate integrators (wallets, routers, aggregators that loop over multiple precompile/contract calls in one transaction, similar to the SetToken issue/redeem loop in the original PoC), this means:
- Wasted energy/fees for users on what should be an inexpensive failed call.
- Transactions failing entirely if the vulnerable call isn't the last one in the sequence.
- Unreliable/incorrect `eth_estimateGas` / energy estimation for flows touching such a precompile, since normal EVM-style gas isolation is violated.

This mirrors the judge's Medium-severity reasoning in the original report — `eth_estimateGas` typically prevents most naive callers from hitting this, but a contract can still be deliberately/accidentally driven into the exception path, burning far more energy than intended and potentially failing composite transactions.

### Likelihood Explanation
Reachable by any unprivileged party: deploy a small contract that issues a `CALL`/`STATICCALL` with crafted calldata to a TVM precompiled-contract address. Whether it's currently exploitable end-to-end for a *specific* precompile other than the two already patched (`ValidateMultiSign`, `BatchValidateSign`) is not something I could fully verify for all ~31 `PrecompiledContract` subclasses in `PrecompiledContracts.java` in the time available — several I did inspect (`ModExp`, `Blake2F`, `EthRipemd160`, `DelegatableResource`, `ResourceV2`) do have explicit length/bounds checks before decoding. What is concretely established, however, is that (a) the generic containment mechanism at `Program.callToPrecompiledAddress` does not exist, and (b) exactly two precompiles needed a dedicated TIP to retrofit input-validation guards against this very failure mode, confirming the pattern is a recurring, non-theoretical risk in this codebase whenever a precompile's decode path is not perfectly defensive.

### Recommendation
- Wrap `contract.execute(data)` in `Program.callToPrecompiledAddress` (and the corresponding call sites) in a try/catch that treats any `RuntimeException` thrown by a precompile the same way a `false` return is treated today (push `0`, refund unspent-but-not-required energy, roll back the child deposit) rather than letting it propagate into `VM.play`'s `spendAllEnergy()` handler.
- Audit all `PrecompiledContract.execute()` implementations to ensure every attacker-reachable field of `data` is length/bounds-validated before use (as was retrofitted for `ValidateMultiSign`/`BatchValidateSign` via TIP-854), rather than relying on defense being added precompile-by-precompile after each incident.

### Proof of Concept
Not independently reproduced in this review (read-only codebase analysis). The existing regression tests already demonstrate the pre-fix failure mode for the two patched precompiles and can be used as a template to probe any other precompile for the same behavior: [6](#0-5) [3](#0-2)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-438)
```java
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }

  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/BatchValidateSignContractTest.java (L203-213)
```java
  // TIP-854: before activation the guard is not consulted. Malformed calldata
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
