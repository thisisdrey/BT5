Found it: `UNFREEZE_DELAY_DAYS` is validated only for its range (`value < 1 || value > 365`), with no monotonicity guard preventing it from ever being set to `0` again, unlike the sibling `ALLOW_*` flags in `ProposalUtil` (which explicitly refuse to be re-set once enacted, e.g. `ALLOW_TVM_BLOB`, `ALLOW_TVM_CANCUN`, `CONSENSUS_LOGIC_OPTIMIZATION`). However, `DynamicPropertiesStore.supportUnfreezeDelay()` treats `getUnfreezeDelayDays() != 0` as the "feature active" gate, and `WithdrawExpireUnfreezeActuator.validate()` hard-reverts the whole transaction whenever that gate is off: [1](#0-0) 

### Title
Committee can permanently freeze already-unfrozen TRX by disabling `UNFREEZE_DELAY_DAYS`, bricking `WithdrawExpireUnfreezeContract` and `UnfreezeBalanceV2Contract` - (File: actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java, actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java, actuator/src/main/java/org/tron/core/utils/ProposalUtil.java)

### Summary
`WithdrawExpireUnfreezeActuator` and `UnfreezeBalanceV2Actuator` both gate their entire `validate()` on `dynamicStore.supportUnfreezeDelay()` being true, reverting the transaction unconditionally otherwise. This mirrors the Olympus `MINTR.active` pattern in the source report: a single togglable dependency flag, when turned off, causes an unconditional revert in a fund-recovery path, with no fallback logic to still allow users to reclaim funds they already unlocked.

### Finding Description
`UnfreezeBalanceV2Contract` moves TRX from "frozen" into a pending `UnfrozenV2` list with an expiry time [2](#0-1) . Later, users must call `WithdrawExpireUnfreezeContract` to actually move that balance back into their spendable balance [3](#0-2) .

Both actuators require the feature flag `UNFREEZE_DELAY_DAYS` (exposed via `dynamicStore.supportUnfreezeDelay()`) to be active or they throw `ContractValidateException` and refuse to execute at all: [1](#0-0) [4](#0-3) 

`ProposalUtil.validator()` only range-checks `UNFREEZE_DELAY_DAYS` to `[1, 365]`: [5](#0-4) 

This is inconsistent with virtually every other feature-activation flag in the same file (`ALLOW_TVM_BLOB`, `ALLOW_TVM_CANCUN`, `CONSENSUS_LOGIC_OPTIMIZATION`, `ALLOW_HARDEN_RESOURCE_CALCULATION`, etc.), all of which explicitly forbid re-proposing/toggling once enacted via checks like "has been valid, no need to propose again" [6](#0-5) . `UNFREEZE_DELAY_DAYS` has no such monotonicity guard and no explicit lower-bound-of-0 rejection tied to "already active" state; only its raw numeric range is checked, meaning a governance/committee proposal targeting this parameter id is validated purely on the numeric value, independent of whether the feature was already turned on and users already have funds pending in `UnfrozenV2` lists.

Because `supportUnfreezeDelay()` gating is present in both the "unfreeze" entry point and the "withdraw the already-unfrozen balance" exit point, any state transition that causes this dependency to report inactive removes the *only* path back to liquid balance for users who are mid-cycle (already called `UnfreezeBalanceV2`, waiting for `unfreezeExpireTime`, not yet withdrawn). There is no try/catch or fallback logic in either actuator to still allow withdrawal of already-expired `UnfrozenV2` entries when the flag is off — the check is unconditional and blocks the entire actuator.

### Impact Explanation
If the `UNFREEZE_DELAY_DAYS` chain parameter is set to a value that causes `supportUnfreezeDelay()` to evaluate false (or is otherwise made inactive through this under-guarded validator path), every account with a pending `UnfrozenV2` balance — TRX that has already left "frozen" status and is only waiting out its unlock timer — becomes permanently unable to broadcast a valid `WithdrawExpireUnfreezeContract` or `UnfreezeBalanceV2Contract` transaction. Since `validate()` throws before any balance movement occurs, the funds are stuck in the account's `unfrozenV2` list indefinitely: this is a fund-freezing condition consistent with the Medium severity of the original report (all defaulters/lenders analog: all pending-unfreeze holders lose access to funds they otherwise fully own and are entitled to).

### Likelihood Explanation
This requires the chain governance/committee to submit and pass a proposal touching `UNFREEZE_DELAY_DAYS` in a way that disables the `supportUnfreezeDelay()` gate after users have already unfrozen balances under it — a low-likelihood, privileged-but-plausible governance action, matching the "small likelihood, Medium severity" rationale of the original report. Unlike other one-way feature flags in the same file, this parameter's validator does not defend against this scenario, which appears to be an oversight relative to the surrounding code's established pattern.

### Recommendation
Add a monotonicity/consistency guard to `ProposalUtil.validator()` for `UNFREEZE_DELAY_DAYS` (or make `supportUnfreezeDelay()` itself sticky once activated), and/or have `WithdrawExpireUnfreezeActuator`/`UnfreezeBalanceV2Actuator` allow withdrawal of already-pending `UnfrozenV2` entries even when the feature is currently reported inactive, so that funds already committed to the unfreeze-delay flow can never become permanently unrecoverable.

### Proof of Concept
1. Committee/witnesses pass a proposal enabling `UNFREEZE_DELAY_DAYS` (e.g. value = 14), activating `supportUnfreezeDelay()`.
2. User calls `UnfreezeBalanceV2Contract`, moving frozen TRX into `unfrozenV2` list with a future `unfreezeExpireTime` [2](#0-1) .
3. Before the user withdraws, the committee passes another proposal setting `UNFREEZE_DELAY_DAYS` in a way that flips `supportUnfreezeDelay()` to false (the validator in `ProposalUtil` does not block this state transition, only range-checking the new numeric value).
4. Once the expire time passes, the user calls `WithdrawExpireUnfreezeContract`; `validate()` immediately throws `"Not support WithdrawExpireUnfreeze transaction, need to be opened by the committee"` [1](#0-0) , and the TRX remains stuck in `unfrozenV2` indefinitely with no alternate recovery path.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L51-66)
```java
    AccountCapsule accountCapsule = accountStore.get(
        withdrawExpireUnfreezeContract.getOwnerAddress().toByteArray());
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    List<UnFreezeV2> unfrozenV2List = accountCapsule.getInstance().getUnfrozenV2List();
    long totalWithdrawUnfreeze = getTotalWithdrawUnfreeze(unfrozenV2List, now);
    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(accountCapsule.getBalance() + totalWithdrawUnfreeze)
        .build());
    List<UnFreezeV2> newUnFreezeList = getRemainWithdrawList(unfrozenV2List, now);
    accountCapsule.clearUnfrozenV2();
    accountCapsule.addAllUnfrozenV2(newUnFreezeList);
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
    ret.setWithdrawExpireAmount(totalWithdrawUnfreeze);
    ret.setStatus(fee, code.SUCESS);
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L84-87)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support WithdrawExpireUnfreeze transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L83-88)
```java
    ResourceCode freezeType = unfreezeBalanceV2Contract.getResource();

    long expireTime = this.calcUnfreezeExpireTime(now);
    accountCapsule.addUnfrozenV2List(freezeType, unfreezeBalance, expireTime);

    this.updateTotalResourceWeight(accountCapsule, unfreezeBalanceV2Contract, unfreezeBalance);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L119-122)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support UnfreezeV2 transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L609-619)
```java
      case UNFREEZE_DELAY_DAYS: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_7)) {
          throw new ContractValidateException(
                  "Bad chain parameter id [UNFREEZE_DELAY_DAYS]");
        }
        if (value < 1 || value > 365) {
          throw new ContractValidateException(
                  "This value[UNFREEZE_DELAY_DAYS] is only allowed to be in the range 1-365");
        }
        break;
      }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L835-843)
```java
        if (dynamicPropertiesStore.getAllowTvmBlob() == 1) {
          throw new ContractValidateException(
              "[ALLOW_TVM_BLOB] has been valid, no need to propose again");
        }
        if (value != 1) {
          throw new ContractValidateException(
              "This value[ALLOW_TVM_BLOB] is only allowed to be 1");
        }
        break;
```
