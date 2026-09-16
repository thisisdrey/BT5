### Title
Unbounded, permanent witness-candidate creation causes unbounded per-maintenance-cycle iteration in `MaintenanceManager#doMaintenance` - (File: `consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java`)

### Summary

### Finding Description
Any unprivileged account can call `WitnessCreateContract` via `WitnessCreateActuator` to register itself as a witness candidate, paying only `AccountUpgradeCost` (a fixed TRX fee, not gated by any committee/admin permission). [1](#0-0) 
This writes a new entry into `WitnessStore`, and there is no `WitnessDeleteContract`/actuator or any other mechanism in the codebase to remove a witness once created — a grep for `WitnessDeleteContract|deleteWitness|removeWitness` across the repository returns no results, confirming witness records are permanent.

Every maintenance cycle (executed synchronously as part of block application by every full node, roughly every 6 hours via `MaintenanceManager#applyBlock` → `doMaintenance`), the code performs multiple full iterations over **every** entry in `WitnessStore` via `consensusDelegate.getAllWitnesses()`, not limited to the active/top-127 witness set: [2](#0-1) 

Specifically:
- `consensusDelegate.getAllWitnesses().forEach(witness -> delegationStore.accumulateWitnessVi(...))` (line 98-100)
- `consensusDelegate.getAllWitnesses().forEach(witnessCapsule -> newWitnessAddressList.add(...))` (line 108-109)
- `consensusDelegate.getAllWitnesses().forEach(witness -> delegationStore.setBrokerage(...); delegationStore.setWitnessVote(...))` (line 157-161)

Each of these loops touches the full `WitnessStore` on-disk data set and performs DB writes (`setBrokerage`, `setWitnessVote`, `accumulateWitnessVi`) per entry. Since `WitnessStore` grows without bound (any account can add an entry at low, fixed cost, and no entry is ever removed), the cost of `doMaintenance()` grows linearly and unboundedly with the number of registered witnesses over time. This mirrors the reported bug class exactly: an unprivileged actor can permissionlessly grow an array/store that is unconditionally iterated by a shared, unavoidable system function (`Voter#distribute` ↔ `MaintenanceManager#doMaintenance`), with no corresponding "remove" capability.

### Impact Explanation
`doMaintenance()` is not an optional, user-invoked call like `Voter#distribute` — it is mandatory logic executed by **every** full node as part of consensus block processing at every maintenance boundary. If the cost of this function grows large enough (through cheap, repeated witness registration by any account), it can materially slow down or stall block production/maintenance processing network-wide, since all nodes must perform the same expanding iteration deterministically. This can manifest as delayed block production and processing across the whole network (temporary freezing of user transactions/funds network-wide) — the same "Temporary freezing of funds" impact class described in the original report, but here reachable by any funded but otherwise unprivileged account issuing ordinary `WitnessCreateContract` transactions, with no remediation function available in the current version of the protocol.

### Likelihood Explanation
Likelihood is bounded by the `AccountUpgradeCost` fee for each `WitnessCreateContract` (default order of magnitude ~9999 TRX per registration in java-tron networks), making this an economically costly but not privilege-gated DoS. An attacker with sufficient capital (or over an extended campaign of many low-cost transactions) can grow `WitnessStore` significantly since there is no upper bound enforced by `WitnessCreateActuator#validate` other than balance sufficiency and non-duplication of the same address. [3](#0-2) 

### Recommendation
- Introduce a cap on the total number of witness candidates that can exist in `WitnessStore`, or scale `getAccountUpgradeCost()` progressively with the current candidate count to make large-scale spam economically infeasible.
- Add a `WitnessDeleteContract`/actuator (or automatic pruning of long-inactive, zero-vote witnesses) so that permanently unused witness registrations can be removed, bounding the size of `WitnessStore` over time.
- In `MaintenanceManager#doMaintenance`, avoid iterating over the entire `WitnessStore` for vote-independent bookkeeping (`accumulateWitnessVi`, `setBrokerage`, `setWitnessVote`) — restrict these operations to witnesses that actually received votes or are part of the active/standby set, rather than every ever-registered candidate.

### Proof of Concept
Not independently executable within this analysis (no test harness run); the reasoning is based on static code tracing:
1. Attacker repeatedly submits `WitnessCreateContract` transactions from N distinct funded accounts, each paying `AccountUpgradeCost`, adding N entries to `WitnessStore` with no way to ever remove them (confirmed absence of any delete/removal contract type).
2. At the next maintenance boundary, `MaintenanceManager#doMaintenance` invokes `consensusDelegate.getAllWitnesses()` three separate times, each performing an O(N) traversal with per-entry DB reads/writes (`accumulateWitnessVi`, `setBrokerage`, `setWitnessVote`). [4](#0-3) 
3. As N grows, the wall-clock time to complete `doMaintenance()` (executed by every full node synchronously during block application) grows proportionally, degrading block production and processing capacity for the entire network — analogous to the original report's `Voter#distribute` gas-exhaustion scenario, but manifesting as node/consensus-processing time exhaustion rather than an EVM gas limit.

Note: I was not able to verify the exact current numeric value of `AccountUpgradeCost` or any protocol-level timeout/threshold that would trip a "missed block" condition from within the indexed code; a background Devin session with full repository/test access would be needed to quantify the precise number of witnesses required to cause an observable delay, and to confirm whether `getAllWitnesses()` is backed by a cached in-memory list or a live DB scan (which affects the real-world severity).

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WitnessCreateActuator.java (L53-119)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    WitnessStore witnessStore = chainBaseManager.getWitnessStore();
    if (!this.any.is(WitnessCreateContract.class)) {
      throw new ContractValidateException(
          "contract type error, expected type [WitnessCreateContract],real type[" + any
              .getClass() + "]");
    }
    final WitnessCreateContract contract;
    try {
      contract = this.any.unpack(WitnessCreateContract.class);
    } catch (InvalidProtocolBufferException e) {
      throw new ContractValidateException(e.getMessage());
    }

    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    if (!TransactionUtil.validUrl(contract.getUrl().toByteArray())) {
      throw new ContractValidateException("Invalid url");
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule == null) {
      throw new ContractValidateException("account[" + readableOwnerAddress
          + ActuatorConstant.NOT_EXIST_STR);
    }
    /* todo later
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

  @Override
  public ByteString getOwnerAddress() throws InvalidProtocolBufferException {
    return any.unpack(WitnessCreateContract.class).getOwnerAddress();
  }

  @Override
  public long calcFee() {
    return chainBaseManager.getDynamicPropertiesStore().getAccountUpgradeCost();
  }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L89-163)
```java
  public void doMaintenance() {
    VotesStore votesStore = consensusDelegate.getVotesStore();

    tryRemoveThePowerOfTheGr();

    DynamicPropertiesStore dynamicPropertiesStore = consensusDelegate.getDynamicPropertiesStore();
    DelegationStore delegationStore = consensusDelegate.getDelegationStore();
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }

    Map<ByteString, Long> countWitness = countVote(votesStore);
    if (!countWitness.isEmpty()) {
      List<ByteString> currentWits = consensusDelegate.getActiveWitnesses();

      List<ByteString> newWitnessAddressList = new ArrayList<>();
      consensusDelegate.getAllWitnesses()
          .forEach(witnessCapsule -> newWitnessAddressList.add(witnessCapsule.getAddress()));

      countWitness.forEach((address, voteCount) -> {
        byte[] witnessAddress = address.toByteArray();
        WitnessCapsule witnessCapsule = consensusDelegate.getWitness(witnessAddress);
        if (witnessCapsule == null) {
          logger.warn("Witness capsule is null. address is {}", Hex.toHexString(witnessAddress));
          return;
        }
        AccountCapsule account = consensusDelegate.getAccount(witnessAddress);
        if (account == null) {
          logger.warn("Witness account is null. address is {}", Hex.toHexString(witnessAddress));
          return;
        }
        witnessCapsule.setVoteCount(witnessCapsule.getVoteCount() + voteCount);
        consensusDelegate.saveWitness(witnessCapsule);
        logger.info("address is {} , countVote is {}", witnessCapsule.createReadableString(),
            witnessCapsule.getVoteCount());
      });

      dposService.updateWitness(newWitnessAddressList);

      incentiveManager.reward(newWitnessAddressList);

      List<ByteString> newWits = consensusDelegate.getActiveWitnesses();
      if (!CollectionUtils.isEqualCollection(currentWits, newWits)) {
        currentWits.forEach(address -> {
          WitnessCapsule witnessCapsule = consensusDelegate.getWitness(address.toByteArray());
          witnessCapsule.setIsJobs(false);
          consensusDelegate.saveWitness(witnessCapsule);
        });
        newWits.forEach(address -> {
          WitnessCapsule witnessCapsule = consensusDelegate.getWitness(address.toByteArray());
          witnessCapsule.setIsJobs(true);
          consensusDelegate.saveWitness(witnessCapsule);
        });

        SRMetrics.recordSrSetChange(currentWits, newWits);
      }

      logger.info("Update witness success. \nbefore: {} \nafter: {}",
          getAddressStringList(currentWits),
          getAddressStringList(newWits));
    }

    if (dynamicPropertiesStore.allowChangeDelegation()) {
      long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
      dynamicPropertiesStore.saveCurrentCycleNumber(nextCycle);
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.setBrokerage(nextCycle, witness.createDbKey(),
            delegationStore.getBrokerage(witness.createDbKey()));
        delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
  }
```
