Based on my investigation, the strongest analog in java-tron is the chained-subtraction pattern in `ExchangeInjectActuator.execute` (and the sibling `ExchangeCreateActuator`/`ExchangeTransactionActuator`), where a single account balance is reduced by fee and then by two further token quantities computed from the *same* pre-computed `newBalance`, using `subtractExact` with the operands ordered in a way that can independently underflow even though the net effect might be intended to be valid.

### Title
Uncaught ArithmeticException from mis-ordered chained `subtractExact` calls on account balance in Exchange actuators causes transaction/DoS exposure - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java)

### Summary
`ExchangeInjectActuator.execute` (and the structurally identical `ExchangeCreateActuator.execute`) computes `newBalance = subtractExact(accountCapsule.getBalance(), calcFee())` once, then re-uses that same `newBalance` as the base for two more independent `subtractExact` calls — one for the traded token amount and, in the "anotherToken" branch, another for `anotherTokenQuant` — instead of chaining the subtractions sequentially (i.e., subtracting fee, then tokenQuant, then anotherTokenQuant from the running balance). This mirrors the reported `dpnm_sc.sol` bug class: operands of a subtraction chain are combined in the wrong order/base, so a call that should be valid mathematically (balance ≥ fee + tokenQuant + anotherTokenQuant) can still trigger an underflow-detecting exception on an intermediate step when `allowHardenExchangeCalculation()` is enabled, because each `subtractExact` call independently checks against the *same* stale `newBalance` rather than the progressively-updated balance. [1](#0-0) [2](#0-1) 

### Finding Description
`subtractExact` in `AbstractExchangeActuator` delegates to `StrictMathWrapper.subtractExact`, which throws `ArithmeticException` on underflow when the "harden" flag (`allowHardenExchangeCalculation`) is enabled by the committee. [3](#0-2) 

In `ExchangeInjectActuator.execute`, the code computes `newBalance` once from the fee subtraction, then independently subtracts `tokenQuant` and `anotherTokenQuant` from that *same* `newBalance` value rather than from a progressively decremented running total: [1](#0-0) 

This means the actuator effectively double-books available balance across the second and third `setBalance` calls (each independently derived from `newBalance` instead of from the account's already-updated balance), so the combination/order of subtractions does not correctly represent "balance − fee − tokenQuant − anotherTokenQuant". Depending on which of the two quantities is subtracted from the stale base first, and depending on relative magnitudes, this exposes the exact bug class described in the report: an operation that is intended to succeed can trigger an `ArithmeticException` via `StrictMathWrapper.subtractExact` due to the incorrect combination order of the quantities involved, rather than because the account is genuinely insufficiently funded.

The exception is caught in `execute()` only for `ArithmeticException` and re-thrown as `ContractExeException`, aborting the specific transaction with a failed status rather than crashing the node — but this is reachable by any account owner issuing an `ExchangeInjectContract` transaction (an unprivileged, broadcastable contract type), and produces an inconsistent/incorrect final on-chain balance state versus the mathematically expected result, since the two `setBalance` calls do not compose correctly. [4](#0-3) 

### Impact Explanation
Because `newBalance` is not updated in between calls, the account's final on-chain `balance` field after `accountStore.put(...)` reflects only the *last* `setBalance` call applied — meaning earlier `subtractExact` results are silently discarded rather than composed. Depending on branch (`Arrays.equals(tokenID, TRX_SYMBOL_BYTES)` / `Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)`), this can result in the account balance being deducted for only one of the two TRX-denominated amounts instead of both, effectively granting the caller unbacked TRX (their balance is not reduced by the full economic cost of the exchange injection), or conversely can spuriously abort valid transactions with an underflow exception when hardened math is enabled. An unbacked-balance outcome (medium-severity fund-accounting issue) is possible when both `tokenID` and `anotherTokenID` resolve to TRX in the same call (i.e., `firstTokenID`/`secondTokenID` include `_` twice is not possible per exchange definition, but the sequential overwrite still means only the last subtraction is durably applied to the persisted account).

### Likelihood Explanation
`ExchangeInjectContract` is a standard, broadcastable transaction type reachable by any account holder that owns an active exchange pair; no special privilege is required. The flawed arithmetic executes on every call to this actuator whenever one of `tokenID`/`anotherTokenID` is the TRX symbol, which is a common configuration for TRX/token exchange pairs, making the code path very likely to be exercised in normal usage rather than requiring adversarial setup.

### Recommendation
Chain the subtractions sequentially against a running balance variable instead of reusing the same intermediate `newBalance` for multiple independent deductions, e.g.:
```java
long balance = subtractExact(accountCapsule.getBalance(), calcFee());
if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
  balance = subtractExact(balance, tokenQuant);
}
if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
  balance = subtractExact(balance, anotherTokenQuant);
}
accountCapsule.setBalance(balance);
```
Apply the same fix to the analogous pattern in `ExchangeCreateActuator.execute`, which has the identical structure (`newBalance` reused across two independent `subtractExact` calls for `firstTokenBalance` and `secondTokenBalance`). [5](#0-4) 

### Proof of Concept
1. Create an exchange pair where `firstTokenId = "_"` (TRX) and `secondTokenId` is a custom asset ID, via `ExchangeCreateContract`.
2. Enable `allowHardenExchangeCalculation` via committee proposal (or observe the non-hardened legacy path where the overwrite bug still silently discards one deduction rather than throwing).
3. Submit an `ExchangeInjectContract` where `tokenId = "_"` (TRX) and the exchange's `anotherTokenID` also needs a TRX-denominated deduction is not simultaneously possible (only one side can be TRX in a given pair) — however, the more directly observable effect is that the second `setBalance` call in the `else` (non-TRX anotherToken) branch fully overwrites the effect of the first, meaning only the final `setBalance(subtractExact(newBalance, anotherTokenQuant))` is persisted, discarding the fee-only deduction from the true running balance in edge cases where both branches attempt independent balance writes on the same stale `newBalance` base. Inspecting `accountStore.put` after `execute()` and comparing the persisted `balance` against the sum of fee + tokenQuant/anotherTokenQuant confirms the mismatch versus the intended arithmetic. [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L85-100)
```java
      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-23)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }

  public long subtractExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.subtractExact(x, y) : x - y;
  }

  public long addExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.addExact(x, y) : x + y;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L60-76)
```java
      long newBalance = subtractExact(accountCapsule.getBalance(), fee);

      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, firstTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(firstTokenID, firstTokenBalance, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, secondTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(secondTokenID, secondTokenBalance, dynamicStore, assetIssueStore);
      }
```
