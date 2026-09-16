Confirmed: `TransactionInfoCapsule` has a `withdraw_amount` field (populated from `ret.getWithdrawAmount()` set in `WithdrawBalanceActuator.execute()`), but `TransactionLogTriggerCapsule`'s constructor switch statement over `contractType` (framework/src/main/java/org/tron/common/logsfilter/capsule/TransactionLogTriggerCapsule.java:115-265) has no `case WithdrawBalanceContract:` branch at all — unlike `UnfreezeBalanceContract`, `WithdrawExpireUnfreezeContract`, and `CancelAllUnfreezeV2Contract`, which each populate `assetAmount`/`extMap` from the corresponding `TransactionInfo` fields.

### Title
Missing event population for WithdrawBalanceContract causes off-chain listeners to always observe zero withdrawn amount - (File: framework/src/main/java/org/tron/common/logsfilter/capsule/TransactionLogTriggerCapsule.java)

### Summary
The `TransactionLogTriggerCapsule` constructor builds a `TransactionLogTrigger` event object that is broadcast to plugins/subscribers (used by exchanges, wallets, and monitoring services) for on-chain activity. For several native contract types that move TRX (`UnfreezeBalanceContract`, `WithdrawExpireUnfreezeContract`, `CancelAllUnfreezeV2Contract`), the code correctly reads the actual moved amount from the `TransactionInfo` (populated during actuator execution) and sets `assetName`/`assetAmount` on the trigger. However, no equivalent branch exists for `WithdrawBalanceContract`, even though `WithdrawBalanceActuator.execute()` computes and stores a real amount (`ret.setWithdrawAmount(allowance)`) at [1](#0-0) , which is carried into `TransactionInfoCapsule` via the `withdraw_amount` field.

### Finding Description
This is directly analogous to the reported Solidity issue: an event meant to communicate a fund movement to off-chain consumers is effectively "wrongly emitted" (in this case, entirely un-populated / defaulted) even though the underlying protocol data (`withdraw_amount` on `TransactionInfo`) has the correct value. Compare the handled cases: [2](#0-1) [3](#0-2) 

against the full switch statement, which has no `WithdrawBalanceContract` case and falls through to `default: break;`: [4](#0-3) [5](#0-4) 

As a result, `transactionLogTrigger.getAssetAmount()` for any `WithdrawBalanceContract` transaction is left at its default value (0), and `fromAddress`/`toAddress`/`assetName` are also unset, regardless of the actual TRX amount withdrawn from the witness allowance to the owner's balance.

### Impact Explanation
Any downstream system (exchange deposit/withdrawal trackers, accounting dashboards, wallets) that relies on the java-tron event-plugin `TransactionLogTrigger` feed to detect and reconcile balance-affecting transactions will see `WithdrawBalanceContract` transactions reported with amount 0 and no from/to addresses, while real TRX has moved on-chain (from allowance into the account's spendable balance) — mirroring the "always reports 0" pattern from the source report and its "unexpected behavior in the frontend" impact. This does not itself cause loss of on-chain funds, but it can cause silent misreporting of asset movement to consumers of the officially supported event-notification path, leading to reconciliation errors or dropped notifications for legitimate reward withdrawals.

### Likelihood Explanation
This triggers on every `WithdrawBalanceContract` transaction, which is broadcastable by any account with a non-zero allowance (witness rewards) and is a normal, frequently used operation, so the incorrect/missing event data occurs deterministically and with high frequency whenever this contract type executes successfully.

### Recommendation
Add a `case WithdrawBalanceContract:` branch in `TransactionLogTriggerCapsule`'s switch statement, following the same pattern used for `UnfreezeBalanceContract`/`WithdrawExpireUnfreezeContract`: unpack the contract to get the owner address for `fromAddress`, set `assetName` to `"trx"`, and set `assetAmount` from `transactionInfo.getWithdrawAmount()`.

### Proof of Concept
1. Fund a witness account with allowance/reward via normal voting/reward-cycle mechanics.
2. Broadcast a `WithdrawBalanceContract` transaction from that account; `WithdrawBalanceActuator.execute()` moves `allowance` into `balance` and records `ret.setWithdrawAmount(allowance)` [6](#0-5) .
3. Observe the resulting `TransactionInfo.getWithdrawAmount()` correctly reflects the withdrawn amount, but the corresponding `TransactionLogTrigger` object produced by `TransactionLogTriggerCapsule` for this transaction has `assetAmount == 0` and empty `fromAddress`/`toAddress`, because no switch case exists to copy that data over.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-70)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());

    AccountCapsule accountCapsule = accountStore.
        get(withdrawBalanceContract.getOwnerAddress().toByteArray());
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(oldBalance + allowance)
        .setAllowance(0L)
        .setLatestWithdrawTime(now)
        .build());
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
    ret.setWithdrawAmount(allowance);
    ret.setStatus(fee, code.SUCESS);
```

**File:** framework/src/main/java/org/tron/common/logsfilter/capsule/TransactionLogTriggerCapsule.java (L113-115)
```java
      if (Objects.nonNull(contractParameter) && Objects.nonNull(contract)) {
        try {
          switch (contractType) {
```

**File:** framework/src/main/java/org/tron/common/logsfilter/capsule/TransactionLogTriggerCapsule.java (L181-196)
```java
            case UnfreezeBalanceContract:
              UnfreezeBalanceContract unfreezeBalanceContract = contractParameter
                  .unpack(UnfreezeBalanceContract.class);

              transactionLogTrigger.setFromAddress(StringUtil
                  .encode58Check(unfreezeBalanceContract.getOwnerAddress().toByteArray()));
              if (!ByteString.EMPTY.equals(unfreezeBalanceContract.getReceiverAddress())) {
                transactionLogTrigger.setToAddress(StringUtil
                    .encode58Check(unfreezeBalanceContract.getReceiverAddress().toByteArray()));
              }
              transactionLogTrigger.setAssetName("trx");
              if (Objects.nonNull(transactionInfo)) {
                transactionLogTrigger.setAssetAmount(
                    transactionInfo.getUnfreezeAmount());
              }
              break;
```

**File:** framework/src/main/java/org/tron/common/logsfilter/capsule/TransactionLogTriggerCapsule.java (L216-226)
```java
            case WithdrawExpireUnfreezeContract:
              WithdrawExpireUnfreezeContract withdrawExpireUnfreezeContract = contractParameter
                  .unpack(WithdrawExpireUnfreezeContract.class);

              transactionLogTrigger.setFromAddress(StringUtil.encode58Check(
                  withdrawExpireUnfreezeContract.getOwnerAddress().toByteArray()));
              transactionLogTrigger.setAssetName("trx");
              if (Objects.nonNull(transactionInfo)) {
                transactionLogTrigger.setAssetAmount(transactionInfo.getWithdrawExpireAmount());
              }
              break;
```

**File:** framework/src/main/java/org/tron/common/logsfilter/capsule/TransactionLogTriggerCapsule.java (L263-265)
```java
            default:
              break;
          }
```
