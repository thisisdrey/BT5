Confirmed: `ExchangeWithdrawContract` has no `expected`/minimum-amount field, unlike `ExchangeTransactionContract` which does (`expected` field, checked against `tokenExpected` in `ExchangeTransactionActuator.doValidate()`). This is a direct analog to the reported DODO V3 bug class.

### Title
`ExchangeWithdrawActuator` lacks minimum-received-amount protection, enabling sandwich attacks on TRC10 bancor-exchange withdrawals - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawActuator` computes the counter-asset amount a user receives when withdrawing liquidity from a TRC10 bancor-style `Exchange` pair purely from the pool's current on-chain balances at execution time, with no user-supplied minimum-output parameter to protect against price movement between transaction submission and execution.

### Finding Description
`ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — there is no expected/minimum amount field [1](#0-0) . In `ExchangeWithdrawActuator.execute()`, `anotherTokenQuant` (the amount of the other token the withdrawer receives) is derived directly from the pool's live `firstTokenBalance`/`secondTokenBalance` ratio at execution time via `bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)` (or the symmetric case) [2](#0-1) . The same unconstrained ratio computation is repeated in `doValidate()` [3](#0-2) . Nowhere in `execute()` or `doValidate()` is `anotherTokenQuant` compared against any caller-supplied minimum — the only checks are that the exchange balance is sufficient and that `anotherTokenQuant > 0` and passes a "precise enough" rounding check [4](#0-3) .

This is directly analogous to the DODO V3 report: an attacker who controls block/transaction ordering (or who can submit their own transactions before/after the victim's) can manipulate `firstTokenBalance`/`secondTokenBalance` immediately before the victim's `ExchangeWithdrawContract` executes (e.g., via `ExchangeTransactionContract` trades against the same pair) to shift the exchange rate unfavorably, then reverse the manipulation afterward, extracting value from the victim's withdrawal — a classic sandwich attack.

By contrast, `ExchangeTransactionContract`/`ExchangeTransactionActuator` (the trade-side actuator for the same `Exchange` pair) explicitly carries an `expected` field [5](#0-4)  and rejects the transaction in validation if `anotherTokenQuant < tokenExpected` [6](#0-5) . `ExchangeWithdrawContract` has no equivalent slippage-protection field or check, so users withdrawing liquidity from an `Exchange` pair have no on-chain mechanism to bound the amount of counter-asset they will receive.

### Impact Explanation
Any account holding dToken-equivalent balance in a TRC10 `Exchange` pair (created via `ExchangeCreateContract`, funded via `ExchangeInjectContract`) that later calls `ExchangeWithdrawContract` can have the received counter-asset amount manipulated downward by an attacker sandwiching the withdrawal transaction with `ExchangeTransactionContract` trades. This results in unauthorized value extraction from ordinary users' withdrawal transactions — a concrete "theft of funds via sandwich attack" as described in the referenced report, applicable here because the withdraw path is reachable by any unprivileged account holding an `Exchange` position and has no user-configurable protection.

### Likelihood Explanation
Exploitation only requires the attacker to submit ordinary, unprivileged `ExchangeTransactionContract` transactions before and after the victim's `ExchangeWithdrawContract` transaction within the same block (or across adjacent blocks by observing pending transactions), which is achievable by any network participant with TRX/asset balance and no special permissions — same low-barrier reachability as the sherlock report's underlying bug class.

### Recommendation
Add a minimum-expected-amount field (e.g., `expected_another_token_quant`) to `ExchangeWithdrawContract`, and in `ExchangeWithdrawActuator.doValidate()`/`execute()` reject the transaction if the computed `anotherTokenQuant` is below that caller-supplied minimum, mirroring the existing `expected` check already implemented for `ExchangeTransactionContract`.

### Proof of Concept
1. Attacker observes a pending `ExchangeWithdrawContract` transaction from victim V withdrawing `tokenQuant` of `firstTokenID` from exchange pair `(firstTokenID, secondTokenID)`.
2. Attacker front-runs with an `ExchangeTransactionContract` trade that shifts `firstTokenBalance`/`secondTokenBalance` so the ratio used in `ExchangeWithdrawActuator.execute()` (lines 77-89) favors the attacker.
3. V's withdrawal executes, computing `anotherTokenQuant` off the manipulated ratio — no check exists to reject this (no `mindTokenAmount`/`expected` field in `ExchangeWithdrawContract`), so V receives less counter-asset than expected under fair pricing.
4. Attacker back-runs with an opposite `ExchangeTransactionContract` trade to restore the pool ratio and realize the extracted value, completing the sandwich. [7](#0-6)

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L24-29)
```text
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L63-89)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeWithdrawContract.getTokenId().toByteArray();
      long tokenQuant = exchangeWithdrawContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-247)
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

    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
