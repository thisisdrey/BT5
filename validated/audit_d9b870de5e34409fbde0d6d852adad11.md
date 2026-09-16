### Title
Flash-vote manipulation of witness elections via minimum-lock-free FreezeBalanceV2/UnfreezeBalanceV2 (Vote-For-TRX-Power without stake commitment) - (File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java)

### Summary
java-tron's DPoS witness election weights votes by TronPower, which is obtained by freezing TRX via `FreezeBalanceV2Contract`/`UnfreezeBalanceV2Contract`. Unlike the legacy `FreezeBalanceContract`, which enforces a `FROZEN_PERIOD` minimum lock (3 days) via `frozenDuration` in `FreezeBalanceActuator`, the newer `FreezeBalanceV2Actuator.validate()` imposes no minimum lock duration at all — only that `frozenBalance >= 1 TRX`. Combined with the fact that witness vote totals are only tallied once per maintenance cycle (`MaintenanceManager.doMaintenance()` / `countVote()`), an attacker can freeze a large TRX balance for `TRON_POWER` immediately before a maintenance cycle boundary, cast `VoteWitnessContract` votes, have them counted into `WitnessCapsule.voteCount` at `doMaintenance()`, and then immediately submit `UnfreezeBalanceV2Contract` to begin withdrawing the stake — exactly mirroring the "buy just before voting ends, sell right after" attack described in the report.

