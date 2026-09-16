## Analysis

The BathBuddy bug class is: a contract can receive value but has no mechanism to ever move that value back out, permanently locking funds. The closest reachable analog in java-tron is in the TVM's precompiled-contract call path.

When a smart contract executes a `CALL` (or similar) opcode targeting a precompiled contract address (e.g. `0x01`–`0x09`, `ecRecover`, `sha256`, etc.) and supplies a non-zero `value`/endowment, `Program.callToPrecompiledAddress` unconditionally transfers that TRX (or TRC10 token) balance into the precompiled address's account before invoking the precompiled logic: [1](#0-0) 

Precompiled addresses (`ecRecoverAddr`, `sha256Addr`, etc.) are hard-coded system addresses defined in `PrecompiledContracts`, with no corresponding private key, actuator, or opcode path that ever withdraws or spends the balance credited to them: [2](#0-1) 

Any signed transaction that triggers a smart contract calling one of these precompiled addresses with a non-zero value will silently and permanently credit TRX (or TRC10 tokens) to that address's balance, with no code path in the protocol — no `TransferActuator`, no VM opcode, no actuator — capable of ever moving it out again. This mirrors the reported bug class exactly: value accepted by a "receiving" entity with no corresponding withdrawal logic, resulting in permanent freezing of funds, and it is reachable by an ordinary contract-triggering transaction (via `TriggerSmartContractContract` executing user or attacker-supplied bytecode that performs a `CALL` with value to a precompile address), not by any privileged/malicious-SR or p2p actor.

### Title
Value sent to TVM precompiled-contract addresses via CALL is permanently locked with no withdrawal path - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
`Program.callToPrecompiledAddress` transfers the call's TRX/TRC10 endowment into the target precompiled address's account balance before executing the precompiled logic, but precompiled addresses have no owning key and are never touched by any actuator or opcode capable of moving funds back out.

### Finding Description
Whenever a message call targets a known precompiled address (`ecRecover`, `sha256`, `ripemd160`, etc., defined in `PrecompiledContracts` at fixed addresses `0x01`-`0x09` and beyond) and specifies a positive `endowment`, the TVM performs an unconditional balance transfer to that address: [3](#0-2) 
This is analogous to `TransferActuator`'s normal balance credit but with the crucial difference that no actuator, opcode, or key exists for these system addresses that could ever debit the balance again — unlike a normal account (whose owner can sign a `TransferContract`) or the black-hole address (which is explicitly designed to permanently burn, tracked via `DynamicPropertiesStore.burnTrx`): [4](#0-3) 
Funds sent to precompile addresses are neither burned/tracked as intentional burn nor recoverable, so they are simply orphaned in the account state forever.

### Impact Explanation
This causes a permanent freezing of user funds (TRX or TRC10 tokens) that is unrecoverable by design, since the target address is a protocol-defined constant with no associated private key and no actuator path to spend from it. Any contract (deliberately or accidentally, e.g. through delegatecall/proxy patterns forwarding value, or a bug in relayed calls) that ends up calling one of these fixed addresses with value permanently destroys that value without it being accounted as an intentional burn.

### Likelihood Explanation
Reaching this path only requires a standard `TriggerSmartContractContract` transaction whose contract bytecode performs a `CALL`/similar opcode with non-zero value to one of the fixed precompile addresses — something any transaction broadcaster or contract deployer can trigger without any special privilege, matching the reachable actor set for this analysis (unprivileged transaction broadcaster / contract deployer / API client).

### Recommendation
In `Program.callToPrecompiledAddress`, reject (revert) message calls that carry a non-zero endowment/value targeting a precompiled address, mirroring how `TransferActuator` already forbids TRX transfers to certain restricted destinations. Alternatively, redirect any such value to the black-hole address so it is at least tracked as an intentional, accounted-for burn via `DynamicPropertiesStore.burnTrx` rather than being silently orphaned.

### Proof of Concept
1. Deploy a contract `Attacker` with a function `send()` that does `precompileAddr.call{value: X}("")` where `precompileAddr` is `0x0000...0001` (the `ecRecover` address).
2. Fund `Attacker` with TRX and call `send()` via `TriggerSmartContractContract`.
3. Observe (via `callToPrecompiledAddress`, lines 1712-1721) that `X` TRX is transferred into the account at `0x0000...0001`.
4. Confirm no actuator, opcode, or API path (e.g., `TransferActuator`, `SUICIDE`) can ever move that balance out of `0x0000...0001`, since no key or contract code exists for it — the funds are permanently locked.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1704-1721)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L133-140)
```java
  private static final DataWord ecRecoverAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000001");
  private static final DataWord sha256Addr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000002");
  private static final DataWord ripempd160Addr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000003");
  private static final DataWord identityAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000004");
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L2455-2461)
```java
  public void burnTrx(long amount) {
    if (amount <= 0) {
      return;
    }
    amount += getBurnTrxAmount();
    saveBurnTrx(amount);
  }
```
