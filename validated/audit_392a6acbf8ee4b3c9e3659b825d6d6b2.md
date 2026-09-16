Based on my research, the closest legitimate analog in java-tron to this report's bug class ("public mempool visibility of a deterministic pricing mechanism, enabling front-running that skews the reported/realized price and enables value extraction") is the on-chain Bancor-style TRC10 `Exchange`.

### Title
Bonding-Curve `ExchangeTransaction` Price is Fully Predictable Pre-Block and Front-Runnable via Public Mempool, Enabling Sandwich-Extraction of Value - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
Similar to the Tokemak report where a predictable, publicly-visible triggering event (a mempool-observable snapshot transaction) let an attacker front-run and skew the derived incentive price, java-tron's `Exchange` (TRC10 bancor-style AMM) computes swap output deterministically from on-chain reserves using a fixed constant-formula curve. Any pending `ExchangeTransactionContract` is visible in the node's mempool before inclusion, and the resulting price impact is fully computable off-chain by any observer, allowing a classic sandwich attack.

### Finding Description
The swap output for any `ExchangeTransactionContract` is computed purely as a deterministic function of the current on-chain reserves (`firstTokenBalance`/`secondTokenBalance`) via `ExchangeCapsule.transaction()` [1](#0-0) , which delegates to the constant-formula `ExchangeProcessor.exchange()` [2](#0-1) . Because this state (reserves) and the pricing formula are fully public, any address broadcasting a transaction can be observed in the mempool prior to block inclusion, and the exact price impact of that pending swap can be precomputed. An attacker can insert their own `ExchangeTransactionContract` immediately before the victim's transaction (shifting reserves unfavorably against the victim) and a second one immediately after (reverting the price and capturing the spread) — a sandwich attack — analogous to how the reported bug exploited a predictable, mempool-visible snapshot trigger to skew price feeds.

The only mitigation present is a user-supplied minimum-output guard, `tokenExpected`, checked once during `doValidate()`: `anotherTokenQuant < tokenExpected` throws `ContractValidateException` [3](#0-2) . This bound is set by the user client-side, generally to their locally-observed pre-trade price, and is only checked once per transaction — it constrains loss but does not neutralize the attack; an attacker can still extract value up to each victim's chosen slippage tolerance, and if the tolerance is generous (or default/unset by wallets), the extraction can be substantial.

### Impact Explanation
Successful sandwiching directly transfers value from ordinary swap issuers to the front-runner within the bounds of their configured slippage, causing real fund loss for users of the TRC10 exchange feature at every trade, which is an in-scope "exchange and market order handling" actuator reachable by any unprivileged transaction broadcaster.

### Likelihood Explanation
Any address can observe pending `ExchangeTransactionContract` transactions in the public mempool, has full knowledge of on-chain reserve state and the exact `ExchangeProcessor` formula, and can broadcast bracket transactions with higher fee/priority to be included immediately before and after the victim transaction — this requires no privileged role and is a common practice on similar constant-formula AMMs.

### Recommendation
Introduce mechanisms to reduce the effectiveness of this predictable-price front-running, such as: enforcing tighter mandatory maximum slippage bounds, per-block price-impact caps for a single exchange pair, a time-weighted/commit-reveal execution price instead of exact spot-reserve pricing, or batching same-block trades on a pair at a uniform clearing price so ordering within a block cannot be exploited.

### Proof of Concept
1. Observe the mempool for a pending `ExchangeTransactionContract` for exchange ID `X` selling token `A` for token `B` with quantity `Q` and `expected` minimum `E`.
2. Compute (using `ExchangeProcessor.exchange()` with the current on-chain reserves) the resulting price impact of this trade.
3. Broadcast a higher-fee `ExchangeTransactionContract` on the same pair, buying `B` with `A` first, shifting reserves so that the victim's swap receives an amount just above `E` (still valid per `doValidate()` check at line 219) but worse than the un-manipulated price.
4. After the victim's transaction executes, broadcast a second `ExchangeTransactionContract` reversing the initial trade, capturing the spread as profit — all three transactions ordered by miner/witness inclusion, exploitable purely from public mempool visibility. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
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
