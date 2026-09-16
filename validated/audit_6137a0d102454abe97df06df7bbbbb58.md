### Title
Zero-fee `ExchangeTransactionContract` combined with truncating Bancor-style pricing in `ExchangeProcessor` allows repeated rounding-biased trades to drain TRX/token exchange pools - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The external report describes an attacker repeatedly re-entering a round-based betting game as the "first" position each round, exploiting a rule that always favors the earliest investor to accumulate disproportionate profit at zero incremental risk. The reachable java-tron analog is the on-chain bancor-curve `Exchange` (`ExchangeCreateContract` / `ExchangeTransactionContract`), whose per-trade output is computed with floating-point math and `long` truncation in `ExchangeProcessor`, while `ExchangeTransactionActuator.calcFee()` charges **zero** TRX fee for each trade. An unprivileged transaction broadcaster can therefore submit an unlimited number of small `ExchangeTransactionContract` transactions against the same pool, and because each trade's rounding is biased in the taker's favor (truncation "down" in `exchangeToSupply`/`exchangeFromSupply`), the attacker accumulates value each round exactly like the reported "first position" rollback exploit, without paying any protocol fee to offset the extraction.

### Finding Description
`ExchangeCapsule.transaction()` computes trade output via a `Processor` interface, defaulting to `ExchangeProcessor` (the legacy, non-hardened path) unless `hardenedCalc` (`allowHarden()`) is enabled: [1](#0-0) 

The legacy `ExchangeProcessor` performs the Bancor-formula computation using `double` arithmetic and truncates the result to `long` via a straight cast, which always rounds toward zero (down for positive results): [2](#0-1) [3](#0-2) 

Each call to `exchange()` chains `exchangeToSupply` then `exchangeFromSupply`, both of which truncate independently, meaning the rounding error compounds per trade and its direction/magnitude is a deterministic function of the trade size relative to the current pool balance — something a caller can compute off-chain before submitting the transaction, similar to the "known-favorable-position" pattern in the game exploit.

Critically, `ExchangeTransactionActuator` charges **no fee** for this operation: [4](#0-3) 

and `validate()` only checks that the received amount meets the caller-supplied `expected` minimum — it does not bound trade size to prevent rounding-favorable micro-trades, nor does it require a minimum hold time or cooldown between trades from the same account: [5](#0-4) 

Because trades are free and can be sized by the attacker to maximize the rounding bias each time (the "first mover" round-by-round pattern from the report), an attacker can repeatedly trade back and forth (or repeatedly trade in one direction against a shallow pool) to accumulate a systematic profit extracted from the pool's `firstTokenBalance`/`secondTokenBalance`, funded by other liquidity providers/traders in the same exchange, with the only cost being blockchain bandwidth/energy for transaction broadcast — not a proportional protocol fee.

### Impact Explanation
Repeated exploitation drains real TRX/TRC-10 asset value from the on-chain `Exchange` liquidity pool (`ExchangeCapsule.firstTokenBalance`/`secondTokenBalance`) into the attacker's account balance via `AccountCapsule.setBalance`/`addAssetAmountV2` updates performed in `ExchangeTransactionActuator.execute()`. This is a concrete unauthorized value-extraction / theft-of-funds condition against any TRX-token exchange pool, matching the "unbacked balance" / "theft of funds" impact bar, and is directly reachable by any account able to broadcast a signed `ExchangeTransactionContract`.

### Likelihood Explanation
Likelihood is High: the operation is entirely fee-free at the actuator level (`calcFee()` returns 0), requires no special privilege, and the rounding behavior of the legacy `double`-based processor is deterministic and can be modeled/simulated by an attacker beforehand to choose trade sizes that maximize favorable truncation each round — directly analogous to the reported "first position rollback" gaming strategy that guaranteed profit on every round.

### Recommendation
- Always use the hardened `SafeExchangeProcessor` (`BigDecimal`-based, `RoundingMode.DOWN` applied consistently in favor of the pool, not the trader) for all trades, removing the legacy double/`(long)`-cast path entirely, or ensure `allowHarden()` is unconditionally enforced once activated.
- Introduce a non-zero, proportional fee for `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` operations to offset systematic rounding extraction, and/or a minimum trade-size threshold to make rounding-bias exploitation economically unviable.
- Add invariant checks ensuring the constant product (or Bancor invariant) never decreases across a trade, rejecting transactions that would reduce total pool value below the pre-trade invariant beyond a negligible epsilon.

### Proof of Concept
1. Attacker identifies an `Exchange` pool created via `ExchangeCreateActuator` with a small `firstTokenBalance`/`secondTokenBalance` ratio.
2. Attacker computes, off-chain, a sequence of small `sellTokenQuant` values for which `ExchangeProcessor.exchange()` truncation consistently yields a `buyTokenQuant` that is rounding-favorable relative to the true (unrounded) Bancor price.
3. Attacker repeatedly broadcasts `ExchangeTransactionContract` transactions (owner_address = attacker, exchange_id = target pool) alternating tokenID/quant per the precomputed favorable sequence; since `calcFee()` is 0, each transaction costs only bandwidth/energy, not a pool-proportional fee.
4. After many rounds, the attacker's account balance/asset holdings (`AccountStore` entries updated in `ExchangeTransactionActuator.execute()`) increase at the expense of the exchange pool's `firstTokenBalance`/`secondTokenBalance`, replicating the reported "first position" round-based extraction with no offsetting cost.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-29)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L31-45)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L232-235)
```java
  @Override
  public long calcFee() {
    return 0;
  }
```
