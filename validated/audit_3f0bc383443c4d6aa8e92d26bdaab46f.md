Confirmed: `ExchangeCreateActuator.execute()` deducts real TRX/TRC10 balance from the creator's account into an `ExchangeCapsule`'s `firstTokenBalance`/`secondTokenBalance` fields [1](#0-0) . Those two `long` fields are the sole record of what the pool "holds" — there is no separate custodial account; trades pay real TRX/tokens out of the trader's own account and into the pool's virtual counters via `ExchangeCapsule.transaction()` [2](#0-1) , and the legacy (non-hardened) arithmetic path uses plain `long` `+`/`-` with no bounds check unless `hardenedCalc` is true.

### Title
Integer underflow in legacy `ExchangeCapsule.transaction()` pool accounting allows minting unbacked TRX/TRC10 balance - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java)

### Summary
`ExchangeTransactionActuator` lets any account trade against a bancor-style liquidity pool (`ExchangeCapsule`). The payout amount (`anotherTokenQuant`) is computed by `ExchangeProcessor.exchange()` using floating-point `Math.pow` on user-controlled `sellTokenQuant`, and the pool's counters are updated with raw `long` subtraction (`secondTokenBalance - buyTokenQuant` / `firstTokenBalance - buyTokenQuant`) when the "hardened" calculation is not enabled [3](#0-2) . The `< 0` sanity check that would reject a negative resulting balance is gated behind `hardenedCalc` and is skipped entirely on the legacy path: [4](#0-3) .

### Finding Description
This mirrors the reported bug class: code assumes an arithmetic accumulation "will not" go out of range and omits the corresponding guard on one code path (analogous to `multicall.sol`'s unchecked `valaccumulator`). Here, the omitted guard is the negative-balance check for the legacy (`hardenedCalc == false`) `ExchangeProcessor` path.

`ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute the payout with raw `double` math and no overflow/rounding safety net [5](#0-4) . `ExchangeTransactionActuator.doValidate()` only checks that the *requested* `anotherTokenQuant >= tokenExpected` — it never checks that `anotherTokenQuant <= secondTokenBalance` (or `firstTokenBalance`), unlike `ExchangeWithdrawActuator`, which does perform an explicit "exchange balance is not enough" check [6](#0-5) . Combined with the missing negative-balance guard in `transaction()` for the non-hardened path, a sell that produces a `buyTokenQuant` larger than the pool's stored counterpart balance drives the tracked balance negative silently, while `ExchangeTransactionActuator.execute()` still unconditionally credits the trader's real account with the full `anotherTokenQuant` via `addExact`/`addAssetAmountV2` [7](#0-6) , i.e., paying out more TRX/TRC10 than the pool actually holds. This is functionally an unbacked-balance mint — the "hacker steals staked/backed funds by exploiting an unguarded arithmetic corner-case" pattern from the report.

Whether the extreme `sellTokenQuant` values needed to overshoot `buyTokenBalance` are actually reachable is not fully proven here: `ExchangeTransactionActuator.doValidate()` does clamp the growth of the *sold* token side via `getExchangeBalanceLimit()` at [8](#0-7) , which constrains `sellTokenQuant` (added to the sell-side balance) to stay under `balanceLimit`, and repeated trades using the exponential bonding-curve formula make it non-trivial to force `buyTokenQuant > buyTokenBalance` in a single call under realistic pool sizes. Confirming actual exploitability (single-call vs. multi-call sequence, and what `balanceLimit`/typical pool sizes make the double-precision curve overshoot the reserve) requires numeric analysis or a live/simulated test, which was not performed.

### Impact Explanation
If the underflow is reachable, a trader could receive more TRX or TRC10 tokens than the exchange pool holds, effectively minting unbacked balance credited to a real account — a direct "unbacked balance" / theft-of-funds outcome as defined in scope. `ExchangeTransactionContract` is reachable by any signed transaction from an unprivileged account, matching the required "order placer" attack surface.

### Likelihood Explanation
Medium-to-Low confidence: the code path clearly lacks the same negative-balance guard that exists in the hardened path (`SafeExchangeProcessor`) and lacks the explicit "exchange balance is not enough" check present in `ExchangeWithdrawActuator`, which is a real code smell/regression risk. However, actually forcing `buyTokenQuant` to exceed the counter-side reserve through the bonding-curve exponential formula, within the `getExchangeBalanceLimit()` constraint on the sell side, has not been numerically confirmed as achievable in a single transaction.

### Recommendation
Apply the same invariant check used in the hardened path to the legacy path as well: after computing `newFirstTokenBalance`/`newSecondTokenBalance` in `ExchangeCapsule.transaction()`, reject the transaction (throw `ContractValidateException`) whenever either result is negative, regardless of `hardenedCalc`. Additionally, add an explicit check in `ExchangeTransactionActuator.doValidate()` that `anotherTokenQuant` does not exceed the corresponding pool balance, mirroring `ExchangeWithdrawActuator`'s "exchange balance is not enough" check.

### Proof of Concept
Not independently verified against live numeric bounds. The conceptual PoC path is: attacker computes a `sellTokenQuant`/pool-size combination via `ExchangeProcessor`'s `Math.pow`-based curve such that the returned `buyTokenQuant` exceeds `secondTokenBalance` (or `firstTokenBalance`), submits `ExchangeTransactionContract` with that quant while `AllowHardenExchangeCalculation` is disabled (legacy path), and observes `ExchangeCapsule.transaction()` silently store a negative pool balance while `ExchangeTransactionActuator.execute()` credits the full `anotherTokenQuant` to the attacker's account balance/asset map.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

    long buyTokenQuant = 0;
    long firstTokenBalance = this.exchange.getFirstTokenBalance();
    long secondTokenBalance = this.exchange.getSecondTokenBalance();
    long newFirstTokenBalance;
    long newSecondTokenBalance;

    if (this.exchange.getFirstTokenId().equals(ByteString.copyFrom(sellTokenID))) {
      buyTokenQuant = processor.exchange(firstTokenBalance,
          secondTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(firstTokenBalance, sellTokenQuant)
          : firstTokenBalance + sellTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
          : secondTokenBalance - buyTokenQuant;

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-39)
```java
  private long exchangeToSupply(long balance, long quant) {
    logger.debug("balance: " + balance);
    long newBalance = balance + quant;
    logger.debug("balance + quant: " + newBalance);

    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }

  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L221-223)
```java
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L86-91)
```java
      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-205)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```
