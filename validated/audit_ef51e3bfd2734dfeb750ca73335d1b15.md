Confirmed: `ExchangeWithdrawContract` and `ExchangeInjectContract` have no min/expected output field, unlike `ExchangeTransactionContract` which explicitly has an `expected` slippage-protection field ( [1](#0-0) ). This is the strongest analog to the reported bug class.

### Title
Bancor-style Exchange Withdraw/Inject lack slippage protection, exposing liquidity providers to sandwich attacks - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
Java-tron's TRC-10 Bancor-style exchange supports `ExchangeTransactionContract` (trading), which correctly implements a caller-supplied `expected` minimum-output parameter enforced in `doValidate()` [2](#0-1) . However, the sibling contracts `ExchangeWithdrawContract` and `ExchangeInjectContract`, used by the pool creator (an asset issuer) to add/remove liquidity, have **no equivalent minimum/maximum "expected" field** in their protobuf definitions [3](#0-2) . The proportional token amount the creator will receive on withdraw (or give up on inject) is computed purely from the *current* on-chain pool ratio at execution time, with no way for the creator to bound the outcome.

### Finding Description
`ExchangeWithdrawActuator.doValidate()` computes `anotherTokenQuant` strictly from the current `firstTokenBalance`/`secondTokenBalance` ratio in the `ExchangeCapsule` at the time the transaction is executed [4](#0-3) . The only checks performed are that the computed ratio is "precise enough" (an integer/rounding check), not a value the caller can constrain to protect against price movement [5](#0-4) .

Because `ExchangeTransactionContract` orders (freely submittable by any unprivileged transaction broadcaster) can move the pool's `firstTokenBalance`/`secondTokenBalance` ratio arbitrarily within a block (subject to available liquidity and fees), an attacker who observes a pending `ExchangeWithdrawContract` in the mempool can:
1. Front-run it with an `ExchangeTransactionContract` that skews the pool ratio unfavorably for the withdrawer's chosen `token_id`.
2. Let the victim's withdraw execute at the manipulated ratio, receiving less of `anotherTokenID` than they would have at the un-manipulated price.
3. Back-run with a reverse `ExchangeTransactionContract` to restore the ratio and capture the difference as arbitrage profit extracted directly from the withdrawer's expected proceeds.

The identical mechanism applies to `ExchangeInjectActuator`, which also computes `anotherTokenQuant` from the live pool ratio without any bound the caller can set [6](#0-5) , allowing an attacker to force the injector to contribute more of the second token than intended for a given `quant` of the first token.

This is the same root cause as the reported bug: a value-transferring operation whose price/exchange-rate exposure has **no caller-configurable minimum/maximum bound**, making it exploitable via classic sandwich/MEV extraction — except here the flaw is baked into the protocol contract schema itself (missing field), not merely a bot's runtime choice of parameter.

### Impact Explanation
An exchange creator withdrawing or injecting liquidity can have value siphoned by an attacker sandwiching the transaction with `ExchangeTransactionContract` trades, permanently and irreversibly transferring funds from the liquidity provider to the attacker. This is a concrete value-extraction/theft-of-funds impact reachable purely through ordinary signed transactions from unprivileged accounts (the attacker needs no special privilege — any asset holder can submit `ExchangeTransactionContract` trades against the pool).

### Likelihood Explanation
Exploitation requires only mempool visibility and the ability to submit two `ExchangeTransactionContract` transactions around the victim's `ExchangeWithdrawContract`/`ExchangeInjectContract` — no witness/committee/validator privilege is needed. Any pool with meaningful liquidity and normal transaction propagation delay is exposed. Likelihood scales with pool value and network mempool visibility, similar to standard AMM sandwich attacks.

### Recommendation
Add an explicit slippage-protection field to `ExchangeWithdrawContract` and `ExchangeInjectContract` protobufs (e.g., `min_another_token_quant` / `max_another_token_quant`), and enforce it in `ExchangeWithdrawActuator.doValidate()` / `ExchangeInjectActuator.doValidate()` analogous to the existing `expected` check in `ExchangeTransactionActuator` [2](#0-1) , so callers can bound their exposure to pool-ratio manipulation between transaction submission and inclusion.

### Proof of Concept
1. Attacker observes victim's pending `ExchangeWithdrawContract` (withdraw `quant` of `tokenID` from `exchangeId`, expecting proportional `anotherTokenID` based on current pool ratio).
2. Attacker submits an `ExchangeTransactionContract` trade that shifts the pool ratio so `anotherTokenID` becomes cheaper relative to `tokenID`.
3. Victim's withdraw executes via `ExchangeWithdrawActuator.execute()` [7](#0-6) , receiving less `anotherTokenID` than the pre-manipulation ratio implied, with no validation failure since there is no minimum-output bound to violate.
4. Attacker submits a reverse `ExchangeTransactionContract` to restore/rebalance the pool, realizing the captured difference as profit.

Note: I could not find any additional off-chain "bot" component analogous to the reported liquidation bot within this repository's scope (java-tron is node/blockchain software, not a bot); the closest and most concretely reachable analog within the allowed attack surface (single signed transactions against actuators) is the missing slippage bound on `ExchangeWithdrawContract`/`ExchangeInjectContract` described above.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-37)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-247)
```java
    BigDecimal bigFirstTokenBalance = new BigDecimal(String.valueOf(firstTokenBalance));
    BigDecimal bigSecondTokenBalance = new BigDecimal(String.valueOf(secondTokenBalance));
    BigDecimal bigTokenQuant = new BigDecimal(String.valueOf(tokenQuant));
    final boolean allowHarden = allowHarden();
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
