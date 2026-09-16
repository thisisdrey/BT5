### Title
Missing slippage/minimum-output protection in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` allows sandwich-attack value extraction - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the counterpart token amount for a liquidity add/remove purely from the exchange's current on-chain balance ratio at execution time, with no caller-supplied minimum/maximum bound. This is the same root-cause bug class as the reported `ThecosomataETH.addLiquidity` finding: an AMM-style operation whose "expected amount" is derived from the current pool state inside the same transaction, with no independent slippage/minimum-output guard, making it exploitable via front-running/sandwiching. By contrast, the sibling `ExchangeTransactionActuator` (the actual swap) correctly takes a caller-supplied `expected` field and enforces `anotherTokenQuant >= tokenExpected`.

### Finding Description
`ExchangeInjectActuator.execute`/`doValidate` computes `anotherTokenQuant` as `secondTokenBalance * tokenQuant / firstTokenBalance` (or the symmetric case) directly from `exchangeCapsule.getFirstTokenBalance()`/`getSecondTokenBalance()` read at execution time: [1](#0-0) 
The validate path performs the identical calculation and only checks that the result is `> 0` and within the global balance limit — there is no comparison against any caller-provided minimum/expected value: [2](#0-1) 

`ExchangeWithdrawActuator` has the same structure — the counterpart amount is computed from the live pool ratio with no expected/minimum parameter at all in `ExchangeWithdrawContract`: [3](#0-2) 

This is inconsistent with `ExchangeTransactionActuator`, which was explicitly designed (per the `ExchangeTransactionContract.expected` field) to let the caller enforce a minimum received amount: [4](#0-3) [5](#0-4) 

Because the pool ratio (`firstTokenBalance`/`secondTokenBalance`) can be moved arbitrarily by any unprivileged account via `ExchangeTransactionContract` immediately before the victim's inject/withdraw is included in a block, and then moved back afterward, a bot can sandwich the inject/withdraw transaction: front-run to shift the ratio unfavorably for the victim, let the victim's inject/withdraw execute at the manipulated ratio, then back-run to restore the ratio and pocket the difference. The victim has no on-chain mechanism to bound the acceptable counterpart amount, exactly mirroring the reported bug class where an AMM operation's "expected amount" is taken from the pool at execution time without any independent slippage guard.

### Impact Explanation
An exchange creator calling `ExchangeInjectContract` or `ExchangeWithdrawContract` can have value extracted from them by an MEV/sandwich actor who only needs to submit ordinary `ExchangeTransactionContract` transactions (fully unprivileged, reachable by any signed transaction) around the victim's transaction. This results in economic loss to the victim's TRX/TRC10 balance — a concrete value-extraction/theft-of-funds scenario, consistent with Medium severity as judged in the original report.

### Likelihood Explanation
Exploitation requires only the ability to broadcast ordinary, unprivileged `ExchangeTransactionContract` transactions and observe the victim's pending inject/withdraw transaction in the mempool — no special privilege, malicious SR/witness/peer role, or protocol-level access is needed. Any user can also become an exchange "creator" themselves via `ExchangeCreateContract`, so the inject/withdraw actuators are reachable by ordinary accounts performing routine liquidity operations on pools they created.

### Recommendation
Add an explicit minimum/maximum bound parameter to `ExchangeInjectContract` and `ExchangeWithdrawContract` (analogous to `ExchangeTransactionContract.expected`), and validate the computed `anotherTokenQuant` against it in both `ExchangeInjectActuator` and `ExchangeWithdrawActuator`, rejecting the transaction if the computed amount falls outside the caller's tolerated range.

### Proof of Concept
1. Attacker monitors the mempool for a pending `ExchangeInjectContract` (or `ExchangeWithdrawContract`) transaction from the exchange creator for exchange `id`.
2. Attacker submits an `ExchangeTransactionContract` swap against the same exchange to shift `firstTokenBalance`/`secondTokenBalance` unfavorably for the pending inject/withdraw, ordered to execute before it.
3. The victim's inject/withdraw executes using `exchangeCapsule.transaction`/ratio math shown in `ExchangeInjectActuator.java:71-83` (or `ExchangeWithdrawActuator.java:74-89`) against the manipulated balances, receiving a worse counterpart amount than expected, with no check to reject this (`ExchangeInjectActuator.java:229-231` only checks `> 0`).
4. Attacker submits a reverse `ExchangeTransactionContract` to restore the ratio and realize the extracted value.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-236)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }

    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** protocol/src/main/protos/core/Tron.proto (L50-59)
```text
// Exchange
message Exchange {
  int64 exchange_id = 1;
  bytes creator_address = 2;
  int64 create_time = 3;
  bytes first_token_id = 6;
  int64 first_token_balance = 7;
  bytes second_token_id = 8;
  int64 second_token_balance = 9;
}
```
