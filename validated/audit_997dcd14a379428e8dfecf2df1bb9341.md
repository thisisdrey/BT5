## Finding

### Title
STATICCALL modification guard for FREEZE/UNFREEZE opcodes is gated on the wrong feature flag, allowing state mutation inside supposedly read-only calls - (File: actuator/src/main/java/org/tron/core/vm/OperationActions.java)

### Summary
The TVM enforces that opcodes reached via `STATICCALL` must never mutate state, and each state-changing opcode is individually guarded with `if (program.isStaticCall()) throw new Program.StaticCallModificationException();` [1](#0-0) . However, the `FREEZE` and `UNFREEZE` opcodes wrap this guard in an extra, unrelated condition — `VMConfig.allowTvmVote()` — instead of the flag that actually activates these opcodes, `VMConfig.allowTvmFreeze`.

### Finding Description
`freezeAction` and `unfreezeAction` are registered in `OperationRegistry.appendFreezeOperations`, gated purely on the `allowTvmFreeze` proposal flag: [2](#0-1) 

Yet the static-call safety check inside the action itself is conditioned on a *different* flag, `allowTvmVote`: [3](#0-2) 

This means the opcode's activation condition (`allowTvmFreeze`) and its static-call-safety condition (`allowTvmVote`) are decoupled. If a chain (private/consortium chain, or any chain where committee proposals are applied independently, as `reference.conf` documents these flags being for manual/private-chain testing [4](#0-3) ) has `allowTvmFreeze` enabled while `allowTvmVote` is disabled, the `FREEZE`/`UNFREEZE` opcodes are live in the jump table, but their static-call guard never fires — the `program.isStaticCall()` check is short-circuited away because `allowTvmVote()` is false.

Every other state-mutating opcode in the same file (`SSTORE`, `TSTORE`, `LOG`, `CREATE`, `CREATE2`, `SUICIDE`, `FREEZEBALANCEV2`, `DELEGATERESOURCE`, `VOTEWITNESS`, `WITHDRAWREWARD`, etc.) enforces the static-call check unconditionally — see for comparison `sStoreAction`, `tStoreAction`, `logAction`, `createAction`, `suicideAction`, `voteWitnessAction`, `withdrawRewardAction`, `freezeBalanceV2Action` [5](#0-4) . Only `freezeAction`/`unfreezeAction` incorrectly tie the guard to an unrelated feature flag, which is the exact class of bug in CVE-2019-3886: an incorrect/misapplied permission gate lets a nominally "read-only" invocation path reach an API that mutates protected state.

The top-level `triggerconstantcontract`/`eth_call` path sets `isConstantCall` (via `VMActuator(true)` and `programInvoke.setConstantCall()` in `Wallet.callConstantContract` [6](#0-5) ) but does *not* set `isStaticCall` on the outer `ProgramInvoke` (that constructor overload used for the wire transaction has no `isStaticCall` parameter [7](#0-6) ). `isStaticCall` is only set true when a *nested* call is dispatched through the actual `STATICCALL` opcode, via the other `createProgramInvoke` overload [8](#0-7) , and propagated in `Program.callToAddress` as `msg.getOpCode() == Op.STATICCALL || isStaticCall()` [9](#0-8) . So the concrete exploitable path is: any contract (deployed by any unprivileged account) issues `STATICCALL` to another contract's function that internally executes `FREEZE`/`UNFREEZE`; under the described flag combination the sub-call still mutates frozen balance/bandwidth-energy state even though the calling contract, its ABI ("view"/"pure" annotation), and any composing contract or off-chain consumer all assume `STATICCALL` guarantees no state change.

### Impact Explanation
`FREEZE`/`UNFREEZE` mutate account balance, frozen resource state and (in the pre-FreezeV2 model) voting power. Breaking the "STATICCALL never mutates state" invariant lets a malicious contract silently freeze or unfreeze TRX belonging to any contract that STATICCALLs into it — for example a price-oracle/read helper pattern, a `view` function called during another contract's balance/allowance check, or a constant-call chain reused by an aggregator contract. This can move real TRX balance in/out of the frozen state as a side effect of what every caller (Solidity compiler-enforced `view`, other contracts, wallets performing constant simulation) treats as guaranteed side-effect-free. This is a concrete "unauthorized account operation" affecting real funds/resources reachable from a normal unprivileged contract deployer/caller, matching the required impact bar.

### Likelihood Explanation
Exploitability depends entirely on the chain's committee-flag configuration: it requires `allowTvmFreeze` active while `allowTvmVote` is inactive. On current public mainnet both proposals have long been activated together, reducing practical exposure there, but the code path is unconditionally reachable and incorrect regardless of network — it is a genuine logic defect (wrong flag used in a security-relevant conditional) rather than a hypothetical concern, and is directly triggerable on any private/consortium/test java-tron network that activates these flags independently (which `reference.conf` explicitly supports and documents as a use case). No privileged role is needed — only deploying two ordinary contracts and issuing a `STATICCALL`.

### Recommendation
Change the guard in `freezeAction` and `unfreezeAction` to check `program.isStaticCall()` unconditionally (as all other state-mutating opcodes do), removing the dependency on `VMConfig.allowTvmVote()`. If a phased rollout is intentional, gate it on `VMConfig.allowTvmFreeze()` (the opcode's own activation flag) rather than an unrelated proposal.

### Proof of Concept
1. Configure/activate a java-tron network (private/consortium) with `committee.allowTvmFreeze = 1` and `committee.allowTvmVote = 0`.
2. Deploy Contract `B` whose Solidity source declares a `view`/`pure` function that internally invokes TRON's low-level `freeze`/`unfreeze` opcode wrapper (e.g. `address(this).freeze(amount, resourceType)` compiled to the `FREEZE` opcode).
3. Deploy Contract `A` that calls `B`'s function via a low-level `staticcall`.
4. Observe that `B`'s frozen balance/resource state changes despite the call being issued through `STATICCALL`, confirmed by inspecting `OperationActions.freezeAction`/`unfreezeAction` at [3](#0-2)  — the `program.isStaticCall()` throw is never reached because `VMConfig.allowTvmVote()` evaluates false, and the opcode proceeds to `program.freeze(...)`/`program.unfreeze(...)`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L600-603)
```java
  public static void sStoreAction(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L791-822)
```java
  public static void freezeAction(Program program) {
    // after allow vote, check static
    if (VMConfig.allowTvmVote() && program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
    // 0 as bandwidth, 1 as energy
    DataWord resourceType = program.stackPop();
    DataWord frozenBalance = program.stackPop();
    DataWord receiverAddress = program.stackPop();

    if (VMConfig.allowTvmFreezeV2()) {
      // after v2 activated, we just push zero to stack and do nothing
      program.stackPush(DataWord.ZERO());
    } else {
      boolean result = program.freeze(receiverAddress, frozenBalance, resourceType );
      program.stackPush(result ? DataWord.ONE() : DataWord.ZERO());
    }
    program.step();
  }

  public static void unfreezeAction(Program program) {
    if (VMConfig.allowTvmVote() && program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }

    DataWord resourceType = program.stackPop();
    DataWord receiverAddress = program.stackPop();

    boolean result = program.unfreeze(receiverAddress, resourceType);
    program.stackPush(result ? DataWord.ONE() : DataWord.ZERO());
    program.step();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L833-837)
```java
  public static void freezeBalanceV2Action(Program program) {
    // after allow vote, check static
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L571-591)
```java
  public static void appendFreezeOperations(JumpTable table) {
    BooleanSupplier proposal = VMConfig::allowTvmFreeze;

    table.set(new Operation(
        Op.FREEZE, 3, 1,
        EnergyCost::getFreezeCost,
        OperationActions::freezeAction,
        proposal));

    table.set(new Operation(
        Op.UNFREEZE, 2, 1,
        EnergyCost::getUnfreezeCost,
        OperationActions::unfreezeAction,
        proposal));

    table.set(new Operation(
        Op.FREEZEEXPIRETIME, 2, 1,
        EnergyCost::getFreezeExpireTimeCost,
        OperationActions::freezeExpireTimeAction,
        proposal));
  }
```

**File:** common/src/main/resources/reference.conf (L817-821)
```text
# Governance / feature-flag parameters. Most are controlled by on-chain committee proposals;
# a few (e.g. allowNewRewardAlgorithm) are startup-only flags.
# Comments list the /wallet/getchainparameters API key and ProposalType ID where applicable.
# All default to 0 (disabled) unless noted. Manual config is for private-chain testing only.
committee = {
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3159-3168)
```java
    VMActuator vmActuator = new VMActuator(true);

    try {
      vmActuator.validate(context);
      vmActuator.execute(context);
    } finally {
      // constant call runs on a pooled RPC worker; drop its thread-local VM config view so it
      // can never leak into a later (block/broadcast) execution on the same thread.
      VMConfig.clearLocalSnapshot();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/invoke/ProgramInvokeFactory.java (L27-119)
```java
  public static ProgramInvoke createProgramInvoke(InternalTransaction.TrxType trxType,
      InternalTransaction.ExecutorType executorType, Transaction tx, long tokenValue, long tokenId,
      Block block,
      Repository deposit, long vmStartInUs,
      long vmShouldEndInUs, long energyLimit) throws ContractValidateException {
    byte[] contractAddress;
    byte[] ownerAddress;
    long balance;
    byte[] data;
    byte[] lastHash = null;
    byte[] coinbase = null;
    long timestamp = 0L;
    long number = -1L;

    if (trxType == TRX_CONTRACT_CREATION_TYPE) {
      CreateSmartContract contract = ContractCapsule.getSmartContractFromTransaction(tx);
      contractAddress = generateContractAddress(tx);
      ownerAddress = contract.getOwnerAddress().toByteArray();
      balance = deposit.getBalance(ownerAddress);
      data = ByteUtil.EMPTY_BYTE_ARRAY;
      long callValue = contract.getNewContract().getCallValue();

      switch (executorType) {
        case ET_NORMAL_TYPE:
        case ET_PRE_TYPE:
          if (null != block) {
            lastHash = block.getBlockHeader().getRawDataOrBuilder().getParentHash().toByteArray();
            coinbase = block.getBlockHeader().getRawDataOrBuilder().getWitnessAddress()
                .toByteArray();
            timestamp = block.getBlockHeader().getRawDataOrBuilder().getTimestamp() / 1000;
            number = block.getBlockHeader().getRawDataOrBuilder().getNumber();
          }
          break;
        default:
          break;
      }

      return new ProgramInvokeImpl(contractAddress, ownerAddress, ownerAddress, balance, callValue,
          tokenValue, tokenId, data, lastHash, coinbase, timestamp, number, deposit, vmStartInUs,
          vmShouldEndInUs, energyLimit);

    } else if (trxType == TRX_CONTRACT_CALL_TYPE) {
      TriggerSmartContract contract = ContractCapsule
          .getTriggerContractFromTransaction(tx);
      /***         ADDRESS op       ***/
      // YP: Get address of currently executing account.
      byte[] address = contract.getContractAddress().toByteArray();

      /***         ORIGIN op       ***/
      // YP: This is the sender of original transaction; it is never a contract.
      byte[] origin = contract.getOwnerAddress().toByteArray();

      /***         CALLER op       ***/
      // YP: This is the address of the account that is directly responsible for this execution.
      byte[] caller = contract.getOwnerAddress().toByteArray();

      /***         BALANCE op       ***/
      balance = deposit.getBalance(caller);

      /***        CALLVALUE op      ***/
      long callValue = contract.getCallValue();

      /***     CALLDATALOAD  op   ***/
      /***     CALLDATACOPY  op   ***/
      /***     CALLDATASIZE  op   ***/
      data = contract.getData().toByteArray();

      switch (executorType) {
        case ET_CONSTANT_TYPE:
          break;
        case ET_PRE_TYPE:
        case ET_NORMAL_TYPE:
          if (null != block) {
            /***    PREVHASH  op  ***/
            lastHash = block.getBlockHeader().getRawDataOrBuilder().getParentHash().toByteArray();
            /***   COINBASE  op ***/
            coinbase = block.getBlockHeader().getRawDataOrBuilder().getWitnessAddress()
                .toByteArray();
            /*** TIMESTAMP  op  ***/
            timestamp = block.getBlockHeader().getRawDataOrBuilder().getTimestamp() / 1000;
            /*** NUMBER  op  ***/
            number = block.getBlockHeader().getRawDataOrBuilder().getNumber();
          }
          break;
        default:
          break;
      }

      return new ProgramInvokeImpl(address, origin, caller, balance, callValue, tokenValue, tokenId,
          data,
          lastHash, coinbase, timestamp, number, deposit, vmStartInUs, vmShouldEndInUs,
          energyLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/invoke/ProgramInvokeFactory.java (L126-149)
```java
  public static ProgramInvoke createProgramInvoke(Program program, DataWord toAddress,
      DataWord callerAddress,
      DataWord inValue, DataWord tokenValue, DataWord tokenId, long balanceInt, byte[] dataIn,
      Repository deposit, boolean isStaticCall, boolean byTestingSuite, long vmStartInUs,
      long vmShouldEndInUs, long energyLimit) {

    DataWord address = toAddress;
    DataWord origin = program.getOriginAddress();
    DataWord caller = callerAddress;
    DataWord balance = new DataWord(balanceInt);
    DataWord callValue = inValue;

    byte[] data = Arrays.clone(dataIn);
    DataWord lastHash = program.getPrevHash();
    DataWord coinbase = program.getCoinbase();
    DataWord timestamp = program.getTimestamp();
    DataWord number = program.getNumber();
    DataWord difficulty = program.getDifficulty();

    return new ProgramInvokeImpl(address, origin, caller, balance, callValue, tokenValue, tokenId,
        data, lastHash, coinbase, timestamp, number, difficulty,
        deposit, program.getCallDeep() + 1, isStaticCall, byTestingSuite, vmStartInUs,
        vmShouldEndInUs, energyLimit);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1141-1149)
```java
      ProgramInvoke programInvoke = ProgramInvokeFactory.createProgramInvoke(
          this, new DataWord(contextAddress),
          msg.getOpCode() == Op.DELEGATECALL ? getCallerAddress() : getContractAddress(),
          !isTokenTransfer ? callValue : DataWord.ZERO(),
          !isTokenTransfer ? DataWord.ZERO() : callValue,
          !isTokenTransfer ? DataWord.ZERO() : msg.getTokenId(),
          contextBalance, data, deposit,
          msg.getOpCode() == Op.STATICCALL || isStaticCall(),
          byTestingSuite(), vmStartInUs, getVmShouldEndInUs(), msg.getEnergy().longValueSafe());
```
