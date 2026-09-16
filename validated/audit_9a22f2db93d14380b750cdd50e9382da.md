### Title
CALL/CALLCODE with non-zero endowment to a precompiled contract address permanently locks TRX with no accounting or recovery path - ([File: actuator/src/main/java/org/tron/core/vm/program/Program.java])

### Summary
When a smart contract executes `CALL` (or `CALLCODE`) with `value > 0` against an address that resolves to a TVM precompiled contract (e.g. `ecRecover`, `sha256`, `TotalVoteCount`, `RewardBalance`, etc.), the TVM unconditionally transfers the TRX endowment into that precompiled address's balance before invoking the precompile logic, but no precompiled contract implementation accounts for, forwards, or allows withdrawal of that value. Because precompiled addresses (0x01, 0x02, ... and the TRON-specific vote/freeze query precompiles) have no known private key, any TRX sent this way is permanently and unrecoverably locked, mirroring the analog report's "no accounting for received native tokens / no withdrawal method" pattern.

### Finding Description
`Program.callToPrecompiledAddress()` resolves the message-call target via `PrecompiledContracts.getContractForAddress()` and, before executing the precompile, unconditionally moves the attached endowment from the caller to the precompiled contract's `contextAddress`: [1](#0-0) 

This transfer happens purely based on `msg.getEndowment().value().longValueExact() > 0`, with no check that the destination is a "real", accountable contract capable of handling value. It is only skipped/rolled back if the precompile's `execute()` subsequently fails (the child `deposit` is simply not committed): [2](#0-1) 

