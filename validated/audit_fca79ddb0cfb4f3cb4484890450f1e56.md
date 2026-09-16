### Title
Phantom overflow in `ExchangeInjectActuator.execute()` causes validated exchange-inject transactions to unexpectedly revert - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`ExchangeInjectActuator` computes the counter-asset amount for an `ExchangeInjectContract` differently in `validate()` and `execute()`. `validate()` uses `BigInteger` multiplication/division (no overflow possible), while `execute()` recomputes the same value using checked `long` arithmetic (`multiplyExact(...)` followed by `floorDiv`). Because the *intermediate product* can exceed `Long.MAX_VALUE` even when the *final quotient* is small and well within all configured limits, a transaction that passes `validate()` can throw an `ArithmeticException` in `execute()`, causing an unnecessary `ContractExeException`/`FAILED` transaction — a phantom overflow leading to an unexpected revert of an otherwise legitimate transaction.

### Finding Description
`ExchangeInjectActuator.doValidate()` computes the counter-token amount using unbounded `BigInteger` math: [1](#0-0) 

`execute()` recomputes the very same value using bounded `long` arithmetic via `multiplyExact`/`floorDiv`: [2](#0-1) 

`multiplyExact` throws `ArithmeticException` whenever the *intermediate* product `secondTokenBalance * tokenQuant` (or `firstTokenBalance * tokenQuant`) overflows a signed 64-bit long — regardless of whether the final quotient (`anotherTokenQuant`) fits comfortably in a `long`. Since `validate()` never checks that this intermediate product stays within `long` range (it only checks the final `anotherTokenQuant` and resulting balances against `dynamicStore.getExchangeBalanceLimit()`), a transaction can be fully validated and still throw in `execute()`.

The exchange balance limit defaults to `1_000_000_000_000_000L` (1e15): [3](#0-2) 

Both `firstTokenBalance` and `secondTokenBalance` are constrained by prior operations to stay `<= balanceLimit` (~1e15), and `tokenQuant` is likewise bounded by the same limit (`newTokenBalance = tokenBalance + tokenQuant <= balanceLimit`). However `1e15 * 1e15 = 1e30`, and even far smaller combinations such as `9e14 * 1e5 = 9e19` already exceed `Long.MAX_VALUE` (~9.223e18), while the resulting quotient (`9e19 / 1e12 = 9e7`) stays trivially small and within the configured balance limit. `validate()` (BigInteger-based) approves such a transaction, but `execute()` (long-based `multiplyExact`) throws.

### Impact Explanation
Any account that is the creator of an exchange pair can construct an `ExchangeInjectContract` whose numeric parameters cause the intermediate multiplication in `execute()` to overflow a `long`, even though the transaction is entirely legitimate and passes `validate()`. This results in the actuator throwing `ArithmeticException` → `ContractExeException`, causing the transaction to be marked `FAILED` after having already passed validation. This is an inconsistency between the two phases of transaction processing (validate vs execute) that an unprivileged, signed transaction (`ExchangeInjectContract`, reachable by any exchange-creator account) can trigger, denying a legitimate operation and wasting the caller's resources/fee on a transaction that should have succeeded. Because the overflow check (`multiplyExact`) occurs before any state mutation in `execute()` (lines 73–83 precede all `setBalance`/`put` calls), no partial state corruption or fund loss occurs — the impact is limited to an unexpected, avoidable revert of a transaction that the node itself already approved.

### Likelihood Explanation
Likelihood is high for the specific actor who owns/creates the exchange pair (only the exchange creator can call `ExchangeInjectContract`, per `doValidate()`'s creator-address check), and the triggering condition only requires ordinary large-but-in-limit balances/quantities that are fully achievable given the default `1e15` `EXCHANGE_BALANCE_LIMIT`. No special privileges beyond being the exchange creator are needed, and no external conditions (price, network state) beyond picking appropriate `tokenQuant`/balances are required.

### Recommendation
Make `execute()` use the same unbounded arithmetic as `validate()` (i.e., compute `anotherTokenQuant` via `BigInteger.multiply().divide()` as `doValidate()` already does, or reuse the value validated), instead of `multiplyExact`/`floorDiv` on `long`. This removes the intermediate-overflow-only-in-execute() mismatch and prevents legitimately validated transactions from failing during execution. The same pattern should be reviewed in any other actuator computing `long` products bounded by a proven-safe divisor path in `validate()` but re-derived with checked `long` math in `execute()`.

### Proof of Concept
1. Create an exchange (`ExchangeCreateContract`) with `firstTokenBalance = 1_000_000,000,000 (1e12)` and `secondTokenBalance = 900,000,000,000,000 (9e14)` (both `<= EXCHANGE_BALANCE_LIMIT` default `1e15`).
2. Submit `ExchangeInjectContract` from the exchange creator with `tokenId = firstTokenID`, `tokenQuant = 100,000 (1e5)`.
   - `newTokenBalance = firstTokenBalance + tokenQuant = 1e12 + 1e5` — within limit.
   - `validate()` computes `anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance) = (9e14 * 1e5) / 1e12 = 9e7`, well within limit; `newAnotherTokenBalance = 9e14 + 9e7` — within limit. Transaction passes `validate()`.
3. In `execute()`, `multiplyExact(secondTokenBalance, tokenQuant) = multiplyExact(9e14, 1e5)` computes `9e19`, which exceeds `Long.MAX_VALUE` (~9.223e18) and throws `ArithmeticException`.
4. The actuator catches the `ArithmeticException`, sets `ret.setStatus(fee, code.FAILED)`, and rethrows as `ContractExeException` — the transaction that passed `validate()` fails unexpectedly at `execute()`. [4](#0-3)

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L107-112)
```java
    } catch (ItemNotFoundException | InvalidProtocolBufferException
        | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L209-227)
```java
    BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
    BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
    BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
    long newTokenBalance;
    long newAnotherTokenBalance;

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
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L556-560)
```java
    try {
      this.getExchangeBalanceLimit();
    } catch (IllegalArgumentException e) {
      this.saveExchangeBalanceLimit(1_000_000_000_000_000L);
    }
```