### Finding Description
Witness election weight is tron power from frozen TRX: [1](#0-0) 

`FreezeBalanceV2Actuator.validate()` only checks the frozen amount, not any lock duration: [2](#0-1) 

This is in stark contrast to the legacy `FreezeBalanceActuator`, which imports and (elsewhere in the file) enforces `FROZEN_PERIOD`/`frozenDuration` as a minimum commitment window: [3](#0-2) 

Votes cast via `VoteWitnessActuator` are only reflected in `WitnessCapsule.voteCount` at the next maintenance tick, where `MaintenanceManager.doMaintenance()` calls `countVote(votesStore)` to sum `oldVotes`/`newVotes` from `VotesCapsule` entries and add the delta to each witness's vote count, then selects/updates the active witness set: [4](#0-3) 

`UnfreezeBalanceV2Actuator.execute()` allows unfreezing as soon as a corresponding `FreezeV2` entry with a positive amount exists, with no minimum holding time — only an `unfreezeDelayDays` timer that delays return of the TRX balance itself, not re-validation of whether the vote was ever "earned" by longer participation: [5](#0-4) 

Because a vote, once counted in `doMaintenance()`, permanently increments `witnessCapsule.setVoteCount(...)` for that cycle (there is no re-verification that the voter's stake was held throughout the epoch, only that it existed at the moment votes were tallied), an attacker can:
1. Freeze a large TRX amount for `TRON_POWER` in a transaction just before the maintenance boundary.
2. Immediately vote for a target witness with `VoteWitnessContract`.
3. Let `doMaintenance()` tally the vote into the witness's `voteCount`, potentially flipping which witnesses are in the active (top-27) set via `dposService.updateWitness(newWitnessAddressList)`.
4. Immediately submit `UnfreezeBalanceV2Contract` to start the withdrawal, recovering nearly all economic exposure while only enduring the `unfreezeDelayDays` liquidity delay (not a vote-eligibility delay).

The active witness set determines block production rights, block rewards, and (via `IncentiveManager.reward`) reward distribution, so this is a direct governance/consensus-influencing mechanism reachable by any funded, unprivileged account issuing signed `FreezeBalanceV2Contract`/`VoteWitnessContract`/`UnfreezeBalanceV2Contract` transactions.

### Impact Explanation
An attacker with sufficient capital can manipulate the active Super Representative (witness) set for a single maintenance cycle without sustained economic commitment, since `FreezeBalanceV2` requires no lock-up period unlike the original `FreezeBalanceContract`. This can be used to install or displace witnesses, affecting block production, consensus, and reward distribution — a Medium/High severity governance-manipulation issue, since it can influence which entities produce blocks and control block rewards for a cycle, and repeated abuse could be used to bias the network's SR set.

### Likelihood Explanation
The precondition is simply having enough TRX to outweigh existing votes near a maintenance boundary (maintenance cycles occur roughly every few hours), and requires only ordinary, unprivileged transactions (`FreezeBalanceV2Contract`, `VoteWitnessContract`, `UnfreezeBalanceV2Contract`) — no special permissions, no witness/committee role, and no code changes to consensus logic are needed to execute it. The only cost is temporary illiquidity of the frozen TRX for `unfreezeDelayDays`, which is a resource cost, not a structural barrier, exactly as described in the original report's exploit scenario ("Eve buys tokens right before voting ends and sells them right after").

### Recommendation
Introduce a minimum lock-in / lookback requirement before a `FreezeBalanceV2Contract`-derived `TRON_POWER` can be counted toward votes at `doMaintenance()` — e.g., require the frozen balance to have existed for at least one full maintenance cycle (mirroring or exceeding the legacy `FROZEN_PERIOD`) before its associated votes are tallied in `MaintenanceManager.countVote()`, or apply a time-decayed/weighted vote-counting scheme as suggested in the original report so that votes cast immediately before a maintenance boundary carry proportionally less weight than long-held stake.

### Proof of Concept
Conceptual sequence using existing test infrastructure (`VoteWitnessActuatorTest`, `FreezeV2Test`) as a template:
1. Call `FreezeBalanceV2Actuator` with `resource=TRON_POWER` and a large `frozenBalance` for account `Eve`, immediately before the next maintenance boundary — validated to succeed with no duration restriction per `FreezeBalanceV2Actuator.validate()`.
2. Call `VoteWitnessActuator`/`VoteWitnessContract` from `Eve` for the target witness; `VoteWitnessActuator.validate()` only checks `sum <= tronPower`, which passes immediately.
3. Trigger `MaintenanceManager.doMaintenance()` (as in `VoteWitnessActuatorTest.voteWitness()`), which adds `Eve`'s vote weight to the target witness's `voteCount` and may change the active witness set via `dposService.updateWitness(...)`.
4. Immediately call `UnfreezeBalanceV2Actuator` for the same amount/resource; `checkExistFrozenBalance`/`checkUnfreezeBalance` succeed since the freeze exists, and only `unfreezeDelayDays` gates return of the TRX balance — the already-counted vote/witness-set change from step 3 is not reverted.

Note: full end-to-end confirmation of maintenance-cycle timing and exact vote-decay behavior across multiple maintenance ticks was not independently re-run in a live environment as part of this analysis; the control-flow evidence above (absence of any lock-duration check in `FreezeBalanceV2Actuator`/`FreezeBalanceV2Processor`, and vote tallying happening only at `doMaintenance()`) is based on static code review of the cited files.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L129-143)
```java
      long tronPower;
      DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
      if (dynamicStore.supportAllowNewResourceModel()) {
        tronPower = accountCapsule.getAllTronPower();
      } else {
        tronPower = accountCapsule.getTronPower();
      }

      sum = LongMath
          .checkedMultiply(sum, TRX_PRECISION); //trx -> drop. The vote count is based on TRX
      if (sum > tronPower) {
        throw new ContractValidateException(
            "The total number of votes[" + sum + "] is greater than the tronPower[" + tronPower
                + "]");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java (L131-141)
```java
    long frozenBalance = freezeBalanceV2Contract.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("frozenBalance must be positive");
    }
    if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("frozenBalance must be greater than or equal to 1 TRX");
    }

    if (frozenBalance > accountCapsule.getBalance()) {
      throw new ContractValidateException("frozenBalance must be less than or equal to accountBalance");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L1-9)
```java
package org.tron.core.actuator;

import static org.tron.core.actuator.ActuatorConstant.NOT_EXIST_STR;
import static org.tron.core.config.Parameter.ChainConstant.FROZEN_PERIOD;
import static org.tron.core.config.Parameter.ChainConstant.TRX_PRECISION;
import static org.tron.protos.contract.Common.ResourceCode;
import static org.tron.protos.contract.Common.ResourceCode.BANDWIDTH;
import static org.tron.protos.contract.Common.ResourceCode.ENERGY;
import static org.tron.protos.contract.Common.ResourceCode.TRON_POWER;
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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L103-185)
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
    if (!this.any.is(UnfreezeBalanceV2Contract.class)) {
      throw new ContractValidateException(
          "contract type error, expected type [UnfreezeBalanceContract], real type[" + any
              .getClass() + "]");
    }

    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support UnfreezeV2 transaction,"
          + " need to be opened by the committee");
    }

    final UnfreezeBalanceV2Contract unfreezeBalanceV2Contract;
    try {
      unfreezeBalanceV2Contract = this.any.unpack(UnfreezeBalanceV2Contract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    byte[] ownerAddress = unfreezeBalanceV2Contract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] does not exist");
    }

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    switch (unfreezeBalanceV2Contract.getResource()) {
      case BANDWIDTH:
        if (!checkExistFrozenBalance(accountCapsule, BANDWIDTH)) {
          throw new ContractValidateException("no frozenBalance(BANDWIDTH)");
        }
        break;
      case ENERGY:
        if (!checkExistFrozenBalance(accountCapsule, ENERGY)) {
          throw new ContractValidateException("no frozenBalance(Energy)");
        }
        break;
      case TRON_POWER:
        if (dynamicStore.supportAllowNewResourceModel()) {
          if (!checkExistFrozenBalance(accountCapsule, TRON_POWER)) {
            throw new ContractValidateException("no frozenBalance(TronPower)");
          }
        } else {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy]");
        }
        break;
      default:
        if (dynamicStore.supportAllowNewResourceModel()) {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy、TRON_POWER]");
        } else {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy]");
        }
    }

    if (!checkUnfreezeBalance(accountCapsule, unfreezeBalanceV2Contract, unfreezeBalanceV2Contract.getResource())) {
      throw new ContractValidateException(
          "Invalid unfreeze_balance, [" + unfreezeBalanceV2Contract.getUnfreezeBalance() + "] is error"
      );
    }

    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UNFREEZE_MAX_TIMES <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }

    return true;
  }
```