None of the precompiled contract implementations in `PrecompiledContracts.java` (e.g. `ecRecover`, `sha256`, `TotalVoteCount`, `RewardBalance`, `IsSrCandidate`, etc.) contain any logic to record, forward, or refund a received balance — they are pure computation/query functions: [3](#0-2) 

The routing that decides whether a `CALL` targets a precompile happens in `exeCall`, which does not reject non-zero value for precompiled destinations: [4](#0-3) 

and `callAction`/`callTokenAction` do not prevent attaching TRX value to such calls either (they only block value on `STATICCALL`): [5](#0-4) 

The full list of resolvable precompiled addresses (all reachable via `getContractForAddress`) shows the attack surface is broad — including always-active precompiles like `ecRecover`, `sha256`, `ripemd160`, `identity`, `modExp`, and feature-gated ones like the TVM vote/reward query precompiles: [6](#0-5) 

Since these are fixed, deterministic addresses derived from small integers (not from any real key pair), there is no private key that controls them, so any TRX credited to their balance via this path can never be spent or withdrawn by anyone.

### Impact Explanation
Any transaction or contract logic that inadvertently (or via a bug in caller-side contract code, e.g. a low-level `.call{value: x}()`-style pattern compiled to bytecode) sends TRX alongside a call whose target address happens to be a precompiled contract address causes permanent loss of that TRX — a real, unbacked/unspendable balance is created at an address nobody controls. This is a permanent freezing-of-funds condition reachable by any ordinary user or contract calling a smart contract with attacker- or user-supplied call targets (e.g. proxies, multicall/relayer contracts, or contracts allowing arbitrary low-level calls) without validating that a value-bearing call isn't directed at a precompile address.

### Likelihood Explanation
This requires only a single `TriggerSmartContract` transaction that executes a `CALL`/`CALLCODE` opcode with non-zero `value` targeting one of the fixed precompile addresses (e.g. `0x01`-`0x09`, `0x100`, or the TRON vote/reward query addresses). No special privilege is needed — it's reachable by any unprivileged contract deployer/caller, particularly through generic proxy/forwarder/multicall contracts that pass through arbitrary `(target, value, data)` tuples supplied by end users, a common pattern in DeFi-style contracts deployed on TRON.

### Recommendation
In `Program.callToPrecompiledAddress()`, reject (or refund/refuse) the message call when `msg.getEndowment().value().longValueExact() > 0` and the target resolves to a precompiled contract that does not explicitly declare itself payable/value-aware, e.g.:
```java
if (endowment > 0) {
    stackPushZero();
    refundEnergy(msg.getEnergy().longValue(), REFUND_ENERGY_FROM_MESSAGE_CALL);
    return;
}
```
placed before the endowment-transfer block at `actuator/src/main/java/org/tron/core/vm/program/Program.java:1712-1732`, unless a specific precompile is intentionally designed to receive and account for TRX.

### Proof of Concept
1. Deploy a contract `Forwarder` with a function `forward(address target, uint256 value, bytes data) external payable { target.call{value: value}(data); }`.
2. Call `forward(0x0000000000000000000000000000000000000001, 1000000, hex"...")` (address `0x01` = `ecRecover`) with `callValue = 1000000` sun attached to the outer `TriggerSmartContract` transaction, and valid ecRecover input data so `execute()` succeeds.
3. Observe via `callToPrecompiledAddress` (`actuator/src/main/java/org/tron/core/vm/program/Program.java:1712-1732`) that 1,000,000 sun is transferred from `Forwarder` to account `0x01` and, because `execute()` returns success, `deposit.commit()` persists the balance change.
4. Query the balance of address `0x01` (or the relevant precompile address) — the TRX is now credited there permanently, with no known key or protocol mechanism to ever move or withdraw it.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1690-1732)
```java
    long endowment = msg.getEndowment().value().longValueExact();
    long senderBalance = 0;
    byte[] tokenId = null;

    checkTokenId(msg);
    boolean isTokenTransfer = isTokenTransfer(msg);
    // transfer TRX validation
    if (!isTokenTransfer) {
      senderBalance = deposit.getBalance(senderAddress);
    } else {
      // transfer trc10 token validation
      tokenId = String.valueOf(msg.getTokenId().longValue()).getBytes();
      senderBalance = deposit.getTokenBalance(senderAddress, tokenId);
    }
    if (senderBalance < endowment) {
      stackPushZero();
      refundEnergy(msg.getEnergy().longValue(), REFUND_ENERGY_FROM_MESSAGE_CALL);
      return;
    }
    byte[] data = this.memoryChunk(msg.getInDataOffs().intValue(),
        msg.getInDataSize().intValue());

    // Charge for endowment - is not reversible by rollback
    if (!ArrayUtils.isEmpty(senderAddress) && !ArrayUtils.isEmpty(contextAddress)
        && senderAddress != contextAddress && msg.getEndowment().value().longValueExact() > 0) {
      if (!isTokenTransfer) {
        try {
          MUtil.transfer(deposit, senderAddress, contextAddress,
              msg.getEndowment().value().longValueExact());
        } catch (ContractValidateException e) {
          throw new BytecodeExecutionException("transfer failure");
        }
      } else {
        try {
          VMUtils
              .validateForSmartContract(deposit, senderAddress, contextAddress, tokenId, endowment);
        } catch (ContractValidateException e) {
          throw new BytecodeExecutionException(VALIDATE_FOR_SMART_CONTRACT_FAILURE, e.getMessage());
        }
        deposit.addTokenBalance(senderAddress, tokenId, -endowment);
        deposit.addTokenBalance(contextAddress, tokenId, endowment);
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1734-1774)
```java
    long requiredEnergy = contract.getEnergyForData(data);
    if (requiredEnergy > msg.getEnergy().longValue()) {
      // Not need to throw an exception, method caller needn't know that
      // regard as consumed the energy
      this.refundEnergy(0, CALL_PRE_COMPILED); //matches cpp logic
      this.stackPushZero();
    } else {
      // Delegate or not. if is delegated, we will use msg sender, otherwise use contract address
      if (msg.getOpCode() == Op.DELEGATECALL) {
        contract.setCallerAddress(getCallerAddress().toTronAddress());
      } else {
        contract.setCallerAddress(getContextAddress());
      }
      // this is the depositImpl, not contractState as above
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
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

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
      } else {
        this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
      }
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L224-284)
```java
  public static PrecompiledContract getContractForAddress(DataWord address) {

    if (address == null) {
      return identity;
    }
    if (address.equals(ecRecoverAddr)) {
      return ecRecover;
    }
    if (address.equals(sha256Addr)) {
      return sha256;
    }
    if (address.equals(ripempd160Addr)) {
      return ripempd160;
    }
    if (address.equals(identityAddr)) {
      return identity;
    }
    // Byzantium precompiles
    if (address.equals(modExpAddr)) {
      return modExp;
    }
    if (address.equals(altBN128AddAddr)) {
      return altBN128Add;
    }
    if (address.equals(altBN128MulAddr)) {
      return altBN128Mul;
    }
    if (address.equals(altBN128PairingAddr)) {
      return altBN128Pairing;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(validateMultiSignAddr)) {
      return validateMultiSign;
    }
    if (VMConfig.allowShieldedTRC20Transaction() && address.equals(verifyMintProofAddr)) {
      return verifyMintProof;
    }
    if (VMConfig.allowShieldedTRC20Transaction() && address.equals(verifyTransferProofAddr)) {
      return verifyTransferProof;
    }
    if (VMConfig.allowShieldedTRC20Transaction() && address.equals(verifyBurnProofAddr)) {
      return verifyBurnProof;
    }
    if (VMConfig.allowShieldedTRC20Transaction() && address.equals(merkleHashAddr)) {
      return merkleHash;
    }
    if (VMConfig.allowTvmVote() && address.equals(rewardBalanceAddr)) {
      return rewardBalance;
    }
    if (VMConfig.allowTvmVote() && address.equals(isSrCandidateAddr)) {
      return isSrCandidate;
    }
    if (VMConfig.allowTvmVote() && address.equals(voteCountAddr)) {
      return voteCount;
    }
    if (VMConfig.allowTvmVote() && address.equals(usedVoteCountAddr)) {
      return usedVoteCount;
    }
    if (VMConfig.allowTvmVote() && address.equals(receivedVoteCountAddr)) {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1945-1974)
```java
  public static class TotalVoteCount extends PrecompiledContract {

    @Override
    public long getEnergyForData(byte[] data) {
      return 20;
    }

    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      if (data == null || data.length != WORD_SIZE) {
        return Pair.of(true, longTo32Bytes(0L));
      }

      byte[] address = new DataWord(data).toTronAddress();
      AccountCapsule accountCapsule = this.getDeposit().getAccount(address);

      long tronPower;
      if (accountCapsule == null) {
        tronPower = 0;
      } else {
        if (getDeposit().getDynamicPropertiesStore().supportUnfreezeDelay()
            && getDeposit().getDynamicPropertiesStore().supportAllowNewResourceModel()) {
          tronPower = accountCapsule.getAllTronPower();
        } else {
          tronPower = accountCapsule.getTronPower();
        }
      }
      return Pair.of(true, longTo32Bytes(tronPower / TRX_PRECISION));
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L969-999)
```java
  public static void callAction(Program program) {
    // use adjustedCallEnergy instead of requested
    program.stackPop();
    DataWord codeAddress = program.stackPop();
    DataWord value = program.stackPop();

    if (program.isStaticCall() && !value.isZero()) {
      throw new Program.StaticCallModificationException();
    }
    DataWord adjustedCallEnergy = program.getAdjustedCallEnergy();
    if (!value.isZero()) {
      adjustedCallEnergy.add(new DataWord(EnergyCost.getStipendCallCost()));
    }
    exeCall(program, adjustedCallEnergy, codeAddress, value, DataWord.ZERO(), false);
  }

  public static void callTokenAction(Program program) {
    program.stackPop();
    DataWord codeAddress = program.stackPop();
    DataWord value = program.stackPop();

    if (program.isStaticCall() && !value.isZero()) {
      throw new Program.StaticCallModificationException();
    }
    DataWord adjustedCallEnergy = program.getAdjustedCallEnergy();
    if (!value.isZero()) {
      adjustedCallEnergy.add(new DataWord(EnergyCost.getStipendCallCost()));
    }
    DataWord tokenId = program.stackPop();
    exeCall(program, adjustedCallEnergy, codeAddress, value, tokenId, VMConfig.allowMultiSign());
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L1031-1057)
```java
  public static void exeCall(Program program, DataWord adjustedCallEnergy,
      DataWord codeAddress, DataWord value, DataWord tokenId, boolean isTokenTransferMsg) {

    DataWord inDataOffs = program.stackPop();
    DataWord inDataSize = program.stackPop();

    DataWord outDataOffs = program.stackPop();
    DataWord outDataSize = program.stackPop();

    program.memoryExpand(outDataOffs, outDataSize);
    int op = program.getCurrentOpIntValue();
    MessageCall msg = new MessageCall(
        op, adjustedCallEnergy, codeAddress, value, inDataOffs, inDataSize,
        outDataOffs, outDataSize, tokenId, isTokenTransferMsg);

    PrecompiledContracts.PrecompiledContract contract =
        PrecompiledContracts.getContractForAddress(codeAddress);
    if (contract != null) {
      if (program.isConstantCall()) {
        contract =  PrecompiledContracts.getOptimizedContractForConstant(contract);
      }
      program.callToPrecompiledAddress(msg, contract);
    } else {
      program.callToAddress(msg);
    }
    program.step();
  }
```
