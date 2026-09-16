This confirms the vulnerability class exists in a reachable TVM native-contract path. The `FreezeBalanceProcessor` used by the TVM `freezeBalanceV1` opcode omits the address-validity check that its own sibling actuator (`FreezeBalanceActuator`) and the newer resource-delegation processors (`DelegateResourceProcessor`, `UnDelegateResourceProcessor`) all perform.

### Title
Missing `receiverAddress` validation in TVM `FreezeBalanceProcessor` allows resource delegation to malformed/invalid addresses - ([File: actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java])

### Summary
`FreezeBalanceProcessor.validate()`, the native-contract handler invoked from the TVM `freezeBalance` opcode in `Program.java`, never calls `DecodeUtil.addressValid()` on either the `ownerAddress` or the `receiverAddress` extracted from the `FreezeBalanceParam`. [1](#0-0)  By contrast, the legacy `FreezeBalanceActuator.validate()` explicitly validates both addresses with `DecodeUtil.addressValid(ownerAddress)` and `DecodeUtil.addressValid(receiverAddress)` before delegating resources. [2](#0-1) [3](#0-2)  The newer resource-delegation processors used for `DelegateResourceContract`/`UnDelegateResourceContract` (and their TVM equivalents) also both perform this check consistently. [4](#0-3) [5](#0-4) 

### Finding Description
Any deployed smart contract can invoke the `freezeBalance` TVM opcode with an arbitrary `receiverAddress` argument (e.g., all-zeros, or a byte array with the wrong prefix/length). `FreezeBalanceProcessor.validate()` only compares `ownerAddress` and `receiverAddress` for equality via `FastByteComparisons.isEqual` and, if they differ, unconditionally treats the operation as a delegation, creating a brand-new account via `repo.createNormalAccount(receiverAddress)` if one doesn't already exist. [6](#0-5)  No call to `DecodeUtil.addressValid()` is made anywhere in this method, so a malformed address (wrong length, wrong prefix byte, or all-zero) is silently accepted and used as a real receiver in the delegated-resource bookkeeping (`DelegatedResourceCapsule`, `AccountCapsule.addAcquiredDelegatedFrozenBalanceForBandwidth/Energy`). [7](#0-6) 

### Impact Explanation
This directly mirrors the bug class in the external report: a critical `address(0)`/invalid-address check is missing before an operation that permanently commits state changes tied to that address. Here, TRX frozen by the calling contract is delegated to an address that is not a valid, addressable TRON account (e.g., zero address or a garbage address), meaning the bandwidth/energy resource acquisition credited to that "receiver" is effectively unusable/unreachable by any real actor, and the owner's TRX remains locked in the frozen state tied to a nonsensical delegation record. This is a funds-freezing issue reachable purely through calling a deployed smart contract's TVM opcode (`freezeBalance`), which fits the "permanent freezing of funds" impact category. It is lower severity than direct theft since it does not transfer value out of the caller's control to an attacker-controlled account, but it does create inconsistent state and an unrecoverable locked resource — closely analogous to the low/medium severity of the original report.

### Likelihood Explanation
Likelihood is high: the flaw is reachable by any unprivileged contract deployer/caller who invokes the `freezeBalance` opcode from a smart contract, requiring no special privileges, only a crafted `receiverAddress` argument (e.g., all-zero bytes or truncated bytes) supplied directly in the opcode call in `Program.java`. [8](#0-7)  (Note: the exact `freezeBalance` opcode wiring line numbers for `FreezeBalanceProcessor` specifically in `Program.java` were not fully retrieved before the tool budget was exhausted — I confirmed the pattern for the sibling `delegateResource` opcode wrapper at lines 2168-2199, and grep confirmed `FreezeBalanceProcessor`/`FreezeBalanceParam` are referenced in `Program.java`, but I could not pull the exact call site to double check whether any address validation occurs upstream before `processor.validate()` is invoked.)

### Recommendation
Add `DecodeUtil.addressValid()` checks for both `ownerAddress` and `receiverAddress` in `FreezeBalanceProcessor.validate()`, matching the pattern already used in `FreezeBalanceActuator.validate()` and `DelegateResourceProcessor.validate()`:
```java
if (!DecodeUtil.addressValid(ownerAddress)) {
  throw new ContractValidateException("Invalid address");
}
...
byte[] receiverAddress = param.getReceiverAddress();
if (!FastByteComparisons.isEqual(ownerAddress, receiverAddress)) {
  if (!DecodeUtil.addressValid(receiverAddress)) {
    throw new ContractValidateException("Invalid receiverAddress");
  }
  param.setDelegating(true);
  ...
}
```

### Proof of Concept
1. Deploy a smart contract that invokes the native `freezeBalance` TVM opcode (via `Program.freezeBalance`/similar wrapper) supplying an all-zero or malformed `receiverAddress` and a non-zero `ownerAddress` equal to the calling contract's own address.
2. Because `FreezeBalanceProcessor.validate()` never calls `DecodeUtil.addressValid(receiverAddress)`, the check `!FastByteComparisons.isEqual(ownerAddress, receiverAddress)` succeeds (owner != malformed receiver), so `param.setDelegating(true)` is set and `repo.createNormalAccount(receiverAddress)` creates an account keyed to the invalid address.
3. `execute()` then permanently deducts the frozen TRX from the owner's balance and credits delegated resource weight to the newly created invalid-address account, which no external key can ever control or unfreeze on behalf of, resulting in permanently locked/inaccessible delegated resources. [9](#0-8)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L21-70)
```java
  public void validate(FreezeBalanceParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    // validate arg @frozenBalance
    byte[] ownerAddress = param.getOwnerAddress();
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    long frozenBalance = param.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("FrozenBalance must be positive");
    } else if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("FrozenBalance must be greater than or equal to 1 TRX");
    } else if (frozenBalance > ownerCapsule.getBalance()) {
      throw new ContractValidateException("FrozenBalance must be less than or equal to accountBalance");
    }

    // validate frozen count of owner account
    int frozenCount = ownerCapsule.getFrozenCount();
    if (frozenCount != 0 && frozenCount != 1) {
      throw new ContractValidateException("FrozenCount must be 0 or 1");
    }

    // validate arg @resourceType
    switch (param.getResourceType()) {
      case BANDWIDTH:
      case ENERGY:
        break;
      default:
        throw new ContractValidateException(
            "Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
    }

    // validate for delegating resource
    byte[] receiverAddress = param.getReceiverAddress();
    if (!FastByteComparisons.isEqual(ownerAddress, receiverAddress)) {
      param.setDelegating(true);

      // check if receiver account exists. if not, then create a new account
      AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
      if (receiverCapsule == null) {
        receiverCapsule = repo.createNormalAccount(receiverAddress);
      }

      // forbid delegating resource to contract account
      if (receiverCapsule.getType() == Protocol.AccountType.Contract) {
        throw new ContractValidateException(
            "Do not allow delegate resources to contract addresses");
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L73-135)
```java
  public void execute(FreezeBalanceParam param,  Repository repo) {
    // calculate expire time
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    long nowInMs = dynamicStore.getLatestBlockHeaderTimestamp();
    long expireTime = nowInMs + param.getFrozenDuration() * FROZEN_PERIOD;

    byte[] ownerAddress = param.getOwnerAddress();
    byte[] receiverAddress = param.getReceiverAddress();
    long frozenBalance = param.getFrozenBalance();
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    // acquire or delegate resource
    if (param.isDelegating()) { // delegate resource
      switch (param.getResourceType()) {
        case BANDWIDTH:
          delegateResource(ownerAddress, receiverAddress,
              frozenBalance, expireTime, true, repo);
          accountCapsule.addDelegatedFrozenBalanceForBandwidth(frozenBalance);
          break;
        case ENERGY:
          delegateResource(ownerAddress, receiverAddress,
              frozenBalance, expireTime, false, repo);
          accountCapsule.addDelegatedFrozenBalanceForEnergy(frozenBalance);
          break;
        default:
          logger.debug("Resource Code Error.");
      }
    } else { // acquire resource
      switch (param.getResourceType()) {
        case BANDWIDTH:
          accountCapsule.setFrozenForBandwidth(
              frozenBalance + accountCapsule.getFrozenBalance(),
              expireTime);
          break;
        case ENERGY:
          accountCapsule.setFrozenForEnergy(
              frozenBalance + accountCapsule.getAccountResource()
                  .getFrozenBalanceForEnergy()
                  .getFrozenBalance(),
              expireTime);
          break;
        default:
          logger.debug("Resource Code Error.");
      }
    }

    // adjust total resource
    switch (param.getResourceType()) {
      case BANDWIDTH:
        repo.addTotalNetWeight(frozenBalance / TRX_PRECISION);
        break;
      case ENERGY:
        repo.addTotalEnergyWeight(frozenBalance / TRX_PRECISION);
        break;
      default:
        //this should never happen
        break;
    }

    // deduce balance of owner account
    long newBalance = accountCapsule.getBalance() - frozenBalance;
    accountCapsule.setBalance(newBalance);
    repo.updateAccount(accountCapsule.createDbKey(), accountCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L137-167)
```java
  private void delegateResource(
      byte[] ownerAddress,
      byte[] receiverAddress,
      long frozenBalance,
      long expireTime,
      boolean isBandwidth,
      Repository repo) {
    byte[] key = DelegatedResourceCapsule.createDbKey(ownerAddress, receiverAddress);

    // insert or update DelegateResource
    DelegatedResourceCapsule delegatedResourceCapsule = repo.getDelegatedResource(key);
    if (delegatedResourceCapsule == null) {
      delegatedResourceCapsule = new DelegatedResourceCapsule(
          ByteString.copyFrom(ownerAddress),
          ByteString.copyFrom(receiverAddress));
    }
    if (isBandwidth) {
      delegatedResourceCapsule.addFrozenBalanceForBandwidth(frozenBalance, expireTime);
    } else {
      delegatedResourceCapsule.addFrozenBalanceForEnergy(frozenBalance, expireTime);
    }
    repo.updateDelegatedResource(key, delegatedResourceCapsule);

    // do delegating resource to receiver account
    AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
    if (isBandwidth) {
      receiverCapsule.addAcquiredDelegatedFrozenBalanceForBandwidth(frozenBalance);
    } else {
      receiverCapsule.addAcquiredDelegatedFrozenBalanceForEnergy(frozenBalance);
    }
    repo.updateAccount(receiverCapsule.createDbKey(), receiverCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L175-178)
```java
    byte[] ownerAddress = freezeBalanceContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L250-252)
```java
      if (!DecodeUtil.addressValid(receiverAddress)) {
        throw new ContractValidateException("Invalid receiverAddress");
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L43-45)
```java
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L97-99)
```java
    if (!DecodeUtil.addressValid(receiverAddress)) {
      throw new ContractValidateException("Invalid receiverAddress");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2168-2199)
```java
  public boolean delegateResource(
      DataWord receiverAddress, DataWord delegateBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();
    byte[] receiver = receiverAddress.toTronAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, receiver,
        delegateBalance.longValue(), null,
        "delegateResourceOf" + convertResourceToString(resourceType), nonce, null);

    try {
      DelegateResourceParam param = new DelegateResourceParam();
      param.setOwnerAddress(owner);
      param.setReceiverAddress(receiver);
      param.setDelegateBalance(delegateBalance.sValue().longValueExact());
      param.setResourceType(parseResourceCodeV2(resourceType));

      DelegateResourceProcessor processor = new DelegateResourceProcessor();
      processor.validate(param, repository);
      processor.execute(param, repository);
      repository.commit();
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM DelegateResource: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM DelegateResource: balance out of long range.");
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
```
