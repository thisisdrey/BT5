### Title
CREATE2 contract-address squatting lets an attacker DoS a legitimate CREATE2 deployment (and hijack funds sent to the pre-computed address) - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
The external report's bug class is: a user picks a deterministic, application-level identifier before broadcasting a transaction; because of a delay between broadcast and on-chain execution, an attacker can observe the intended identifier and race the user by claiming it first, causing the user's transaction to permanently revert once it lands (griefing/DoS, with possible loss of pre-committed funds). The closest reachable analog in java-tron's actuator/opcode layer is `CREATE2`, where the resulting contract address is fully deterministic from `(deployer, salt, init-code hash)` and can be computed in advance by anyone watching the mempool.

### Finding Description
`Program.createContract`/`createContractImpl` implements both `CREATE` and `CREATE2`. For `CREATE2`, the new contract address is computed off-chain-predictably via `WalletUtil.generateContractAddress2(address, salt, code)`: [1](#0-0) 

Inside `createContractImpl`, before executing the constructor, the VM checks whether an account/contract already exists at that predicted address: [2](#0-1) 

If a contract is already deployed at that address (`contractAlreadyExists == true`), the create is rejected with a `BytecodeExecutionException`, the internal transaction is rejected, and `0` is pushed to the stack, but the calling transaction still spends the CREATE-related energy that was already charged before the check: [3](#0-2) [4](#0-3) 

Because the salt and init-code (and therefore the resulting address) are chosen by the legitimate deployer and can be observed in the mempool before the transaction is confirmed, an adversary can:
1. Observe a pending transaction that will trigger a factory contract's `CREATE2` call with a known salt/init-code.
2. Compute the same target address off-chain using the identical formula.
3. Front-run with their own transaction that deploys arbitrary bytecode (or even just funds an EOA that becomes an account) at that exact address before the victim's transaction executes.
4. When the victim's transaction lands, `contractAlreadyExists` is `true`, the victim's `CREATE2` fails, energy already spent up to that point is burned, and (depending on the factory's Solidity logic) the whole call may revert.

This mirrors the reported bug class precisely: a delay between transaction submission and execution lets an attacker squat a user-chosen deterministic identifier, permanently denying the legitimate operation and wasting the victim's resources. It is also strictly worse than the original report in one respect: many CREATE2-based patterns (e.g. counterfactual wallet instantiation, deterministic escrow addresses) rely on users or third parties sending TRX/TRC10 to the not-yet-deployed address in advance. If the attacker deploys different bytecode at that address first, any value already sent there can be redirected/controlled by attacker-authored code, not just griefed.

### Impact Explanation
- Denial of service against any application that depends on CREATE2 for deterministic addressing (factory patterns, minimal-proxy clones, counterfactual accounts), a common pattern for wallets/escrows/exchanges deployed on TRON.
- Loss of already-spent CREATE energy/fee for the victim on each failed attempt, and repeatable by the same attacker for the same salt, similar to the original report's "griefing" impact.
- In counterfactual-deployment patterns, an attacker who wins the address race can deploy attacker-controlled bytecode at the address funds were expected to be sent to, leading to potential freezing or theft of those funds — a more severe than "griefing-only" outcome.

### Likelihood Explanation
- The only prerequisite is a public factory contract exposing a `CREATE2` call whose salt is either fixed, incremental, or otherwise predictable/observable (a common and encouraged Solidity pattern for deterministic deployments).
- The attacker needs no special privileges — it is reachable purely from unprivileged `TriggerSmartContract` calls, i.e. any account that can pay for energy/bandwidth.
- Mempool front-running of pending transactions is a standard capability on TRON (as on any EVM-like chain), so the timing window matches the "delay before execution" condition in the original report.

### Recommendation
- Document and encourage salts to incorporate `msg.sender` or another caller-bound value so a pre-computed address cannot be squatted by a third party (this is a Solidity/application-level mitigation, matching the original report's recommendation to bind identifiers to the caller).
- Consider adding TVM-level guidance or a reference implementation demonstrating safe CREATE2 usage (e.g., factory-level authorization or commit-reveal salts) since the underlying opcode behavior in `Program.createContractImpl` is standard/EVM-compatible and cannot itself be changed without breaking compatibility.

### Proof of Concept
1. Deploy a `Factory` contract exposing `deploy(bytes code, uint256 salt)` implemented with the `create2` opcode (same pattern already covered by java-tron's own test, `Create2Test.testCreate2`, which calls `WalletUtil.generateContractAddress2` to compute the deterministic address): [5](#0-4) 
2. As the victim, broadcast a `TriggerSmartContract` transaction calling `deploy(testCode, salt)`.
3. As the attacker, independently compute the same address off-chain via `generateContractAddress2(factoryAddress, salt, testCode)` and broadcast a competing transaction that gets included first, deploying different bytecode (or simply transferring TRX to create an account) at that exact address.
4. When the victim's transaction executes, `createContractImpl` finds `contractAlreadyExists == true`, sets a `BytecodeExecutionException`, rejects the internal transaction, and pushes `0` — the victim's deployment permanently fails for that salt while the attacker's contract occupies the address, matching lines 836-919 of `Program.java` referenced above.

### Citations

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L55-59)
```java
  // for `CREATE2`
  public static byte[] generateContractAddress2(byte[] address, byte[] salt, byte[] code) {
    byte[] mergedData = ByteUtil.merge(address, salt, Hash.sha3(code));
    return Hash.sha3omit12(mergedData);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L836-842)
```java
    AccountCapsule existingAccount = getContractState().getAccount(newAddress);
    boolean contractAlreadyExists = existingAccount != null;

    if (VMConfig.allowTvmConstantinople()) {
      contractAlreadyExists =
          contractAlreadyExists && isContractExist(existingAccount, getContractState());
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L896-919)
```java

    // actual energy subtract
    DataWord energyLimit = this.getCreateEnergy(getEnergyLimitLeft());
    spendEnergy(energyLimit.longValue(), "internal call");

    increaseNonce();
    // [5] COOK THE INVOKE AND EXECUTE
    InternalTransaction internalTx = addInternalTx(null, senderAddress, newAddress, endowment,
        programCode, "create", nonce, null);
    long vmStartInUs = System.nanoTime() / 1000;
    ProgramInvoke programInvoke = ProgramInvokeFactory.createProgramInvoke(
        this, new DataWord(newAddress), getContractAddress(), value, DataWord.ZERO(),
        DataWord.ZERO(),
        newBalance, null, deposit, false, byTestingSuite(), vmStartInUs,
        getVmShouldEndInUs(), energyLimit.longValueSafe());
    if (isConstantCall()) {
      programInvoke.setConstantCall();
    }
    ProgramResult createResult = ProgramResult.createEmpty();

    if (contractAlreadyExists) {
      createResult.setException(new BytecodeExecutionException(
          "Trying to create a contract with existing contract address: 0x" + Hex
              .toHexString(newAddress)));
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L959-976)
```java
    if (createResult.getException() != null || createResult.isRevert()) {
      logger.debug("contract run halted by Exception: contract: [{}], exception: [{}]",
          Hex.toHexString(newAddress),
          createResult.getException());

      if (internalTx != null) {
        internalTx.reject();
      }

      createResult.rejectInternalTransactions();

      stackPushZero();

      if (createResult.getException() != null) {
        return;
      } else {
        returnDataBuffer = createResult.getHReturn();
      }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/Create2Test.java (L113-140)
```java
  @Test
  public void testCreate2()
      throws ContractExeException, ReceiptCheckErrException,
      VMIllegalException, ContractValidateException {
    manager.getDynamicPropertiesStore().saveAllowTvmTransferTrc10(1);
    manager.getDynamicPropertiesStore().saveAllowTvmConstantinople(1);
    manager.getDynamicPropertiesStore().saveAllowTvmIstanbul(0);
    String contractName = "Factory_0";
    byte[] address = Hex.decode(OWNER_ADDRESS);
    String abi = "[{\"constant\":false,\"inputs\":[{\"name\":\"code\",\"type\":\"bytes\"},"
        + "{\"name\":\"salt\",\"type\":\"uint256\"}],\"name\":\"deploy\",\"outputs\":[{\"name\""
        + ":\"\",\"type\":\"address\"}],\"payable\":false,\"stateMutability\":\"nonpayable\","
        + "\"type\":\"function\"},{\"anonymous\":false,\"inputs\":[{\"indexed\":false,\"name\":"
        + "\"addr\",\"type\":\"address\"},{\"indexed\":false,\"name\":\"salt\",\"type\":\"uint256\""
        + "}],\"name\":\"Deployed\",\"type\":\"event\"}]";

    String factoryCode = "608060405234801561001057600080fd5b50d3801561001d57600080fd5b50d2801561002"
        + "a57600080fd5b506101c18061003a6000396000f3fe6080604052600436106100245760003560e01c63fffff"
        + "fff1680639c4ae2d014610029575b600080fd5b34801561003557600080fd5b50d3801561004257600080fd5"
        + "b50d2801561004f57600080fd5b506100f86004803603604081101561006657600080fd5b810190602081018"
        + "13564010000000081111561008157600080fd5b82018360208201111561009357600080fd5b8035906020019"
        + "18460018302840111640100000000831117156100b557600080fd5b91908080601f016020809104026020016"
        + "0405190810160405280939291908181526020018383808284376000920191909152509295505091359250610"
        + "121915050565b6040805173ffffffffffffffffffffffffffffffffffffffff9092168252519081900360200"
        + "190f35b600080828451602086016000f59050803b151561013d57600080fd5b6040805173fffffffffffffff"
        + "fffffffffffffffffffffffff831681526020810185905281517fb03c53b28e78a88e31607a27e1fa48234dc"
        + "e28d5d9d9ec7b295aeb02e674a1e1929181900390910190a1939250505056fea165627a7a7230582079653f6"
        + "506bd7d3bdf4954ec98c452c5455d2b11444642db00b38fa422b25a650029";
```
