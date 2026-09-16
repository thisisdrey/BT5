### Title
Guard Representative reward-withdrawal restriction can be bypassed via UnfreezeBalance / Vote paths that also trigger `withdrawReward` - (File: actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java)

### Summary
The Sherlock report describes a check (`notSuspended`) that is enforced on the direct withdraw path but omitted on a cooperating path (`fillCloseRequest`) that produces the same economic effect, letting a restricted user extract funds indirectly. java-tron has an analogous asymmetry: the "Guard Representative may not withdraw witness allowance/reward" restriction is enforced only in `WithdrawBalanceActuator` (and the TVM `WithdrawRewardProcessor`), but the underlying `MortgageService.withdrawReward()` routine — which credits the exact same reward into the account's spendable `balance` — is also invoked unconditionally from several other actuators/native-contract processors that do not carry the guard-representative check.

### Finding Description
`WithdrawBalanceActuator.validate()` explicitly forbids a genesis "guard representative" from withdrawing their accumulated witness allowance: [1](#0-0) 

This is the java-tron analog of the `notSuspended(msg.sender)` modifier in the report: a single, security-relevant guard limited to one entry point.

However, the same underlying operation — `MortgageService.withdrawReward(ownerAddress)`, which moves the witness reward/allowance into the account's spendable balance — is also called directly from `UnfreezeBalanceActuator.execute()` with no equivalent guard-representative check performed anywhere in its `validate()`: [2](#0-1) 

Grep evidence confirms the isGP/guard-representative check text only appears in `WithdrawBalanceActuator` and the TVM `WithdrawRewardProcessor`, while `withdrawReward` itself is also referenced from `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `VoteWitnessActuator`, `VoteWitnessProcessor`, `UnfreezeBalanceProcessor`, and `UnfreezeBalanceV2Processor` — none of which carry the matching restriction. This mirrors the reported pattern exactly: the restriction is attached to one action (`WithdrawBalanceContract`) but not to the sibling actions (`UnfreezeBalanceContract`, `UnfreezeBalanceV2Contract`, `VoteWitnessContract`, and their TVM native-contract equivalents) that route through the same fund-crediting primitive.

### Impact Explanation
A witness address flagged as a genesis "guard representative" — explicitly barred by protocol design from withdrawing accumulated allowance via `WithdrawBalanceContract` — can still realize that same balance credit by broadcasting an `UnfreezeBalanceContract`, `UnfreezeBalanceV2Contract`, or `VoteWitnessContract` transaction (or invoking the equivalent TVM native contracts), since each of those code paths calls `mortgageService.withdrawReward(ownerAddress)` without checking the guard-representative restriction. This is a concrete bypass of an explicit unauthorized-withdrawal restriction, resulting in funds being released to an account that the protocol intends to keep locked — an unauthorized account operation on protocol-restricted balances.

### Likelihood Explanation
Reachable by any account matching the genesis guard-representative address list, requiring only a single signed `UnfreezeBalanceContract`/`UnfreezeBalanceV2Contract`/`VoteWitnessContract` transaction (or its TVM native-contract equivalent) — no special privileges beyond holding frozen balance/votes are needed to trigger `withdrawReward`.

### Recommendation
Apply the same guard-representative check used in `WithdrawBalanceActuator.validate()` to every code path that invokes `MortgageService.withdrawReward()` — specifically `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `VoteWitnessActuator`, and the corresponding TVM native-contract processors (`UnfreezeBalanceProcessor`, `UnfreezeBalanceV2Processor`, `VoteWitnessProcessor`) — so the restriction cannot be bypassed through an alternate transaction type.

### Proof of Concept
1. Identify an address present in `CommonParameter.getInstance().getGenesisBlock().getWitnesses()` (a guard representative) that has frozen balance and accrued witness allowance/reward.
2. Confirm that submitting `WithdrawBalanceContract` from this address is rejected with "is a guard representative and is not allowed to withdraw Balance" per [1](#0-0) .
3. Instead, submit an `UnfreezeBalanceContract` transaction from the same address; `UnfreezeBalanceActuator.execute()` unconditionally calls `mortgageService.withdrawReward(ownerAddress)` per [2](#0-1) , crediting the same reward to the account balance without the guard-representative check being evaluated anywhere in `UnfreezeBalanceActuator.validate()`.
4. Observe that the previously-blocked allowance/reward has been withdrawn into spendable `balance`, confirming the restriction bypass.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L112-119)
```java
    boolean isGP = CommonParameter.getInstance()
        .getGenesisBlock().getWitnesses().stream().anyMatch(witness ->
            Arrays.equals(ownerAddress, witness.getAddress()));
    if (isGP) {
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress
              + "] is a guard representative and is not allowed to withdraw Balance");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L64-77)
```java
    try {
      unfreezeBalanceContract = any.unpack(UnfreezeBalanceContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    byte[] ownerAddress = unfreezeBalanceContract.getOwnerAddress().toByteArray();

    //
    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    long oldBalance = accountCapsule.getBalance();
```
