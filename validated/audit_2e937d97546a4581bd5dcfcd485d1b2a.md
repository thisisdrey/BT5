## Finding

### Title
Missing `supportUnfreezeDelay()` feature-gate check in `WithdrawExpireUnfreezeProcessor` allows TVM contract callers to bypass committee-controlled unfreeze-delay activation - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java`)

### Summary
The Stake 2.0 "unfreeze delay" withdrawal path is implemented twice: once as the ordinary transaction actuator `WithdrawExpireUnfreezeActuator` and once as the TVM native-contract processor `WithdrawExpireUnfreezeProcessor` (reachable from a smart contract via the staking precompile). Both perform the same state mutation — moving expired `UnFreezeV2` entries into the account balance — but only the actuator enforces the governance gate that this feature must first be enabled by the committee.

### Finding Description
`WithdrawExpireUnfreezeActuator.validate()` explicitly requires the feature to be turned on before allowing the withdrawal: [1](#0-0) 

By contrast, `WithdrawExpireUnfreezeProcessor.validate()` — the sibling implementation invoked from the TVM native-contract/precompile path used by smart contracts — performs balance and existence checks but never calls `dynamicStore.supportUnfreezeDelay()`: [2](#0-1) 

This mirrors the reported Napier pattern exactly: several "sibling" entry points into the same sensitive state (`issue`/`updateUnclaimedYield`/`collect` vs. `redeemWithYT` in the Solidity report) are supposed to share a common protective gate (`whenNotPaused` there, `supportUnfreezeDelay()` here), but one of the entry points omits it. Confirming this, the same asymmetry exists for the corresponding delegate/undelegate resource pair: `DelegateResourceActuator` and `UnDelegateResourceActuator` both call `supportUnfreezeDelay()` in their `validate()` methods, and the equivalent native contract processors (`DelegateResourceProcessor`, `UnDelegateResourceProcessor`) were also found to reference `supportDR`/`supportUnfreezeDelay`, but `WithdrawExpireUnfreezeProcessor` has no such reference at all.

### Impact Explanation
`supportUnfreezeDelay()` gates the entire Stake 2.0 "expire-unfreeze" withdrawal mechanism until the committee proposal enabling `unfreezeDelayDays`/the associated ALLOW flag has been approved. If the native-contract path is reachable while the feature is still disabled network-wide, a smart contract could execute the withdrawal logic (moving `UnFreezeV2` amounts back into an account's spendable balance) before the feature and its associated accounting/consensus rules are properly active, potentially producing an inconsistent or premature balance credit relative to the rest of the protocol's freeze/unfreeze v2 state machine, which functions as an "unauthorized account operation / unbacked balance" class of impact.

### Likelihood Explanation
This is reachable by any unprivileged party that can trigger the staking-related native/precompiled contract from within a deployed smart contract (a single signed `TriggerSmartContract` transaction), i.e., no special privilege is required. The condition only manifests while the committee proposal is not yet approved network-wide, but during that window it is deterministically triggerable.

### Recommendation
Add the same `if (!dynamicStore.supportUnfreezeDelay()) { throw new ContractValidateException(...); }` gate at the start of `WithdrawExpireUnfreezeProcessor.validate()` to match `WithdrawExpireUnfreezeActuator`, ensuring the TVM-invoked path cannot be used to bypass the governance-controlled feature flag.

### Proof of Concept
1. Ensure the committee has not yet enabled the unfreeze-delay proposal (`dynamicStore.supportUnfreezeDelay()` returns `false`).
2. Attempt to call `WithdrawExpireUnfreezeActuator` via a normal `WithdrawExpireUnfreezeContract` transaction — it is rejected with "Not support WithdrawExpireUnfreeze transaction, need to be opened by the committee" per [1](#0-0) .
3. Instead, invoke the equivalent staking precompile/native contract from a smart contract that internally calls `WithdrawExpireUnfreezeProcessor.validate()`/`execute()`. Because this processor never checks `supportUnfreezeDelay()` ( [2](#0-1) ), the withdrawal succeeds and expired `UnFreezeV2` balances are credited to the account despite the feature being disabled at the protocol/governance level.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L84-87)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support WithdrawExpireUnfreeze transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java (L26-60)
```java
  public void validate(WithdrawExpireUnfreezeParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    if (Objects.isNull(accountCapsule)) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(ACCOUNT_EXCEPTION_STR
          + readableOwnerAddress + NOT_EXIST_STR);
    }

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    List<Protocol.Account.UnFreezeV2> unfrozenV2List = accountCapsule.getInstance()
        .getUnfrozenV2List();
    long totalWithdrawUnfreeze = getTotalWithdrawUnfreeze(unfrozenV2List, now);
    if (totalWithdrawUnfreeze < 0) {
      throw new ContractValidateException("no unFreeze balance to withdraw ");
    }
    try {
      LongMath.checkedAdd(accountCapsule.getBalance(), totalWithdrawUnfreeze);
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
  }
```
