Found: `payReward` in `MortgageService` reads the SR's brokerage rate for the *current* cycle at the moment each block/transaction-fee reward is paid, via `delegationStore.getBrokerage(cycle, witnessAddress)`, and `UpdateBrokerageActuator.execute` writes with `setBrokerage(-1, ownerAddress, brokerage)`, i.e. `cycle = -1` ("REMARK"/default slot), not the current cycle.

### Title
Witness (SR) brokerage ratio can be changed mid-cycle and immediately affects reward splitting for the in-progress cycle - ([File: chainbase/src/main/java/org/tron/core/store/DelegationStore.java])

### Summary
`UpdateBrokerageActuator` lets any registered witness broadcast an `UpdateBrokerageContract` transaction at any time, with no restriction tied to the current maintenance cycle. [1](#0-0) 

### Finding Description
`getBrokerage(long cycle, byte[] address)` falls back to `DEFAULT_BROKERAGE` only when no value exists for that exact `cycle` key; otherwise it returns whatever was last stored. [2](#0-1)  `UpdateBrokerageActuator.execute` always calls `delegationStore.setBrokerage(ownerAddress, brokerage)`, which is a fixed pass-through to `setBrokerage(-1, address, brokerage)`. [3](#0-2) [4](#0-3) 

`MortgageService.payReward`, which is invoked per-block and per-transaction to distribute block rewards and fee rewards to the block-producing witness, reads the brokerage rate with `delegationStore.getBrokerage(cycle, witnessAddress)` using the *current* cycle number (`dynamicPropertiesStore.getCurrentCycleNumber()`), not a snapshot fixed at the start of the cycle. [5](#0-4)  Because `getBrokerage(cycle, address)` falls through to the same `-1` bucket whenever no cycle-specific entry exists (which is the normal case, since the actuator only ever writes to bucket `-1`), any block reward paid within the currently running cycle immediately reflects a brand-new brokerage value set by the witness in the middle of that same cycle, splitting the same cycle's rewards between two different (self-chosen) allocation ratios depending on exactly when in the cycle each block/fee reward happens to be paid.

This is analogous to the reported "vault can be changed during an auction" issue: a value that should only take effect starting at the boundary of a discrete accounting period (auction / cycle) can instead be mutated by the party who benefits from it while that period is still open, retroactively/partially altering how funds already accruing in that period get split.

### Impact Explanation
A witness can front-run its own upcoming blocks: keep brokerage low (or default) while votes/blocks accumulate, then raise brokerage right before a large block reward or fee reward is paid within the same cycle, capturing a larger share of the SR draw than voters expected for that cycle, and vice versa to briefly lower it to appear more generous before raising it again. Since block reward/fee reward distribution happens continuously through the cycle via `payReward`, and there is no cycle-boundary lock or "effective next cycle" semantics enforced at read time, the split between witness brokerage and voter reward pool within one cycle can be manipulated at the voters' expense (or delegators' expense), which is a fund-misallocation issue affecting third parties (voters expecting a stable, cycle-consistent brokerage rate).

### Likelihood Explanation
Any account that is already a registered witness can broadcast `UpdateBrokerageContract` at will (subject only to `brokerage` being between 0 and 100 and account/witness existing) — no epoch/cycle gating exists in `UpdateBrokerageActuator.validate()`. [6](#0-5)  Witnesses control exactly when their own blocks are produced/paid, so timing the update relative to reward payment is straightforward for the actor who benefits.

### Recommendation
Store and read brokerage per explicit cycle number (write to `cycle = currentCycle + 1` instead of the constant `-1`/REMARK bucket) so a brokerage change only takes effect starting from the next full cycle, never affecting rewards already accruing in the cycle during which the change transaction was broadcast — mirroring the report's suggestion to block/defer vault-like parameter changes until the current accounting period is closed.

### Proof of Concept
1. Witness `W` is registered and currently has `DEFAULT_BROKERAGE` (20%) in effect for `cycle` `C` (no entry at bucket `-1` yet).
2. During cycle `C`, before most blocks are produced, `W` calls `UpdateBrokerageContract` broadcast via `UpdateBrokerageActuator`, setting `brokerage = 0`; this writes to `delegationStore` bucket `-1`. [7](#0-6) 
3. Just before `W` produces the last (largest) blocks of cycle `C`, `W` broadcasts another `UpdateBrokerageContract` raising `brokerage` to `100`.
4. `payReward`, called for those last blocks, reads `getBrokerage(C, W)` which resolves to the same `-1` bucket now holding `100`, so nearly all reward for those blocks is diverted to `W`'s own allowance via `adjustAllowance`, while earlier blocks in the same cycle `C` were paid out under the 0% setting — inconsistent, self-serving in-cycle manipulation of the same accounting period's fund split. [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateBrokerageActuator.java (L29-56)
```java
  @Override
  public boolean execute(Object result) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    final UpdateBrokerageContract updateBrokerageContract;
    final long fee = calcFee();

    DelegationStore delegationStore = chainBaseManager.getDelegationStore();

    try {
      updateBrokerageContract = any.unpack(UpdateBrokerageContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    byte[] ownerAddress = updateBrokerageContract.getOwnerAddress().toByteArray();
    int brokerage = updateBrokerageContract.getBrokerage();

    delegationStore.setBrokerage(ownerAddress, brokerage);
    ret.setStatus(fee, code.SUCESS);

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateBrokerageActuator.java (L58-108)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    WitnessStore witnessStore = chainBaseManager.getWitnessStore();
    if (!dynamicStore.allowChangeDelegation()) {
      throw new ContractValidateException(
          "contract type error, unexpected type [UpdateBrokerageContract]");
    }

    if (!this.any.is(UpdateBrokerageContract.class)) {
      throw new ContractValidateException(
          "contract type error, expected type [UpdateBrokerageContract], real type[" + any
              .getClass() + "]");
    }
    final UpdateBrokerageContract updateBrokerageContract;
    try {
      updateBrokerageContract = any.unpack(UpdateBrokerageContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
    byte[] ownerAddress = updateBrokerageContract.getOwnerAddress().toByteArray();
    int brokerage = updateBrokerageContract.getBrokerage();

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }

    if (brokerage < 0 || brokerage > ActuatorConstant.ONE_HUNDRED) {
      throw new ContractValidateException("Invalid brokerage");
    }

    WitnessCapsule witnessCapsule = witnessStore.get(ownerAddress);
    if (witnessCapsule == null) {
      throw new ContractValidateException("Not existed witness:" + Hex.toHexString(ownerAddress));
    }

    AccountCapsule account = accountStore.get(ownerAddress);
    if (account == null) {
      throw new ContractValidateException("Account does not exist");
    }

    return true;
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L103-110)
```java
  public int getBrokerage(long cycle, byte[] address) {
    BytesCapsule bytesCapsule = get(buildBrokerageKey(cycle, address));
    if (bytesCapsule == null) {
      return DEFAULT_BROKERAGE;
    } else {
      return ByteArray.toInt(bytesCapsule.getData());
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L112-114)
```java
  public void setBrokerage(byte[] address, int brokerage) {
    setBrokerage(-1, address, brokerage);
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L79-87)
```java
  private void payReward(byte[] witnessAddress, long value) {
    long cycle = dynamicPropertiesStore.getCurrentCycleNumber();
    int brokerage = delegationStore.getBrokerage(cycle, witnessAddress);
    double brokerageRate = (double) brokerage / 100;
    long brokerageAmount = (long) (brokerageRate * value);
    value -= brokerageAmount;
    delegationStore.addReward(cycle, witnessAddress, value);
    adjustAllowance(witnessAddress, brokerageAmount);
  }
```
