[1](#0-0) Based on the smart-contract owner's ability to change `consume_user_resource_percent` and `origin_energy_limit` for a deployed contract at any time, with immediate effect and no timelock, I found a valid analog within the allowed attack surface.

### Title
Contract owner can instantly change `consume_user_resource_percent`/`origin_energy_limit` to shift energy fees onto callers, DoS-ing dApp users mid-flight - ([File: actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java])

### Summary
`UpdateSettingContractActuator` and `UpdateEnergyLimitContractActuator` let the deployed contract's `originAddress` (the contract owner — an unprivileged, ordinary account reachable via a single broadcast transaction, not an SR/witness/committee member) change `consume_user_resource_percent` and `origin_energy_limit` for any of its deployed contracts, with the change taking effect on the very next block/transaction and no timelock or cooldown. [2](#0-1) [3](#0-2)  This is the exact bug class described in the external report: an "admin" role (here, contract owner) can unilaterally reconfigure a parameter that unrelated users depend on, at any time, with no protection for those users.

### Finding Description
Any deployed smart contract stores `consume_user_resource_percent` and `origin_energy_limit`, which together determine how much of a transaction's energy cost is paid by the contract's creator versus the transaction's caller. [4](#0-3)  These values are read live at execution time from `ContractStore` in `VMActuator.getTotalEnergyLimitWithFixRatio` and in `TransactionTrace.pay()`, which computes the origin/caller energy split for billing. [5](#0-4) [6](#0-5) 

Both `UpdateSettingContractActuator.execute` and `UpdateEnergyLimitContractActuator.execute` write the new value directly into the `ContractStore` and immediately invalidate any cached copy via `RepositoryImpl.removeLruCache`, so the change is live for the very next transaction, with no timelock, no delay period, and no notice to users of the contract. [7](#0-6) [8](#0-7)  The only access-control check is that the caller must be the `originAddress` recorded in the deployed contract — a normal, unprivileged account, not an SR/witness/committee member. [9](#0-8) 

This mirrors the reported pattern exactly: a single-actor "admin" role (here, whoever deployed/owns the contract) can change a cost/availability parameter that other unrelated users (contract callers) rely on, at any time, with no role separation and no timelock — potentially bricking the experience for those users.

### Impact Explanation
A contract owner can raise `consume_user_resource_percent` to 100 (or lower `origin_energy_limit` to a minimal value) immediately before/at the moment users call the contract. Callers who computed their `fee_limit`/expected cost assuming the previous (lower) `consume_user_resource_percent` will suddenly be forced to cover most or all of the energy cost from their own frozen energy/TRX balance, up to their `fee_limit`, or their calls will simply fail once their own energy/feeLimit is exhausted. [10](#0-9)  This can be used to unexpectedly drain a caller's TRX/energy for a single call (unbacked/unexpected balance debit relative to what the user consented to) or to effectively deny service to callers of that specific contract without any warning or delay, exactly the DoS pattern the external report warns about, reachable purely through ordinary broadcast transactions.

### Likelihood Explanation
Low-to-medium: it requires the contract's deployer/owner to be malicious or compromised, similar to the "malicious/compromised admin" precondition in the original report, but no special network privilege (SR, witness, committee) is needed — any account that deployed a widely used contract can trigger this at will.

### Recommendation
Add a timelock/delay (e.g., only take effect after N blocks) before `consume_user_resource_percent`/`origin_energy_limit` changes apply, and/or cap the maximum single-step change allowed, so users interacting with a contract have a predictable, bounded cost model and cannot be blindsided by an instantaneous parameter flip from the contract owner.

### Proof of Concept
1. Deploy `ContractA` with `consume_user_resource_percent = 0` (owner pays all energy) so users are attracted to call it cheaply.
2. Wait for user `U` to prepare/sign a call to `ContractA` with a `fee_limit` sized for near-zero cost.
3. Before `U`'s transaction is packed into a block, owner submits `UpdateSettingContract` setting `consume_user_resource_percent = 100`, processed by `UpdateSettingContractActuator.execute`, which is applied instantly with no timelock. [7](#0-6) 
4. `U`'s transaction now executes with `TransactionTrace.pay()` computing `percent = 0` (100 - 100 = 0) for the origin, shifting the full energy cost onto `U`, who is charged accordingly or whose call fails from insufficient energy/feeLimit — with no recourse and no warning. [10](#0-9)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L24-58)
```java
@Slf4j(topic = "actuator")
public class UpdateSettingContractActuator extends AbstractActuator {

  public UpdateSettingContractActuator() {
    super(ContractType.UpdateSettingContract, UpdateSettingContract.class);
  }

  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    ContractStore contractStore = chainBaseManager.getContractStore();
    try {
      UpdateSettingContract usContract = any.unpack(UpdateSettingContract.class);
      long newPercent = usContract.getConsumeUserResourcePercent();
      byte[] contractAddress = usContract.getContractAddress().toByteArray();
      ContractCapsule deployedContract = contractStore.get(contractAddress);

      contractStore.put(contractAddress, new ContractCapsule(
          deployedContract.getInstance().toBuilder().setConsumeUserResourcePercent(newPercent)
              .build()));
      RepositoryImpl.removeLruCache(contractAddress);

      ret.setStatus(fee, code.SUCESS);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L107-113)
```java
    byte[] deployedContractOwnerAddress = deployedContract.getInstance().getOriginAddress()
        .toByteArray();

    if (!Arrays.equals(ownerAddress, deployedContractOwnerAddress)) {
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] is not the owner of the contract");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateEnergyLimitContractActuator.java (L30-57)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    ContractStore contractStore = chainBaseManager.getContractStore();
    try {
      UpdateEnergyLimitContract usContract = any.unpack(UpdateEnergyLimitContract.class);
      long newOriginEnergyLimit = usContract.getOriginEnergyLimit();
      byte[] contractAddress = usContract.getContractAddress().toByteArray();
      ContractCapsule deployedContract = contractStore.get(contractAddress);

      contractStore.put(contractAddress, new ContractCapsule(
          deployedContract.getInstance().toBuilder().setOriginEnergyLimit(newOriginEnergyLimit)
              .build()));
      RepositoryImpl.removeLruCache(contractAddress);

      ret.setStatus(fee, code.SUCESS);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    return true;
  }
```

**File:** protocol/src/main/protos/core/contract/smart_contract.proto (L88-98)
```text
message UpdateSettingContract {
  bytes owner_address = 1;
  bytes contract_address = 2;
  int64 consume_user_resource_percent = 3;
}

message UpdateEnergyLimitContract {
  bytes owner_address = 1;
  bytes contract_address = 2;
  int64 origin_energy_limit = 3;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L743-777)
```java
    long creatorEnergyLimit = 0;
    ContractCapsule contractCapsule = rootRepository
        .getContract(contract.getContractAddress().toByteArray());
    long consumeUserResourcePercent = contractCapsule.getConsumeUserResourcePercent(
        VMConfig.disableJavaLangMath());

    long originEnergyLimit = contractCapsule.getOriginEnergyLimit();
    if (originEnergyLimit < 0) {
      throw new ContractValidateException("originEnergyLimit can't be < 0");
    }

    long originEnergyLeft = 0;
    if (consumeUserResourcePercent < VMConstant.ONE_HUNDRED) {
      originEnergyLeft = rootRepository.getAccountLeftEnergyFromFreeze(creator);
      if (VMConfig.allowTvmFreeze() || VMConfig.allowTvmFreezeV2()) {
        receipt.setOriginEnergyLeft(originEnergyLeft);
      }
    }
    if (consumeUserResourcePercent <= 0) {
      creatorEnergyLimit = min(originEnergyLeft, originEnergyLimit,
          VMConfig.disableJavaLangMath());
    } else {
      if (consumeUserResourcePercent < VMConstant.ONE_HUNDRED) {
        // creatorEnergyLimit =
        // min(callerEnergyLimit * (100 - percent) / percent,
        //   creatorLeftFrozenEnergy, originEnergyLimit)

        creatorEnergyLimit = min(
            BigInteger.valueOf(callerEnergyLimit)
                .multiply(BigInteger.valueOf(VMConstant.ONE_HUNDRED - consumeUserResourcePercent))
                .divide(BigInteger.valueOf(consumeUserResourcePercent)).longValueExact(),
            min(originEnergyLeft, originEnergyLimit, VMConfig.disableJavaLangMath()),
            VMConfig.disableJavaLangMath());
      }
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/TransactionTrace.java (L239-253)
```java
      case TRX_CONTRACT_CALL_TYPE:
        TriggerSmartContract callContract = ContractCapsule
            .getTriggerContractFromTransaction(trx.getInstance());
        ContractCapsule contractCapsule =
            contractStore.get(callContract.getContractAddress().toByteArray());

        callerAccount = callContract.getOwnerAddress().toByteArray();
        originAccount = contractCapsule.getOriginAddress();
        boolean disableJavaLangMath = dynamicPropertiesStore.disableJavaLangMath();
        percent = max(Constant.ONE_HUNDRED - contractCapsule.getConsumeUserResourcePercent(
            disableJavaLangMath), 0, disableJavaLangMath);
        percent = min(percent, Constant.ONE_HUNDRED,
            disableJavaLangMath);
        originEnergyLimit = contractCapsule.getOriginEnergyLimit();
        break;
```
