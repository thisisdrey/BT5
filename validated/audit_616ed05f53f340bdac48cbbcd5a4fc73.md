### Title
CREATE2 fails to enforce call-depth limit on the legacy (non-fork-gated) code path, allowing recursion past `MAX_DEPTH` - ([File: actuator/src/main/java/org/tron/core/vm/program/Program.java])

### Summary
`Program.createContract2` (the TVM `CREATE2` opcode handler) is supposed to stop contract creation once the call stack reaches `MAX_DEPTH`, just as `createContract` (`CREATE`) unconditionally does. Instead, when neither `allowTvmCompatibleEvm` nor `allowTvmOsaka` is enabled, hitting `MAX_DEPTH` does not push zero and return; it falls through to `MUtil.checkCPUTimeForCreate2()`, which itself only throws if the `VERSION_4_8_1_1` hard fork has passed. If that fork condition is not satisfied, execution silently continues into `createContractImpl`, incrementing `nonce`, deploying a new contract and recursing further — precisely the "should refuse to proceed but instead returns/continues with an incorrect value" pattern described in the Hats `buildHatId` report, where a boundary condition that should cause a revert instead is silently accepted.

### Finding Description
`buildHatId` is meant to reject construction at the maximum hierarchy depth but instead returns a bogus id. The TVM analog is the depth-limit check for `CREATE2`: [1](#0-0) 

```java
public void createContract2(DataWord value, DataWord memStart, DataWord memSize, DataWord salt) {
  ...
  byte[] senderAddress;
  if ((VMConfig.allowTvmCompatibleEvm() || VMConfig.allowTvmOsaka())
      && getCallDeep() == MAX_DEPTH) {
    stackPushZero();
    return;
  }
  if (getCallDeep() == MAX_DEPTH) {
    MUtil.checkCPUTimeForCreate2();
  }
  ...
  createContractImpl(value, programCode, contractAddress, true);
}
```

Compare this with the unconditional guard used by plain `CREATE`: [2](#0-1) 

`checkCPUTimeForCreate2()` in `MUtil` only throws when a specific hard fork has activated: [3](#0-2) 

```java
public static void checkCPUTimeForCreate2() {
  if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_8_1_1)) {
    throw new OutOfTimeException("CPU timeout for create2 executing");
  }
}
```

So on the branch where neither `allowTvmCompatibleEvm` nor `allowTvmOsaka` is active **and** the `VERSION_4_8_1_1` fork has not passed, reaching `MAX_DEPTH` neither pushes zero nor throws — it silently proceeds to `createContractImpl`, which increments the call depth for any further internal call (`program.getCallDeep() + 1` in `ProgramInvokeFactory.createProgramInvoke`, lines 126-149) and continues executing/recursing beyond the intended depth boundary. This is directly analogous to `buildHatId` continuing past the point where it should have reverted, returning `id=0` (a value that misleads callers) instead of stopping.

This exact behavior is deliberately exercised by a project test that documents it is "a bug... but we should keep it": [4](#0-3) 

```java
public void testCreate2MaxDepthWithNeitherFlag() throws ContractValidateException {
  ...
  // With neither flag enabled the MAX_DEPTH short-circuit must not fire: CREATE2
  // proceeds, records an internal transaction and pushes the new contract
  // address (not 0), unlike the Osaka/CompatibleEvm guarded path above.
  Assert.assertFalse(program.getResult().getInternalTransactions().isEmpty());
  Assert.assertFalse(program.getStack().pop().isZero());
}
```

### Impact Explanation
Under the un-gated legacy path, an attacker-controlled deeply-nested contract-call chain (achievable in a single, unprivileged `TriggerSmartContract` transaction from any account) can drive `getCallDeep()` to `MAX_DEPTH` and beyond via repeated `CREATE2`, because the intended stop condition does not fire. Each additional level continues to consume native Java call stack frames (`VM.play` → `Program` → interpreter loop) beyond what the depth guard was meant to cap, risking uncontrolled recursion / stack exhaustion during transaction execution on a full node — a node-crash/halt-class impact rather than a purely "resource metering" nuance, since the entire purpose of the `MAX_DEPTH` check is to bound recursion depth deterministically. It also produces a different, incorrect execution result (a live new contract deployed and a non-zero address pushed to the stack) instead of the depth-limited failure (`0`) that both other TVM depth guards (`CALL`, plain `CREATE`) enforce, causing inconsistent behavior for anything relying on that invariant.

### Likelihood Explanation
The precondition — `!allowTvmCompatibleEvm() && !allowTvmOsaka()` and fork `VERSION_4_8_1_1` not yet passed — is state/configuration dependent on the chain's current hard-fork status and feature flags, so this is only reachable pre-fork/pre-feature-activation, which the project's own tests indicate is deliberately preserved for consensus-history compatibility (old blocks must replay identically). This significantly limits real-world exploitability on any network that has already activated the `VERSION_4_8_1_1` fork, since post-fork the `checkCPUTimeForCreate2()` guard does throw `OutOfTimeException`, closing the gap. I could not confirm from the available code whether any currently-active production/mainnet chain configuration still has this fork unactivated, nor whether `allowTvmCompatibleEvm`/`allowTvmOsaka` defaults leave this window open at present — this would need to be verified against the live chain parameters.

### Recommendation
Make the `MAX_DEPTH` short-circuit for `CREATE2` unconditional (matching plain `CREATE`), i.e. always `stackPushZero(); return;` at `MAX_DEPTH` regardless of `allowTvmCompatibleEvm`/`allowTvmOsaka` flags or fork state, and treat the CPU-time-based fallback purely as a historical-replay compatibility shim guarded strictly to blocks produced before the relevant fork height rather than as a live safety net.

### Proof of Concept
The project's own regression test demonstrates the flaw directly: [4](#0-3) 
With `allowTvmCompatibleEvm=0`, `allowTvmOsaka=0`, and `getCallDeep()==MAX_DEPTH (64)`, calling `program.createContract2(...)` does **not** push zero; it records a new internal transaction and pushes a non-zero (real) contract address — confirming the depth boundary was silently bypassed instead of causing the intended stop/revert.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L804-811)
```java
  @SuppressWarnings("ThrowableResultOfMethodCallIgnored")
  public void createContract(DataWord value, DataWord memStart, DataWord memSize) {
    returnDataBuffer = null; // reset return buffer right before the call

    if (getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      return;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1629-1653)
```java
  public void createContract2(DataWord value, DataWord memStart, DataWord memSize, DataWord salt) {
    if (VMConfig.allowTvmOsaka()) {
      returnDataBuffer = null; // reset return buffer right before the call
    }

    byte[] senderAddress;
    if ((VMConfig.allowTvmCompatibleEvm() || VMConfig.allowTvmOsaka())
        && getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      return;
    }
    if (getCallDeep() == MAX_DEPTH) {
      MUtil.checkCPUTimeForCreate2();
    }
    if (VMConfig.allowTvmIstanbul()) {
      senderAddress = getContextAddress();
    } else {
      senderAddress = getCallerAddress().toTronAddress();
    }
    byte[] programCode = memoryChunk(memStart.intValue(), memSize.intValue());

    byte[] contractAddress = WalletUtil
        .generateContractAddress2(senderAddress, salt.getData(), programCode);
    createContractImpl(value, programCode, contractAddress, true);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L74-78)
```java
  public static void checkCPUTimeForCreate2() {
    if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_8_1_1)) {
      throw new OutOfTimeException("CPU timeout for create2 executing");
    }
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L824-859)
```java
  @Test
  public void testCreate2MaxDepthWithNeitherFlag() throws ContractValidateException {
    boolean allowTvmCompatibleEvm = VMConfig.allowTvmCompatibleEvm();
    boolean allowTvmOsaka = VMConfig.allowTvmOsaka();
    VMConfig.initAllowTvmCompatibleEvm(0);
    VMConfig.initAllowTvmOsaka(0);
    try {
      byte[] contractAddr = Hex.decode("41471fd3ad3e9eeadeec4608b92d16ce6b500704cc");
      invoke = new ProgramInvokeMockImpl(StoreFactory.getInstance(), new byte[0], contractAddr) {
        @Override
        public int getCallDeep() {
          return 64;
        }

        @Override
        public boolean byTestingSuite() {
          return true;
        }
      };
      program = new Program(null, null, invoke,
          new InternalTransaction(Protocol.Transaction.getDefaultInstance(),
              InternalTransaction.TrxType.TRX_UNKNOWN_TYPE));
      program.setRootTransactionId(new byte[32]);

      program.createContract2(DataWord.ZERO(), DataWord.ZERO(), DataWord.ZERO(), DataWord.ZERO());

      // With neither flag enabled the MAX_DEPTH short-circuit must not fire: CREATE2
      // proceeds, records an internal transaction and pushes the new contract
      // address (not 0), unlike the Osaka/CompatibleEvm guarded path above.
      Assert.assertFalse(program.getResult().getInternalTransactions().isEmpty());
      Assert.assertFalse(program.getStack().pop().isZero());
    } finally {
      VMConfig.initAllowTvmCompatibleEvm(allowTvmCompatibleEvm ? 1 : 0);
      VMConfig.initAllowTvmOsaka(allowTvmOsaka ? 1 : 0);
    }
  }
```
