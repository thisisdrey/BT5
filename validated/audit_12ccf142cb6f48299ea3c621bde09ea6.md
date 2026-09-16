### Title
Unbounded witness registration enables denial-of-service in `MaintenanceManager.doMaintenance()` block-application loop - (File: `consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java`)

### Summary
`WitnessCreateActuator` places no cap on the total number of witnesses that can be created — any account can call it repeatedly as long as it pays `AccountUpgradeCost` per call. `MaintenanceManager.doMaintenance()`, which runs synchronously during block application every maintenance cycle, iterates over *all* witnesses several times, performing per-witness state writes each time. Because the witness set size is fully attacker-controlled and unbounded, this loop can be inflated arbitrarily, turning a mandatory, protocol-level computation into a growing bottleneck for every SR producing a block — directly analogous to the reported GaugeController "unbounded loop over all periods/gauges" issue, where an attacker adding many entries can trap subsequent operations.

### Finding Description
`WitnessCreateActuator.validate()` only checks address validity, URL validity, whether the address is already a witness, and balance sufficiency for `AccountUpgradeCost`. There is no limit on the total number of distinct witnesses that can exist: [1](#0-0) 

Each `WitnessCreateContract` transaction is an ordinary, unprivileged, signed transaction that any funded account can broadcast.

`MaintenanceManager.doMaintenance()` is invoked from block application (via `DposService.applyBlock` → `maintenanceManager.applyBlock`) on every maintenance cycle, and iterates over the full witness set multiple times with state-mutating callbacks: [2](#0-1) [3](#0-2) [4](#0-3) 

Each of these `forEach` blocks performs a DB read/write (`delegationStore.accumulateWitnessVi`, `witnessStore`/`delegationStore` puts, `setBrokerage`, `setWitnessVote`) for every single witness in the store, and `getAllWitnesses()` iterates the entire `WitnessStore`: [5](#0-4) 

Unlike the `VoteRewardUtil`/`MortgageService` reward path — which was specifically redesigned with a Vi-accumulator so reward computation is O(votes) instead of O(cycles), avoiding the exact "loop over history" pitfall described in the report — the witness maintenance loop has no such bound and scales linearly (times a constant factor of 3 loops) with the number of registered witnesses, which is attacker-controlled.

### Impact Explanation
`doMaintenance()` runs unconditionally as part of block application on every SR node for every maintenance cycle (not inside a per-transaction energy-metered context, so it cannot simply "run out of gas" and abort — it must complete before block application finishes). If the witness set is inflated to a very large size, this maintenance step becomes correspondingly slower on every full node and every SR, which can cause block-production delays, missed block slots, or synchronized slowdowns across the network — a network/chain availability impact matching the report's "contract is trapped" / DoS class, analogous to GaugeController being made unusable by many gauges.

### Likelihood Explanation
The witness creation cost (`AccountUpgradeCost`) is a fixed, moderate TRX fee; there is no rate limit, cooldown, or maximum-witness-count check, so a well-funded attacker (or a botnet of funded accounts) can create very large numbers of witness entries over time entirely through normal, unprivileged `WitnessCreateContract` transactions. The cost scales linearly with the number of witnesses created, but so does the number of iterations added to every future maintenance cycle indefinitely (witnesses are never removed), making this a cumulative, permanent burden rather than a one-time cost.

### Recommendation
- Add an explicit maximum limit on the number of distinct witnesses (separate from `MAX_ACTIVE_WITNESS_NUM`, which only bounds the *active* set) in `WitnessCreateActuator.validate()`.
- Consider making `AccountUpgradeCost` scale with the current total witness count to disincentivize mass registration.
- Profile `doMaintenance()` under a large synthetic witness set (thousands+) with `--gas`/timing-based tests to quantify the block-application time impact and set safe bounds.
- Consider paginating/streaming the maintenance loops so a single maintenance cycle cannot be blocked by an unbounded witness set.

### Proof of Concept
1. Fund N accounts, each with balance ≥ `dynamicStore.getAccountUpgradeCost()`.
2. Broadcast N `WitnessCreateContract` transactions (one per funded account) — each succeeds because `WitnessCreateActuator.validate()` never checks total witness count, only funds and address uniqueness (`actuator/src/main/java/org/tron/core/actuator/WitnessCreateActuator.java:98-106`).
3. Wait for the next maintenance cycle; `MaintenanceManager.doMaintenance()` iterates `consensusDelegate.getAllWitnesses()` (size N, permanently) up to three separate times with state writes each cycle (`consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java:96-162`).
4. As N grows (e.g., tens of thousands), measure the wall-clock time of `doMaintenance()` during block application and observe it scaling linearly with N, with no upper bound, directly slowing down block production across the network.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WitnessCreateActuator.java (L94-109)
```java
    if (ArrayUtils.isEmpty(accountCapsule.getAccountName().toByteArray())) {
      throw new ContractValidateException("accountStore name not set");
    } */

    if (witnessStore.has(ownerAddress)) {
      throw new ContractValidateException(
          WITNESS_EXCEPTION_STR + readableOwnerAddress + "] has existed");
    }

    if (accountCapsule.getBalance() < dynamicStore
        .getAccountUpgradeCost()) {
      throw new ContractValidateException("balance < AccountUpgradeCost");
    }

    return true;
  }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L96-101)
```java
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L107-110)
```java
      List<ByteString> newWitnessAddressList = new ArrayList<>();
      consensusDelegate.getAllWitnesses()
          .forEach(witnessCapsule -> newWitnessAddressList.add(witnessCapsule.getAddress()));

```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L154-162)
```java
    if (dynamicPropertiesStore.allowChangeDelegation()) {
      long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
      dynamicPropertiesStore.saveCurrentCycleNumber(nextCycle);
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.setBrokerage(nextCycle, witness.createDbKey(),
            delegationStore.getBrokerage(witness.createDbKey()));
        delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** chainbase/src/main/java/org/tron/core/store/WitnessStore.java (L29-36)
```java
  /**
   * get all witnesses.
   */
  public List<WitnessCapsule> getAllWitnesses() {
    return Streams.stream(iterator())
        .map(Entry::getValue)
        .collect(Collectors.toList());
  }
```
