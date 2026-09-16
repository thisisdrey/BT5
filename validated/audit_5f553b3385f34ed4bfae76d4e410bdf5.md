### Title
Missing slippage/minimum-amount protection in `ExchangeWithdrawActuator` allows sandwich attacks on TRX/TRC10 bancor-style exchange withdrawals - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract`/`ExchangeWithdrawActuator` lets an exchange creator withdraw `quant` of one token from a bancor-style liquidity pool (`ExchangeCapsule`) and receive a proportional amount of the paired token (`anotherTokenQuant`), but the contract exposes no minimum-acceptable-output parameter. The paired-token amount the withdrawer actually receives is derived purely from the pool's balances **at execution time**, which anyone can move beforehand via the fully public `ExchangeTransactionContract`, enabling a sandwich attack analogous to the reported "min tokens out not enforced" issue.

### Finding Description
`ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — there is no `expected`/minimum-out field, unlike `ExchangeTransactionContract` which does carry an `expected` field that is enforced in `ExchangeTransactionActuator.doValidate()`: [1](#0-0) 

In contrast, `ExchangeWithdrawActuator.doValidate()` computes `anotherTokenQuant` purely from the current `firstTokenBalance`/`secondTokenBalance` ratio, with only a fixed-point rounding-precision sanity check ("Not precise enough") — no user-supplied minimum bound on the amount received: [2](#0-1) 

The same unconstrained ratio-based computation is repeated in `execute()`: [3](#0-2) 

Because `firstTokenBalance`/`secondTokenBalance` can be freely and atomically shifted by any account via a normal, publicly reachable `ExchangeTransactionContract` trade (which only requires funds and passes its own `expected` check against the *attacker's own* trade, not the victim's), a withdraw transaction sitting in the mempool can be sandwiched:
1. Attacker observes the creator's pending `ExchangeWithdrawContract` transaction.
2. Attacker submits an `ExchangeTransactionContract` trade that skews the `firstTokenBalance`/`secondTokenBalance` ratio unfavorably for the withdrawer (front-run).
3. The victim's withdraw executes against the skewed ratio, yielding a much smaller `anotherTokenQuant` than expected when signed.
4. Attacker submits a reverse trade to restore/profit from the ratio shift (back-run), extracting value that would otherwise have gone to the withdrawer.

This is a direct instance of the reported bug class: the withdraw path relies on the state of an underlying "market" (the exchange pool) at execution time without enforcing a minimum acceptable output chosen by the withdrawing user.

### Impact Explanation
A single-signed transaction (`ExchangeWithdrawContract`) can be sandwiched by unrelated, unprivileged accounts using ordinary `ExchangeTransactionContract` calls, causing the withdrawer to receive materially less of the paired token than intended — a concrete value-extraction/theft scenario against the withdrawing account, satisfying the "unauthorized... theft of funds" bar. Exchange creators (who created TRX/TRC10 pools) are the parties affected; the loss is proportional to trade size and pool depth/slippage.

### Likelihood Explanation
Likelihood is Medium: it requires mempool visibility and the ability to submit trades against the specific exchange before the withdraw transaction is packed into a block — a standard MEV/front-running capability, not a privileged or novel one, and `ExchangeTransactionContract` is a normal, low-fee, publicly reachable contract type available to any account.

### Recommendation
Add a `min_another_token_quant` (or similar) field to `ExchangeWithdrawContract` and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()`, rejecting the withdrawal (`ContractValidateException`) if the computed `anotherTokenQuant` at execution time is below the user-specified minimum, mirroring the `expected` check already present in `ExchangeTransactionActuator`.

### Proof of Concept
1. Alice creates/owns an exchange pool via `ExchangeCreateContract` and later wants to withdraw liquidity via `ExchangeWithdrawContract{exchange_id, token_id=A, quant=Q}`, expecting `anotherTokenQuant ≈ secondTokenBalance*Q/firstTokenBalance` based on the pool state she observed.
2. Bob (any account) sees Alice's pending withdraw transaction in the mempool and submits an `ExchangeTransactionContract` trade against the same `exchange_id` that shifts `firstTokenBalance`/`secondTokenBalance` unfavorably (this only needs to satisfy Bob's own `expected` check, per `ExchangeTransactionActuator.doValidate()` lines 217-221).
3. Alice's withdraw transaction executes next, computing `anotherTokenQuant` from the now-skewed balances in `ExchangeWithdrawActuator.execute()` lines 74-89, with no floor to reject the unfavorable outcome.
4. Bob submits a follow-up trade restoring the ratio, capturing the value difference extracted from Alice's withdrawal.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-272)
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
      if (secondTokenBalance < tokenQuant || firstTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }

      if (allowHarden) {
        BigDecimal remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
    }
```
