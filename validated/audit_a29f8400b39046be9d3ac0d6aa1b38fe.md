### Title
Spot-Balance AMM Pricing in `ExchangeCapsule.transaction` Allows Same-Block Price Manipulation of TRC10 Exchange Pools - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
java-tron's built-in TRC10 "Bancor-style" Exchange feature (`ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeCapsule`) computes the exchange rate between two TRC10 tokens/TRX purely from the *current* `firstTokenBalance` / `secondTokenBalance` fields of the pool, exactly analogous to the `ERC4626.convertToAssets`/`previewRedeem` spot-ratio calculation flagged in the referenced Sherlock finding. Any unprivileged, signed transaction can shift this ratio (via `ExchangeTransactionContract`) and, because a single account can broadcast an unlimited sequence of such contracts inside one block (there is no same-block/same-tx cooldown or TWAP), the pool's spot price can be pushed arbitrarily within one block and reverted immediately afterward.

### Finding Description
The core pricing logic lives in `ExchangeCapsule.transaction`: [1](#0-0) 

`transaction()` reads `this.exchange.getFirstTokenBalance()` / `getSecondTokenBalance()` — the pool's live, single-block-mutable balances — and feeds them straight into `ExchangeProcessor.exchange` (or `SafeExchangeProcessor.exchange`), which derives the trade output purely from those two spot balances: [2](#0-1) 

This is executed on every trade via `ExchangeTransactionActuator.execute`, which is directly reachable from any account by broadcasting an `ExchangeTransactionContract`: [3](#0-2) 

There is no time-weighted averaging, no minimum holding period, and no restriction preventing the same account (or several colluding accounts) from executing multiple `ExchangeTransactionContract` / `ExchangeInjectContract` / `ExchangeWithdrawContract` operations against the same pool within a single block: each actuator independently re-reads the just-mutated `firstTokenBalance`/`secondTokenBalance` and recomputes the price from scratch, precisely the "spot total assets / spot supply, mutable within a single transaction/block" pattern described in the ERC4626Oracle report. `ExchangeInjectActuator` and `ExchangeWithdrawActuator` (restricted to the pool creator) also mutate balances directly using the same spot ratio: [4](#0-3) 

### Impact Explanation
Any component that treats the Exchange pool's spot balance ratio as a price reference (e.g., off-chain services, integrations, or future on-chain logic reading `ExchangeStore`/`ExchangeV2Store` balances to value TRC10 tokens) would inherit the same class of risk described in the report: an attacker can transiently inflate or deflate the reported exchange rate within one block via `ExchangeTransactionContract` trades, then reverse the trade in a following contract in the same block, leaving a manipulated snapshot price available to any observer or downstream consumer that samples it mid-block. Within the Exchange feature itself, large single-block sequences of trade/inject/withdraw operations against the same pool id can also produce economically unexpected slippage/rounding outcomes not bounded by any TWAP or per-block price-impact cap, unlike typical AMMs that rely on external price oracles with staleness protection. This is a design characteristic of the entire legacy Bancor-relay Exchange module rather than an isolated coding defect, so no direct fund-theft path independent of an external consumer of this price could be confirmed from the available code.

### Likelihood Explanation
Reaching this code path only requires broadcasting standard TRC10 `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` transactions, something any funded account can do without any special privilege, and multiple such transactions can be included in a single block by paying normal fees/bandwidth. However, whether this spot price is currently consumed anywhere on-chain as an "oracle" for collateral, liquidation, or other value-sensitive decisions could not be confirmed with the tools available — the only consumer identified is the Market order-book price computed in `MarketUtils`/`MarketComparator`, which uses order-book quantities rather than the Exchange pool's spot balances, so a genuine cross-component oracle-abuse impact analogous to the Sherlock report was not established with certainty.

### Recommendation
If the Exchange pool's spot ratio (`firstTokenBalance`/`secondTokenBalance`) is or will be consumed by any downstream valuation logic, it should not be read as an instantaneous price. Consider maintaining a time-weighted or block-delayed average of the pool ratio in `ExchangeCapsule`, or rate-limit price-moving operations (`ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`) per account per block, so a single block/transaction sequence cannot arbitrarily swing the reported exchange rate.

### Proof of Concept
Conceptual PoC (could not be executed with available read-only tools):
1. Attacker funds an account and identifies an `Exchange` pool id with token pair `(A, B)`.
2. Within one transaction/block, attacker submits `ExchangeTransactionContract` selling a large amount of `A` for `B`, driving `firstTokenBalance`/`secondTokenBalance` far from equilibrium (`ExchangeCapsule.transaction`, `ExchangeProcessor.exchange`).
3. Any consumer that reads the pool's live balances during this block observes a manipulated exchange rate.
4. In the same block, attacker submits the reverse `ExchangeTransactionContract` selling `B` back for `A`, restoring the balances (minus the Bancor-relay fee), leaving no persistent trace beyond the transient manipulated observation.

Given the uncertainty about whether any current on-chain consumer treats this spot ratio as an oracle for value-sensitive decisions (the only identified reader, `MarketUtils`, uses a separate order-book price mechanism), confidence in a concrete "unbacked balance / theft / freezing of funds" impact per the validation criteria is limited — this should be treated as a design-pattern analog flag rather than a confirmed exploitable vulnerability.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L57-69)
```java
      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeTransactionContract.getExchangeId()));

      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-83)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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
