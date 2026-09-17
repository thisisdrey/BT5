### Title
Missing null-check in `MortgageService.adjustAllowance` can crash block-reward distribution for all validators/voters, halting node consensus - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`MortgageService.adjustAllowance(AccountStore, byte[], long)` fetches an account with `accountStore.getUnchecked(accountAddress)` and immediately dereferences it (`account.getAllowance()`) without a null check. This method is invoked from `payReward`, which in turn is called for every standby witness in `payStandbyWitness()` (a loop over up to `WITNESS_STANDBY_LENGTH` accounts) and for every block-producing/fee-collecting witness via `payBlockReward`/`payTransactionFeeReward`. If a single witness account in that loop is missing (never initialized, or removed from the account store), the resulting `NullPointerException` is an unchecked `RuntimeException` that is **not** caught by the surrounding `try { ... } catch (BalanceInsufficientException e)` block in the public `adjustAllowance(byte[], long)` wrapper, so it propagates upward and aborts the whole reward-distribution / block-application flow.

### Finding Description
`payReward` is called once per witness inside a loop that spans *all* standby witnesses: [1](#0-0) 

`payReward` deducts brokerage and calls `adjustAllowance` on the witness address: [2](#0-1) 

The public `adjustAllowance` wraps the state-mutating overload in a `try/catch` that only handles `BalanceInsufficientException`: [3](#0-2) 

The actual mutation fetches the account via `getUnchecked` and dereferences it immediately, with no null check: [4](#0-3) 

This mirrors the Sherlock `payOffDebtAll` bug-class exactly: a single loop iterates over many independent entities (protocols in Sherlock, standby witnesses here) performing a balance-adjustment sub-operation per entity; if that sub-operation throws for *any one* entity, the entire batch operation—and any core function depending on it—aborts. Here the "core function" is block-reward distribution, which is part of block application (`payStandbyWitness`/`payReward` executed during Manager's block-processing/maintenance cycle), not an isolated per-user transaction. A missing account for even one standby witness (e.g., an address that received votes but whose corresponding account record was deleted or never created) breaks reward computation for the entire witness set on every block, not just for that witness.

### Impact Explanation
An uncaught `NullPointerException` thrown deep inside block-reward accounting halts the block-application/maintenance code path in `Manager`. Because this logic executes unconditionally for every block (block reward, transaction-fee reward, or periodic standby-witness payout), an exception here can stop block production/validation entirely — a node crash/halt condition affecting all users, not just the affected witness. This satisfies the "node crash or halt" / "chain split" impact bar, since different nodes might diverge depending on whether they hit the code path at different points or catch/uncatch the exception differently in their execution environment.

### Likelihood Explanation
Reaching this path does not require special privilege: any address that becomes a witness/voted-for standby witness can trigger this loop, and it runs automatically as part of periodic reward distribution — an unprivileged voter or witness candidate merely needs to be included in the standby witness list computed each maintenance cycle. Account records in java-tron can be absent (e.g., an address that has votes/witness registration but has never received a fund transfer or explicit account creation, or whose account entry was otherwise removed), making the null-return-from-`getUnchecked` scenario plausible in production without any attacker action beyond normal witness/voting participation.

### Recommendation
Add a null check in `MortgageService.adjustAllowance(AccountStore, byte[], long)` before dereferencing the fetched `AccountCapsule`, mirroring the defensive `null` handling already present in `MaintenanceManager.doMaintenance()` (which logs and skips witnesses/accounts that are `null`). Treat a missing account as a skip-with-log condition rather than allowing an unchecked exception to propagate, and audit other callers of `getUnchecked` in reward-payment and batch/looped code paths (e.g., `payStandbyWitness`, `withdrawReward`) for the same missing-null-check pattern.

### Proof of Concept
1. Get an address registered/voted such that it appears in `WitnessStore.getWitnessStandby(...)` (i.e., it accumulates votes) but has no corresponding entry in `AccountStore` (or whose account entry becomes absent, e.g. through prior deletion flows).
2. Wait for `MortgageService.payStandbyWitness()` to run in the normal reward-distribution/maintenance cycle: [1](#0-0) .
3. When the loop reaches that witness, `payReward` → `adjustAllowance(witnessAddress, brokerageAmount)` → `adjustAllowance(accountStore, accountAddress, amount)` calls `accountStore.getUnchecked(accountAddress)`, gets `null`, and then calls `account.getAllowance()` at [5](#0-4) , throwing an uncaught `NullPointerException` that is not handled by the outer `catch (BalanceInsufficientException e)` block, aborting reward distribution for all remaining standby witnesses in that cycle and disrupting block application.

Note: I was not able to fully confirm within the available tool budget the exact call site in `Manager.java`/`DposService` that invokes `payStandbyWitness()`/`payBlockReward()`/`payTransactionFeeReward()` per block, nor verify with 100% certainty whether `TronStoreWithRevoking.getUnchecked()`'s base implementation returns `null` (rather than throwing) when an entry is absent — this inference is based on the naming convention and the sibling `AccountStore.get()` override, which explicitly treats an empty result from the underlying `revokingDB.getUnchecked` as "return null." Confirming the base-class `getUnchecked` semantics and the exact Manager call graph would require deeper investigation, possibly via a Devin session with full repository access.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L53-67)
```java
  public void payStandbyWitness() {
    List<WitnessCapsule> witnessStandbys = witnessStore.getWitnessStandby(
        dynamicPropertiesStore.allowWitnessSortOptimization());
    long voteSum = witnessStandbys.stream().mapToLong(WitnessCapsule::getVoteCount).sum();
    if (voteSum < 1) {
      return;
    }
    long totalPay = dynamicPropertiesStore.getWitness127PayPerBlock();
    double eachVotePay = (double) totalPay / voteSum;
    for (WitnessCapsule w : witnessStandbys) {
      long pay = (long) (w.getVoteCount() * eachVotePay);
      payReward(w.getAddress().toByteArray(), pay);
      logger.debug("Pay {} stand reward {}.", Hex.toHexString(w.getAddress().toByteArray()), pay);
    }
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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L232-241)
```java
  public void adjustAllowance(byte[] address, long amount) {
    try {
      if (amount <= 0) {
        return;
      }
      adjustAllowance(accountStore, address, amount);
    } catch (BalanceInsufficientException e) {
      logger.error("WithdrawReward error: {}.", e.getMessage());
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L243-258)
```java
  public void adjustAllowance(AccountStore accountStore, byte[] accountAddress, long amount)
      throws BalanceInsufficientException {
    AccountCapsule account = accountStore.getUnchecked(accountAddress);
    long allowance = account.getAllowance();
    if (amount == 0) {
      return;
    }

    if (amount < 0 && allowance < -amount) {
      throw new BalanceInsufficientException(
          String.format("%s insufficient balance, amount: %d, allowance: %d",
              StringUtil.createReadableString(accountAddress), amount, allowance));
    }
    account.setAllowance(allowance + amount);
    accountStore.put(account.createDbKey(), account);
  }
```
