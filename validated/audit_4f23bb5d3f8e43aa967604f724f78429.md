### Title
Zero-Fee Bancor-Curve TRC10 Exchange Swaps Enable Costless Front-Running/Sandwich Extraction - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java)

### Summary
The TRC10 `Exchange` (Bancor-curve AMM) swap path, reachable by any account via a single `ExchangeTransactionContract`, charges **zero protocol fee** on swaps. Because the swap price is derived purely from the pool's own on-chain balances (no external fee sink), an unprivileged transaction broadcaster who can order/front-run transactions in a block can extract value from any other trader's exchange swap at zero cost, exactly the "no fee to disincentivize round-trip price manipulation" root cause described in the external report (there charging no fee let an attacker profit risklessly from reordering around a price update).

### Finding Description
`ExchangeTransactionActuator.execute()` computes the counter-token amount using `exchangeCapsule.transaction(...)`, which delegates to `ExchangeProcessor.exchange()` — a Bancor-style constant-relay-supply curve computed solely from the exchange's own `firstTokenBalance`/`secondTokenBalance` state: [1](#0-0) [2](#0-1) [3](#0-2) 

Critically, `ExchangeTransactionActuator.calcFee()` returns `0`, meaning no protocol fee is ever levied on the swap itself: [4](#0-3) 

The only user protection is a caller-supplied minimum-output check (`tokenExpected`), which prevents the swapper from getting *worse* than they asked for, but does nothing to prevent a third party from moving the pool price against a pending swap and then reversing it for profit: [5](#0-4) 

Because block producers/relayers can observe and order pending `ExchangeTransactionContract` transactions before inclusion, an attacker can:
1. See a victim's pending swap on Exchange `E` (e.g., swapping token A for token B).
2. Insert their own swap immediately before the victim's, pushing the Bancor curve price of A relative to B in the victim's disadvantaged direction.
3. Let the victim's swap execute at the worse price.
4. Insert a second attacker swap immediately after to reverse their own position, capturing the price impact the victim paid — all with `calcFee() == 0`, so the attack has no cost floor (unlike fee-bearing AMMs where the round-trip fee must be recouped before profit is possible).

This mirrors the report's root cause precisely: a price-computing swap mechanism with no protocol fee that can be exploited purely through transaction ordering around another party's trade.

### Impact Explanation
Any account that creates or trades against a TRC10 `Exchange` pool is exposed to costless sandwich extraction by transaction orderers (block producers or anyone able to influence intra-block ordering), resulting in unauthorized transfer of value (theft) from ordinary swappers into the attacker's account, funded by the victim's degraded execution price. This is a direct value-extraction/theft-of-funds impact reachable from a single signed, unprivileged transaction.

### Likelihood Explanation
Exploitation requires only the ability to observe a pending `ExchangeTransactionContract` and to have transactions ordered before/after it within the same or adjacent blocks — a capability available to block producers (SRs) and to any party with mempool visibility and influence over ordering, and it costs the attacker nothing extra since `calcFee()` is zero. No special privilege, contract vulnerability, or oracle dependency is needed, only the pre-existing fee-free swap path.

### Recommendation
Introduce a non-zero protocol fee (or minimum spread) in `ExchangeTransactionActuator.calcFee()` proportional to swap size, similar to how `MarketSellAssetActuator.calcFee()` charges `dynamicStore.getMarketSellFee()`, so that round-trip sandwich attacks around the Bancor curve are unprofitable for small price deviations. Alternatively/additionally, bound per-transaction price impact or require a minimum holding period between opposing swaps from correlated senders.

### Proof of Concept
1. Attacker monitors the mempool/pending block for an `ExchangeTransactionContract` swapping token A → token B on exchange `E` via `ExchangeTransactionActuator`.
2. Attacker submits swap1: sell token B for token A on `E`, ordered immediately before the victim's transaction, shifting `firstTokenBalance`/`secondTokenBalance` (per `ExchangeCapsule.transaction`) to worsen the victim's effective price.
3. Victim's swap executes at the degraded price (still passes because `tokenExpected` slippage check in `doValidate()` at lines 217-221 is set loosely or the victim doesn't anticipate the sandwich).
4. Attacker submits swap2: sell token A back for token B, ordered immediately after the victim's transaction, restoring the pool state and capturing the spread as profit.
5. Because `calcFee()` returns `0` for this actuator, the attacker pays no protocol fee across the two swaps, making the extraction pure profit net of TRX bandwidth/energy costs only.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-150)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```
