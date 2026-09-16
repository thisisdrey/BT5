This confirms the vulnerability: the `SUICIDE`/`SUICIDE2` opcode of the TVM (`suicide`/`suicide2` in `Program.java`) transfers frozen-V2 resources to an inheritor, but the unfreeze bookkeeping is broken.

### Title
Self-destruct via `SUICIDE`/`SUICIDE2` opcode prematurely clears `unfrozenV2` list, permanently losing not-yet-matured unfrozen TRX - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
`Program.transferFrozenV2BalanceToInheritor` only forwards to the inheritor the portion of the owner's `unfrozenV2` entries whose `unfreezeExpireTime` has already elapsed, but then unconditionally clears the entire `unfrozenV2` list for the self-destructing account via `clearOwnerFreezeV2`. Any `unfrozenV2` entries that have not yet matured are wiped without being credited to anyone, mirroring the reported `clearExtraRewards` bug class where an array holding unclaimed value is cleared before all entries are settled.

### Finding Description
When a contract executes `SUICIDE`/`SUICIDE2` with TVM freeze-V2 enabled, `Program.suicide`/`suicide2` call `transferFrozenV2BalanceToInheritor`: [1](#0-0) 

Inside that method, only the sum of `unfrozenV2` entries whose `unfreezeExpireTime <= nowTimestamp` is computed and credited to the inheritor: [2](#0-1) 

Immediately after, `clearOwnerFreezeV2` is called, which unconditionally clears the *entire* `unfrozenV2` list, regardless of whether individual entries had matured: [3](#0-2) 

Any `unfrozenV2` entry whose `unfreezeExpireTime > nowTimestamp` (i.e., the unfreeze waiting period had not yet elapsed) is therefore dropped from the array without its `unfreezeAmount` ever being credited to the inheritor or retained by the owner. Since the owner account is marked self-destructed (`markSelfDestruct`) and removed from state, those funds become permanently unreachable — no future transaction can claim them because the list entry that would have made them withdrawable no longer exists.

This is directly analogous to the reported `clearExtraRewards` issue: an array tracking pending/claimable value is cleared in full instead of only removing the entries that have actually been settled, causing loss of access to value that was still pending.

### Impact Explanation
Any account (owner of a smart contract, or an externally-owned account executing `SUICIDE` through a contract call) that has pending, not-yet-expired `UnfreezeBalanceV2` entries and then self-destructs will permanently lose that unfrozen TRX. This is a direct, unrecoverable loss of user funds reachable by any transaction sender who: (1) unfreezes TRX via `UnfreezeBalanceV2` (creating an `unfrozenV2` entry with a future expire time), and (2) triggers contract self-destruct (`SUICIDE`/`SUICIDE2`) before that entry matures. No special privileges are required.

### Likelihood Explanation
The sequence — unfreeze V2 balance, then self-destruct before the unfreeze waiting period elapses — is a normal, permissionless operation any user or contract can trigger with two transactions. This does not require malicious SR/witness/committee behavior, network manipulation, or leaked keys; it is a standard actuator/opcode flow (`UnfreezeBalanceV2` contract + `CREATE`/`CALL` triggering a contract that executes `SUICIDE`).

### Recommendation
In `transferFrozenV2BalanceToInheritor`, do not blanket-clear the `unfrozenV2` list. Either:
- Transfer *all* `unfrozenV2` entries (matured and unmatured) to the inheritor account's `unfrozenV2` list, preserving their original `unfreezeExpireTime`, so the inheritor can withdraw them once they mature, or
- Only remove the entries that were actually matured/credited (`unfreezeExpireTime <= nowTimestamp`) from the list, and merge the remaining unmatured entries into the inheritor's `unfrozenV2` list instead of discarding them via `clearOwnerFreezeV2`.

### Proof of Concept
1. Deploy a contract account, call `UnfreezeBalanceV2` for that account (e.g., unfreeze BANDWIDTH) so that `AccountCapsule.unfrozenV2` gets an entry with `unfreezeAmount = X` and `unfreezeExpireTime = now + 14 days` (per network unfreeze delay).
2. Before 14 days elapse, have the contract execute `SUICIDE`/`SUICIDE2` to an inheritor address.
3. In `transferFrozenV2BalanceToInheritor`, `expireUnfrozenBalance` computed at line 673-679 excludes the pending entry (since `unfreezeExpireTime > nowTimestamp`), so it is not credited to the inheritor.
4. `clearOwnerFreezeV2` at line 686 (`ownerCapsule.clearUnfrozenV2()`) wipes the `unfrozenV2` list including the pending entry.
5. The owner account is marked self-destructed; the amount `X` is no longer recorded anywhere and can never be withdrawn — permanent fund loss. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L581-588)
```java
    // transfer freezeV2
    if (VMConfig.allowTvmFreezeV2()) {
      long expireUnfrozenBalance =
          transferFrozenV2BalanceToInheritor(owner, obtainer, getContractState());
      if (expireUnfrozenBalance > 0 && internalTx != null) {
        internalTx.setValue(internalTx.getValue() + expireUnfrozenBalance);
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L671-704)
```java
    // withdraw expire unfrozen balance
    long nowTimestamp = repo.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();
    long expireUnfrozenBalance =
        ownerCapsule.getUnfrozenV2List().stream()
            .filter(
                unFreezeV2 ->
                    unFreezeV2.getUnfreezeAmount() > 0 && unFreezeV2.getUnfreezeExpireTime() <= nowTimestamp)
            .mapToLong(Protocol.Account.UnFreezeV2::getUnfreezeAmount)
            .sum();
    if (expireUnfrozenBalance > 0) {
      inheritorCapsule.setBalance(inheritorCapsule.getBalance() + expireUnfrozenBalance);
      increaseNonce();
      addInternalTx(null, ownerAddr, inheritorAddr, expireUnfrozenBalance, null,
          "withdrawExpireUnfreezeWhileSuiciding", nonce, null);
    }
    clearOwnerFreezeV2(ownerCapsule);
    repo.updateAccount(ownerCapsule.createDbKey(), ownerCapsule);
    repo.updateAccount(inheritorCapsule.createDbKey(), inheritorCapsule);
    return expireUnfrozenBalance;
  }

  private void clearOwnerFreeze(AccountCapsule ownerCapsule) {
    ownerCapsule.setFrozenForBandwidth(0, 0);
    ownerCapsule.setFrozenForEnergy(0, 0);
  }

  private void clearOwnerFreezeV2(AccountCapsule ownerCapsule) {
    ownerCapsule.clearFrozenV2();
    ownerCapsule.setNetUsage(0);
    ownerCapsule.setNewWindowSize(BANDWIDTH, 0);
    ownerCapsule.setEnergyUsage(0);
    ownerCapsule.setNewWindowSize(ENERGY, 0);
    ownerCapsule.clearUnfrozenV2();
  }
```
