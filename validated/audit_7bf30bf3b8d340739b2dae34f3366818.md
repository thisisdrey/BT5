### Title
Native contract precompiles (freeze/vote/delegate/withdraw-reward via TVM `CALL`) accept and permanently lock TRX/TRC10 sent as `msg.value` - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
The reported bug class is: functions whose purpose does not involve moving native value fail to reject a non-zero `msg.value`, so any native funds accidentally attached get stuck in a contract balance that nobody can move out. The java-tron analog is `Program.callToPrecompiledAddress`, which is the code path executed whenever a smart contract issues a TVM `CALL`/`CALLTOKEN` to one of the native "precompiled" resource contracts (freeze/unfreeze/vote/delegate resource/withdraw reward, etc.). This method unconditionally transfers the call's `endowment` (TRX or TRC10 value) into the precompile's pseudo-account balance before invoking the precompile logic, even though none of those native operations are designed to consume or refund that value.

### Finding Description
`Program.callToPrecompiledAddress` is reached from `OperationActions.callAction`/`callTokenAction`, which pop an arbitrary `value` (endowment) off the EVM stack for any `CALL`/`CALLCODE`/`CALLTOKEN` target, including native precompiled contract addresses [1](#0-0) .

In `callToPrecompiledAddress`, the endowment is validated only for sufficient sender balance, then unconditionally transferred/credited to the precompile's `contextAddress` balance (TRX via `MUtil.transfer`, or TRC10 via `deposit.addTokenBalance`) *before* the precompile's `contract.execute(data)` is invoked [2](#0-1) . There is no check that `endowment` must be zero for precompile targets whose semantics (e.g., freeze balance, vote witness, delegate resource, withdraw reward) have no use for attached native value — analogous to the `AnyswapFacet`/`CBridgeFacet`/`HopFacet`/`NXTPFacet` bridging functions in the external report that never checked `msg.value == 0`.

Precompile implementations such as `VoteWitnessProcessor`, `WithdrawRewardProcessor`, `UnfreezeBalanceV2Processor`, etc. operate purely on the caller's/receiver's account resource/vote/reward state; they never read back or spend the value that was pre-credited to the precompile pseudo-address by `callToPrecompiledAddress`. Because the precompile address is not a real account with a signing key or a designated actuator to withdraw its balance, any TRX or TRC10 credited to it via this endowment path is effectively unreachable through normal transaction flows.

### Impact Explanation
Any TRX or TRC10 tokens mistakenly (or via a malicious/buggy DApp contract) attached as `value`/`tokenValue` to a `CALL`/`CALLTOKEN` targeting a native precompiled resource contract are permanently locked in the precompile's balance, since:
1. The precompile's `execute()` never spends or forwards this balance.
2. No actuator or opcode exists to withdraw balance from a precompile pseudo-address.

This constitutes a permanent freezing-of-funds condition reachable from ordinary contract execution triggered by a signed `TriggerSmartContract` transaction, without requiring any privileged role.

### Likelihood Explanation
Likelihood is moderate: it requires a smart contract (deployed by any unprivileged deployer) to make a `CALL`/`CALLTOKEN` to a native precompile address with non-zero value, which can happen either by developer error (analogous to the original ERC20 bridging report) or by a malicious contract designed to siphon/trap funds sent through it. Given TRON smart contracts frequently interact with native staking/voting precompiles, the surface is broadly reachable by any unprivileged contract caller or deployer.

### Recommendation
In `Program.callToPrecompiledAddress`, reject (revert the call, push zero, and refund energy) whenever `endowment > 0` and the target precompiled contract does not declare itself as accepting value — i.e., require `msg.value`/`tokenValue` to be zero for precompiles whose logic has no use for attached native funds, mirroring the recommended fix from the external report ("revert when bridging/non-native-value functions are called with non-zero native amount").

### Proof of Concept
1. Deploy a Solidity contract on java-tron that performs `address(NATIVE_FREEZE_PRECOMPILE_ADDR).call{value: X}(freezeCalldata)` (or the TRC10 equivalent using `CALLTOKEN`).
2. Trace execution: `OperationActions.callAction`/`callTokenAction` (actuator/src/main/java/org/tron/core/vm/OperationActions.java:969-999) pops `value` and calls `exeCall` → `Program.callToAddress`/`callToPrecompiledAddress`.
3. In `callToPrecompiledAddress` (actuator/src/main/java/org/tron/core/vm/program/Program.java:1690-1732), the endowment `X` is transferred into the precompile pseudo-account's balance regardless of the operation being performed.
4. `contract.execute(data)` runs the freeze/vote/etc. logic without ever consuming or returning `X`.
5. Query the precompile pseudo-address's balance afterward — the `X` amount remains permanently credited there with no code path to retrieve it. [3](#0-2) [4](#0-3)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1670-1732)
```java
  public void callToPrecompiledAddress(MessageCall msg,
      PrecompiledContracts.PrecompiledContract contract) {
    returnDataBuffer = null; // reset return buffer right before the call

    if (getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      this.refundEnergy(msg.getEnergy().longValue(), " call deep limit reach");
      return;
    }

    Repository deposit = getContractState().newRepositoryChild();

    byte[] senderAddress = getContextAddress();
    byte[] contextAddress;
    if (msg.getOpCode() == Op.CALLCODE || msg.getOpCode() == Op.DELEGATECALL) {
      contextAddress = senderAddress;
    } else {
      contextAddress = msg.getCodeAddress().toTronAddress();
    }

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
