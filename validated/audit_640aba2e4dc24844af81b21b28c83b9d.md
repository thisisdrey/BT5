## Analog Found: Missing Slippage Protection in `ExchangeWithdrawActuator` (TRX Bancor-style Exchange)

### Title
Withdrawing liquidity from a TRC10 Exchange pool via `ExchangeWithdrawContract` has no minimum-received slippage guard, unlike `ExchangeTransactionContract` - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
java-tron's bancor-formula based token Exchange lets a pool creator swap (`ExchangeTransactionContract`) or withdraw liquidity (`ExchangeWithdrawContract`) from a token pair pool. The swap path correctly implements a caller-supplied minimum-amount check (`expected`), but the withdraw path computes the counter-asset amount purely from the pool's ratio at execution time and has no equivalent caller-supplied minimum, exactly the pattern flagged in the report where `remove_liquidity_one_coin` is called with `_min_amount = 0`.

### Finding Description
`ExchangeTransactionContract` (a swap) includes an explicit `expected` field — "expected minimum number of tokens" — and `ExchangeTransactionActuator.doValidate()` enforces `anotherTokenQuant < tokenExpected` must fail: [1](#0-0) 

In contrast, `ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — no minimum/expected field: [2](#0-1) 

`ExchangeWithdrawActuator.doValidate()` computes `anotherTokenQuant` from the live `firstTokenBalance`/`secondTokenBalance` ratio at validation time and only checks that it is `> 0` and that the computed value is internally self-consistent within a rounding tolerance ("Not precise enough") — there is no user-supplied floor that guards against the ratio having moved due to intervening transactions: [3](#0-2) 

`execute()` then applies the same just-in-time computed ratio and mutates balances without any minimum-amount enforcement: [4](#0-3) 

Because Tron blocks execute multiple transactions in sequence, an attacker can submit an `ExchangeTransactionContract` swap against the same `exchange_id` immediately before the victim's `ExchangeWithdrawContract` is processed in the same block, skewing `firstTokenBalance`/`secondTokenBalance` so the withdrawer receives far less of the counter asset than they intended when they signed their transaction — with no on-chain parameter to reject the trade if the payout falls below an acceptable threshold. This is the direct analog of the reported Curve issue: burning LP-equivalent value (`tokenQuant` withdrawal) while accepting an unbounded-downside amount of the paired asset.

Note: `ExchangeInjectContract` (deposit side) has the same structural gap, but since it only *adds* value (and validates the injector has enough balance of both sides) rather than extracting value, it is not exploitable for loss in the same way; the withdraw path is the one where an attacker profits at the withdrawer's expense.

### Impact Explanation
An exchange creator (the only account authorized to call `ExchangeWithdrawContract`, since `doValidate()` requires `accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())`) can be sandwiched: a third party front-runs the withdrawal with a same-block swap that shifts the pool ratio, causing the creator to receive substantially less of the counter-asset than the pool state implied when they built and signed their transaction. This is a direct loss-of-funds vector for the withdrawing party, matching the High severity of the underlying report (loss of funds due to absent slippage floor). [5](#0-4) 

### Likelihood Explanation
Any unprivileged account can broadcast an `ExchangeTransactionContract` against an arbitrary `exchange_id` at any time — no special permission is required beyond having the relevant token/TRX balance, and transaction ordering within a block/mempool can be influenced by fee/timing. Any pool creator withdrawing liquidity with a pool that has meaningful trading activity is exposed every time they withdraw, since there is no way to specify a floor for the counter asset received.

### Recommendation
Add a caller-supplied minimum-amount field to `ExchangeWithdrawContract` (analogous to `expected` in `ExchangeTransactionContract`) and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()` by rejecting the transaction if the computed `anotherTokenQuant` is below that caller-specified floor, mirroring the check already implemented in `ExchangeTransactionActuator`.

### Proof of Concept
1. Account `C` creates an exchange pool between token `A`/`B` and holds LP-equivalent rights as `creatorAddress`.
2. `C` builds an `ExchangeWithdrawContract` to withdraw `quant` of token `A`, expecting to receive `X` of token `B` based on the current pool ratio, and signs it.
3. Before `C`'s transaction is included, attacker `M` submits a large `ExchangeTransactionContract` swap against the same `exchange_id` that heavily skews the `firstTokenBalance`/`secondTokenBalance` ratio.
4. When `C`'s withdraw is processed (`ExchangeWithdrawActuator.execute`, lines 77-90), `anotherTokenQuant` is recomputed from the now-skewed ratio; since there is no `expected`/minimum field to check against, the transaction succeeds and `C` receives significantly less than `X` of token `B`.
5. `M` can subsequently reverse their swap to restore the ratio, having extracted value from `C`'s withdrawal — the classic sandwich/slippage attack the report describes for `remove_liquidity_one_coin` with `_min_amount = 0`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** Tron protobuf protocol document.md (L1403-1420)
```markdown
     - message `ExchangeWithdrawContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to withdraw.
    
       `quant`: token amount to withdraw.
    
      ```java
      message ExchangeWithdrawContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L71-90)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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
