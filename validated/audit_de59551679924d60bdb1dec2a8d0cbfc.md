Found a strong, precise analog. The `UnfreezeBalanceV2Actuator`/`UnfreezeBalanceV2Processor` enforce a per-account cap (`UNFREEZE_MAX_TIMES = 32`) on outstanding unfreeze requests, and this same capped logic is directly reachable from TVM smart-contract code via the `UNFREEZEBALANCEV2` opcode, which executes in the context of the *calling contract's own address* (`getContextAddress()` as owner). Combined with TVM's `CREATE`/`CREATE2` opcodes, a single attacker transaction can atomically deploy many fresh child contracts (each a distinct on-chain address subject to its own independent 32-slot unfreeze cap) and have each immediately freeze+unfreeze TRX, letting the attacker fragment and churn far more than 32 concurrent unfreeze slots' worth of exit liquidity/vote-invalidation state in one atomic transaction — mirroring the audit's "spawn many addresses inside one tx to defeat a per-address cap" pattern.

### Title
Per-address `UNFREEZE_MAX_TIMES` cap bypassable via CREATE-spawned contract addresses in a single transaction - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java)

### Summary
`UnfreezeBalanceV2Actuator`/`UnfreezeBalanceV2Processor` enforce a hard cap of `UNFREEZE_MAX_TIMES = 32` concurrent unfreezing entries per account address [1](#0-0) . This exact check is duplicated in the TVM native processor that backs the `UNFREEZEBALANCEV2` opcode, so it is directly reachable from contract code, with the "owner" being `getContextAddress()` — i.e., the calling contract itself [2](#0-1) .

### Finding Description
`OperationActions.unfreezeBalanceV2Action`/`freezeBalanceV2Action` call into `Program.freeze`/`unfreeze`-style helpers that route through `UnfreezeBalanceV2Processor`, using the executing contract's own address as `ownerAddress` [3](#0-2) . Because `Program.createContractImpl` (backing `CREATE`/`CREATE2`) lets a single top-level transaction spawn an arbitrary number of new contract accounts and immediately execute their constructor/init code in the same transaction [4](#0-3) , an attacker contract can deploy N child contracts, each of which freezes some TRX for itself and issues unfreeze requests via the FREEZEBALANCEV2/UNFREEZEBALANCEV2 opcodes. Every child contract is a fresh address, so each gets its own independent 32-slot `UNFREEZE_MAX_TIMES` allowance — the cap is per-address, not per-transaction or per-origin, exactly mirroring the reported "cap per address" bypass pattern where cheap sub-account creation inside one atomic call defeats an anti-spam/anti-abuse limit meant to bound one user's outstanding state.

### Impact Explanation
This lets one funder atomically create unbounded numbers of parallel 32-slot unfreeze queues instead of being limited to 32 total, inflating the `UnFreezeV2` list size stored in `AccountCapsule` across many accounts within a single transaction, and can be used to force excessive iteration cost in `getUnfreezingV2Count`/`unfreezeExpire` bookkeeping and vote-recalculation paths (`updateVote`) at scale, since each child contract's frozen TRX also affects `TotalNetWeight`/`TotalEnergyWeight` and votes. Repeated at scale, this defeats the intended per-account bound on the size of unfreeze bookkeeping and can be leveraged to increase state growth and per-block work.

### Likelihood Explanation
Requires only deploying a factory contract able to invoke `CREATE` and trigger `FREEZEBALANCEV2`/`UNFREEZEBALANCEV2` via child contracts — no special privileges beyond normal TVM contract deployment and enough TRX/energy to freeze, which is available to any unprivileged account.

### Recommendation
Tie the `UNFREEZE_MAX_TIMES` cap check (and equivalent per-address anti-abuse limits reachable through TVM native ops) to `tx.origin`/the top-level transaction's owner address in addition to (or instead of) the immediate contract's own address, or otherwise account for aggregate unfreeze-slot usage across contract addresses created within the same transaction, so that atomic sub-account fan-out via `CREATE`/`CREATE2` cannot multiply the per-account allowance.

### Proof of Concept
1. Deploy a `Factory` contract that in its constructor or a single external call loops `N` times, using `CREATE` to deploy `N` instances of a `Child` contract (pattern shown in the existing test fixtures using `create`/`create2` from a factory) [5](#0-4) .
2. Each `Child` contract's constructor (or an immediately-invoked function) calls `freezeBalanceV2` for itself and then `unfreezeBalanceV2` up to 32 times, exactly hitting its own `UNFREEZE_MAX_TIMES` limit as checked in `UnfreezeBalanceV2Processor.validate` [6](#0-5) .
3. Because each `Child` is a distinct address, all `N` children succeed independently in the same top-level transaction, producing `N * 32` outstanding unfreeze entries funded/controlled by one attacker, versus the intended limit of 32 total for one actor.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L43-44)
```java
  @Getter
  private static final int UNFREEZE_MAX_TIMES = 32;
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L34-55)
```java
  public void validate(UnfreezeBalanceV2Param param, Repository repo)
      throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    if (accountCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] does not exist");
    }
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UnfreezeBalanceV2Actuator.getUNFREEZE_MAX_TIMES() <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L615-629)
```java
  public static void appendFreezeV2Operations(JumpTable table) {
    BooleanSupplier proposal = VMConfig::allowTvmFreezeV2;

    table.set(new Operation(
        Op.FREEZEBALANCEV2, 2, 1,
        EnergyCost::getFreezeBalanceV2Cost,
        OperationActions::freezeBalanceV2Action,
        proposal));

    table.set(new Operation(
        Op.UNFREEZEBALANCEV2, 2, 1,
        EnergyCost::getUnfreezeBalanceV2Cost,
        OperationActions::unfreezeBalanceV2Action,
        proposal));

```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L821-867)
```java
  private void createContractImpl(DataWord value, byte[] programCode, byte[] newAddress,
      boolean isCreate2) {
    byte[] senderAddress = getContextAddress();

    if (logger.isDebugEnabled()) {
      logger.debug("creating a new contract inside contract run: [{}]",
          Hex.toHexString(senderAddress));
    }

    long endowment = value.value().longValueExact();
    if (getContractState().getBalance(senderAddress) < endowment) {
      stackPushZero();
      return;
    }

    AccountCapsule existingAccount = getContractState().getAccount(newAddress);
    boolean contractAlreadyExists = existingAccount != null;

    if (VMConfig.allowTvmConstantinople()) {
      contractAlreadyExists =
          contractAlreadyExists && isContractExist(existingAccount, getContractState());
    }
    Repository deposit = getContractState().newRepositoryChild();
    if (VMConfig.allowTvmConstantinople()) {
      if (existingAccount == null) {
        deposit.createAccount(newAddress, "CreatedByContract",
            AccountType.Contract);
      } else if (!contractAlreadyExists) {
        existingAccount.updateAccountType(AccountType.Contract);
        existingAccount.clearDelegatedResource();
        deposit.updateAccount(newAddress, existingAccount);
      }

      if (!contractAlreadyExists) {
        Builder builder = SmartContract.newBuilder();
        if (VMConfig.allowTvmCompatibleEvm()) {
          builder.setVersion(getContractVersion());
        }
        builder.setContractAddress(ByteString.copyFrom(newAddress))
            .setConsumeUserResourcePercent(100)
            .setOriginAddress(ByteString.copyFrom(senderAddress));
        if (isCreate2) {
          builder.setTrxHash(ByteString.copyFrom(rootTransactionId));
        }
        SmartContract newSmartContract = builder.build();
        deposit.createContract(newAddress, new ContractCapsule(newSmartContract));
      }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeTest.sol (L51-63)
```text
contract Factory {

    // selector: 0x41aa9014
    // Deploy FreezeContract using CREATE (salt is unused, CREATE ignores it)
    function deployCreate2Contract(uint256 salt) public returns (address) {
        bytes memory bytecode = type(FreezeContract).creationCode;
        address addr;
        assembly {
            addr := create(0, add(bytecode, 0x20), mload(bytecode))
        }
        require(extcodesize(addr) > 0);
        return addr;
    }
```
