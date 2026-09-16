## Title
Exchange pool reserves can go negative under default (non‑hardened) math, creating unbacked token/TRX balances — (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
The bancor-style AMM pool used by `ExchangeTransactionContract` (`Exchange` a.k.a. TRC10/TRX market swap) computes the counter-party payout via `ExchangeCapsule.transaction()`. By default (`hardenedCalc = false`, i.e. `allowHardenExchangeCalculation` dynamic property disabled, which is the current mainnet default), the reserve update after a swap is performed with plain `long` arithmetic and the resulting reserve is never checked for going negative. The FCoin incident describes an exchange whose declared reserves did not actually back user claims; the analogous root cause here is that java-tron's own on-chain "Exchange" reserve accounting can be pushed negative/unbacked because the safety check that exists (`newFirstTokenBalance < 0 || newSecondTokenBalance < 0`) is gated behind the disabled hardened path.

### Finding Description
In `ExchangeCapsule.transaction()`: [1](#0-0) 

the computed `buyTokenQuant` comes from `ExchangeProcessor.exchange()` (default, non-hardened processor), which relies on `double`/`Math.pow` floating point math with no `BigDecimal` precision and no overflow guard: [2](#0-1) 

After computing `buyTokenQuant`, the new reserve balances are set with plain `+`/`-` (not `addExact`/`subtractExact`) when `hardenedCalc` is `false`:
```
newSecondTokenBalance = hardenedCalc
    ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
    : secondTokenBalance - buyTokenQuant;
```
Critically, the only sanity check that a post-trade reserve cannot go negative is:
```
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException("Exchange balance must be >=0 after transaction");
}
```
This check is skipped entirely in the default, non-hardened path. [3](#0-2) 

`ExchangeTransactionActuator.doValidate()` also never checks that the computed `anotherTokenQuant` is bounded by the actual reserve balance of the token being bought — it only checks `anotherTokenQuant < tokenExpected` (the caller-supplied slippage floor), which an attacker fully controls: [4](#0-3) 

If floating-point error in the bancor formula (`Math.pow(x, 0.0005)` / `Math.pow(x, 2000.0)`) produces a `buyTokenQuant` larger than the actual `buyTokenBalance` (this is most exploitable when the pool has been drained to a very small reserve on one side, or via repeated tiny/edge-ratio trades that accumulate floating point drift), the pool's balance for that token is silently allowed to go negative and gets persisted via `Commons.putExchangeCapsule(...)` in `ExchangeTransactionActuator.execute()`: [5](#0-4) 

The trader's account is simultaneously credited with `anotherTokenQuant` via `addAssetAmountV2`/`setBalance`, meaning tokens/TRX are paid out of the pool that the pool's recorded reserve never actually held — an unbacked-balance condition structurally analogous to the FCoin situation where users were credited/owed funds the reserve could not cover.

### Impact Explanation
A negative or corrupted `Exchange` reserve balance means: (1) the affected liquidity/market becomes permanently unable to honor subsequent withdrawals/injections proportionally (mirroring FCoin's inability to redeem), (2) the attacker who triggered the negative-balance trade receives assets/TRX that are not backed by the pool's real holdings, i.e., value is created out of thin air at the expense of other participants who injected liquidity into that exchange. This is unauthorized extraction of value from a shared reserve reachable by any account issuing a plain `ExchangeTransactionContract`.

### Likelihood Explanation
The developers themselves added the hardened negative-balance guard and overflow-safe arithmetic (`StrictMathWrapper.addExact`/`subtractExact`, `SafeExchangeProcessor`) as an opt-in fix gated by `allowHardenExchangeCalculation`, and test cases (`hardenedExecuteOverflowThrowsArithmeticException`) explicitly demonstrate that without hardening, the same code path allows arithmetic to proceed unchecked. Since hardening is off by default, any node running with default dynamic properties is exposed, and the bug is reachable by a single, unprivileged, signed `ExchangeTransactionContract` transaction — no special privileges required.

### Recommendation
Make the negative-balance / overflow-safe checks in `ExchangeCapsule.transaction()` unconditional (not gated by `hardenedCalc`), and additionally enforce in `ExchangeTransactionActuator.doValidate()`/`execute()` that `anotherTokenQuant` never exceeds the actual current reserve balance of the token being purchased, rejecting the transaction with `ContractValidateException`/`ContractExeException` otherwise. Consider making `allowHardenExchangeCalculation` (and `allowStrictMath`) the default via a hard fork proposal so all nodes enforce the safe path.

### Proof of Concept
1. Deploy/observe an `Exchange` pool with a heavily skewed ratio (or one already drained close to zero on one side via legitimate withdraw/inject sequences allowed by `ExchangeWithdrawActuator`/`ExchangeInjectActuator`).
2. Ensure `allowHardenExchangeCalculation` is `0` (chain default).
3. Submit an `ExchangeTransactionContract` with `tokenId`/`quant` chosen so that `ExchangeProcessor.exchange()`'s floating-point bancor formula returns a `buyTokenQuant` at or exceeding the current opposite-side reserve balance, and set `expected` low enough to pass the `anotherTokenQuant < tokenExpected` check.
4. Observe that `ExchangeCapsule.transaction()` computes `newSecondTokenBalance = secondTokenBalance - buyTokenQuant` going negative without throwing, since the `if (hardenedCalc && ...)` guard is skipped; `execute()` persists this negative reserve and credits the attacker's account, confirmed by `dbManager.getExchangeV2Store()...getFirstTokenBalance()/getSecondTokenBalance()` reading a negative value while the attacker's `AccountCapsule` balance/asset increases by the illegitimate `anotherTokenQuant`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-45)
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

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L93-99)
```java
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
