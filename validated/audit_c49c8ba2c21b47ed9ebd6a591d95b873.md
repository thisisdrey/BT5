This confirms it: `ExchangeInjectContract` and `ExchangeWithdrawContract` (unlike `ExchangeTransactionContract`) have no `expected`/minimum-output field in their proto definitions [1](#0-0) , whereas `ExchangeTransactionContract` explicitly carries an `expected` field that is checked against the computed output before executing the swap [2](#0-1) [3](#0-2) .

### Title
Missing slippage/minimum-output protection in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` enables front-running/sandwich attacks against liquidity providers - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The TRX exchange (Bancor-style AMM) actuators that mutate a liquidity pool's paired-token ratio — `ExchangeInjectActuator` (add liquidity) and `ExchangeWithdrawActuator` (remove liquidity) — compute the counterpart token amount from the exchange's live `firstTokenBalance`/`secondTokenBalance` ratio at execution time, but unlike `ExchangeTransactionActuator` they accept no caller-specified minimum/maximum bound on that computed amount. This omission is exactly the front-running/sandwich-attack class flagged in the external report: because a broadcaster's expected settlement amount is not bound to what they signed for, an attacker observing the pending transaction in mempool can front-run it to shift the pool ratio.

### Finding Description
`ExchangeTransactionContract` includes an `expected` field, and `ExchangeTransactionActuator.doValidate()` explicitly rejects the trade if the computed `anotherTokenQuant` is less than `tokenExpected`, protecting the swap initiator from adverse price movement caused by preceding transactions [3](#0-2) .

In contrast, `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `owner_address`, `exchange_id`, `token_id`, and `quant` — no bound on the resulting counterpart amount [1](#0-0) .

`ExchangeInjectActuator.execute()` derives `anotherTokenQuant` purely from the pool's current balances at execution time (`floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)`), with no minimum-amount check anywhere in `doValidate()` [4](#0-3) . Because it reduces the injector's balance of both tokens by `tokenQuant` and the derived `anotherTokenQuant`, a pool ratio shifted by a preceding attacker transaction directly changes how much of the second asset the injector is forced to contribute for the same `tokenQuant`.

Similarly, `ExchangeWithdrawActuator.execute()` computes `anotherTokenQuant` from the live ratio and credits it to the withdrawer with only a "precision" sanity check (`allowHarden`/remainder logic) — never a caller-supplied minimum acceptable output [5](#0-4) [6](#0-5) .

Because both `ExchangeCreateContract`-derived exchanges are reachable by any unprivileged account issuing a signed `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction, and any account can also send a preceding `ExchangeTransactionContract` swap to shift `firstTokenBalance`/`secondTokenBalance` just before the victim transaction is packed into a block, an attacker (including a block-producing SR observing its own mempool) can manipulate the ratio the victim's inject/withdraw will settle against.

### Impact Explanation
An attacker can sandwich a victim's `ExchangeInject` or `ExchangeWithdraw` transaction: submit a large swap that skews the pool ratio immediately before the victim's transaction, let the victim's inject/withdraw execute at the manipulated ratio (forcing them to inject an excessive amount of the second token, or withdraw less of the second token than the fair-ratio amount would yield), then reverse the swap afterward to restore the ratio and capture the difference. This results in a direct, unauthorized value transfer from liquidity providers to the attacker — a concrete theft-of-funds impact, without requiring any of the excluded malicious-SR/witness/p2p preconditions (a normal user with two back-to-back transactions suffices, and a block-producing SR gets even more reliable ordering control but is not required).

### Likelihood Explanation
The attack requires no special privileges — only the ability to submit ordinary `ExchangeTransactionContract`, `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions and observe the mempool/ordering, both of which are available to any broadcaster. TRON's block time and transaction-ordering-by-fee model make ordering manipulation feasible for anyone willing to pay slightly higher bandwidth/energy priority, and is trivial for block-producing nodes. The lack of any `expected`/min-out field on these two contract types (unlike the sibling `ExchangeTransactionContract`) shows this protection was deliberately implemented for swaps but omitted for inject/withdraw.

### Recommendation
Add a caller-specified bound (e.g., `expected_another_token_min` for inject, or a minimum-acceptable-`anotherTokenQuant` for withdraw) to `ExchangeInjectContract`/`ExchangeWithdrawContract`, mirroring the `expected` field already present on `ExchangeTransactionContract`, and enforce it in `ExchangeInjectActuator.doValidate()`/`ExchangeWithdrawActuator.doValidate()` before mutating balances.

### Proof of Concept
1. Attacker observes a pending `ExchangeInjectContract` transaction from Victim targeting `exchangeId=X`, injecting `tokenQuant` of `firstTokenID`.
2. Attacker submits an `ExchangeTransactionContract` swap on the same exchange with higher fee/priority that shifts `firstTokenBalance`/`secondTokenBalance` sharply (e.g., dumping a large amount of `secondTokenID` into the pool), which is validated only against the trade's own `expected` bound [3](#0-2) .
3. Victim's `ExchangeInjectContract` executes next, computing `anotherTokenQuant` from the now-skewed ratio via `floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` [7](#0-6) , forcing Victim to contribute far more `secondTokenID` than the pre-attack ratio would have required, with no validation check to reject this unfavorable rate.
4. Attacker submits a reverse `ExchangeTransactionContract` swap to restore the ratio, pocketing the extra `secondTokenID` value extracted from Victim's inject.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-29)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L31-37)
```text
message ExchangeTransactionContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
  int64 expected = 5;
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L65-83)
```java
      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-89)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-243)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```
