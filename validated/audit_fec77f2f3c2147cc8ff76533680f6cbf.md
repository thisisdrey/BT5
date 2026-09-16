Based on my analysis, I found a valid analog vulnerability.

### Title
`UpdateBrokerageActuator` allows a witness to change their brokerage ratio mid-cycle, retroactively affecting the current cycle's already-accrued voter rewards - ([File: actuator/src/main/java/org/tron/core/actuator/UpdateBrokerageActuator.java])

### Summary
The Sherlock report describes a round operator changing round-application status (`setApplicationStatuses()`) at any point in time — including during the "voting" phase — without restricting the action to the "selection" period, letting operators retroactively manipulate outcomes that voters/participants already relied on. The analogous pattern in java-tron is `UpdateBrokerageActuator`, which lets a Super Representative (SR) change their brokerage (fee-split) ratio at any time, and that new ratio is applied immediately to reward payouts within the *current, already in-progress* reward cycle rather than only from the next cycle onward — even though voters delegated/voted under the previous ratio.

### Finding Description
`UpdateBrokerageActuator.execute()` calls `delegationStore.setBrokerage(ownerAddress, brokerage)` [1](#0-0) , which internally calls `setBrokerage(-1, address, brokerage)` — writing the new brokerage value to a special "REMARK" cycle slot (`-1`) rather than to the current or a future cycle [2](#0-1) .

Reward distribution reads the brokerage for the *current* cycle via `getBrokerage(cycle, witnessAddress)` inside `payReward()`, called from `payBlockReward`/`payTransactionFeeReward`/`payStandbyWitness` during ordinary block processing [3](#0-2) . Since `getBrokerage(cycle, address)` falls back to the "REMARK" (`-1`) value whenever no cycle-specific brokerage record exists [4](#0-3) , an SR-submitted `UpdateBrokerageContract` transaction takes effect on the very next block/reward computation within the *same, currently in-progress* voting cycle — there is no validation in `UpdateBrokerageActuator.validate()` gating the change to "only effective starting next cycle" [5](#0-4) .

This mirrors the reported bug class exactly: a state-changing, permissioned actuator (comparable to the round operator's `setApplicationStatuses`) can be invoked mid-cycle/mid-process (analogous to mid-"voting period") and immediately alters the terms under which already-committed participant actions (votes/delegations already cast for the cycle) are settled, without any timing/period gate to defer the effect until the next well-defined period boundary.

### Impact Explanation
Voters delegate/vote for an SR based on a known brokerage ratio (which determines the split of block/vote rewards between the SR and its voters). Because the new brokerage takes effect immediately mid-cycle instead of at the next cycle boundary, an SR can raise their brokerage ratio (up to 100, per the `brokerage <= 100` check [6](#0-5) ) after voters have already cast votes for the ongoing cycle, retroactively diverting a larger share of the reward pool to themselves and reducing voter payouts for a cycle voters believed was already locked in under the prior ratio. This is a concrete unauthorized diversion of funds/rewards that were expected to accrue to delegators.

### Likelihood Explanation
Any SR/witness account can submit this transaction at will since `UpdateBrokerageContract` requires only that the sender own a witness account (checked via `witnessStore.get(ownerAddress)` and `accountStore.get(ownerAddress)`) [7](#0-6) , with no cooldown, no restriction to specific block/cycle windows, and no re-validation against a "not yet applied this cycle" state. This makes the change trivially and repeatedly reachable by any witness at any point mid-cycle.

### Recommendation
Modify `UpdateBrokerageActuator`/`DelegationStore` so that brokerage updates are recorded for `currentCycle + 1` (or later) instead of the `-1` "REMARK" slot, ensuring the new ratio only applies starting from the next full reward cycle and never retroactively affects rewards for votes already cast in the current cycle.

### Proof of Concept
1. SR `W` is voted for by voter `V` under the default/previous brokerage of 20% (SR keeps 20%, voters get 80%) recorded via `getBrokerage(cycle, W)` returning the `-1`-slot value.
2. Mid-cycle (after `V`'s votes are already registered for the current cycle via `setAccountVote`), `W` broadcasts an `UpdateBrokerageContract` transaction setting `brokerage = 100`.
3. `UpdateBrokerageActuator.execute()` immediately writes this to the `-1` slot via `setBrokerage(address, 100)` [1](#0-0) .
4. On the next block reward computation in the same cycle, `MortgageService.payReward()` calls `delegationStore.getBrokerage(cycle, W)`, which returns `100` (falling back to the `-1` slot) [3](#0-2) , so 100% of the remaining block/vote reward for `W` in the current cycle is diverted to `W`'s allowance instead of the voter pool — despite `V`'s votes having been cast under the 20% terms.


Repository: Lauraivanka/java-tron--014

Problem: `actuator/src/main/java/org/tron/core/actuator/UpdateBrokerageActuator.java` executes `delegationStore.setBrokerage(ownerAddress, brokerage)`, which stores the new brokerage in a special "REMARK" (cycle = -1) slot in `chainbase/src/main/java/org/tron/core/store/DelegationStore.java` (`setBrokerage(byte[] address, int brokerage)` at line ~112, calling `setBrokerage(-1, address, brokerage)`). Reward computation in `chainbase/src/main/java/org/tron/core/service/MortgageService.java`'s `payReward()` method calls `delegationStore.getBrokerage(cycle, witnessAddress)`, which falls back to this `-1` slot whenever no value is set for the exact current cycle (see `getBrokerage(long cycle, byte[] address)` in `DelegationStore.java`). As a result, an SR's brokerage-ratio change submitted via a signed `UpdateBrokerageContract` transaction takes effect immediately within the currently in-progress reward cycle, retroactively changing the split of rewards for votes/delegations that voters already committed for that cycle under the previous ratio. This lets any witness account unilaterally and immediately redirect a larger (up to 100%) share of block/vote rewards to themselves mid-cycle, at the expense of their voters.

Task:
1. Investigate `chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java` for `getCurrentCycleNumber()` and confirm how/when cycle numbers advance during block maintenance (`Manager`/`ConsensusService` maintenance cycle processing).
2. Modify `DelegationStore.setBrokerage(byte[] address, int brokerage)` (and/or `UpdateBrokerageActuator.execute()`) so that a brokerage update is recorded for `currentCycle + 1` (the next cycle) instead of the `-1` REMARK slot, so it only takes effect starting from the next full reward cycle boundary, never retroactively within the cycle where the update transaction was broadcast.
3. Update `DelegationStore.getBrokerage(long cycle, byte[] address)` lookup logic accordingly so that for any given cycle, it resolves to the most recently set brokerage value that was scheduled to take effect at or before that cycle (i.e., proper cycle-scoped lookup, not simply falling back to a global `-1` slot for every cycle).
4. Update or add tests in `framework/src/test/java/org/tron/core/actuator/UpdateBrokerageActuatorTest.java` and any `MortgageService`/reward computation tests to verify: (a) an `UpdateBrokerageContract` submitted mid-cycle does NOT affect reward payout calculations for the currently in-progress cycle, and (b) it correctly takes effect starting from the next cycle.
5. Check all other call sites of `DelegationStore.setBrokerage`/`getBrokerage` (e.g., `MortgageService.payReward`, any HTTP/gRPC wallet query paths exposing brokerage info) to ensure consistent behavior after the fix.
6. Run the full existing test suite for `UpdateBrokerageActuatorTest`, `MortgageService`-related tests, and any witness/reward integration tests to confirm no regression in reward distribution accounting.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateBrokerageActuator.java (L49-53)
```java
    byte[] ownerAddress = updateBrokerageContract.getOwnerAddress().toByteArray();
    int brokerage = updateBrokerageContract.getBrokerage();

    delegationStore.setBrokerage(ownerAddress, brokerage);
    ret.setStatus(fee, code.SUCESS);
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

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L99-118)
```java
  public void setBrokerage(long cycle, byte[] address, int brokerage) {
    put(buildBrokerageKey(cycle, address), new BytesCapsule(ByteArray.fromInt(brokerage)));
  }

  public int getBrokerage(long cycle, byte[] address) {
    BytesCapsule bytesCapsule = get(buildBrokerageKey(cycle, address));
    if (bytesCapsule == null) {
      return DEFAULT_BROKERAGE;
    } else {
      return ByteArray.toInt(bytesCapsule.getData());
    }
  }

  public void setBrokerage(byte[] address, int brokerage) {
    setBrokerage(-1, address, brokerage);
  }

  public int getBrokerage(byte[] address) {
    return getBrokerage(-1, address);
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
