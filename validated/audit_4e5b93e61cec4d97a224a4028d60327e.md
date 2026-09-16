### Title
Exchange (Bancor-forked) AMM pools can be created and manipulated with attacker-controlled skewed ratios, enabling rounding-based value extraction from other traders/injectors - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java])

### Summary
The Radiant Capital exploit relied on a "known bug" pattern in Aave-forked lending markets: a freshly-created, near-empty market combined with an unguarded price/exchange-rate calculation let a single attacker manipulate the internal ratio and extract value once other users interacted with it. java-tron ships its own AMM-like feature, the on-chain `Exchange` (a Bancor-style relay pool for TRX/TRC10 pairs), reachable directly from a single signed transaction via `ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, and `ExchangeTransactionActuator`. Unlike Uniswap/Aave-style pools, `ExchangeCreateActuator` lets any account seed a brand-new pool with an arbitrary, self-chosen and arbitrarily skewed ratio (the only guard is `firstTokenBalance <= 0 || secondTokenBalance <= 0`), and subsequent state-changing operations use plain integer division (`floorDiv`) on that ratio.

### Finding Description
`ExchangeCreateActuator.doValidate()` only requires that both `firstTokenBalance` and `secondTokenBalance` be strictly greater than zero and below `dynamicStore.getExchangeBalanceLimit()`: [1](#0-0) 
There is no minimum-liquidity requirement and no check that the ratio between the two sides is economically sane — an attacker can create a pool with, e.g., `firstTokenBalance = 1` and `secondTokenBalance` equal to a huge amount of a TRC10 token they mint themselves via a prior `AssetIssueContract`.

Once created, `ExchangeInjectActuator.execute()` computes the counterpart amount to inject using a floor-division ratio derived directly from the (attacker-controlled) pool balances: [2](#0-1) 
while `doValidate()` performs the equivalent computation with `BigInteger` division and only rejects when the *computed* `anotherTokenQuant` is `<= 0`: [3](#0-2) 
Because the pool ratio is attacker-defined at creation time (subject only to `firstTokenBalance/secondTokenBalance > 0` and the balance limit), the ratio can be made extreme enough that integer truncation systematically favors one side. Every subsequent trade against the pool goes through `ExchangeCapsule.transaction()`, which re-derives amounts from the current pool balances via the Bancor relay math in `ExchangeProcessor`/`SafeExchangeProcessor`: [4](#0-3) [5](#0-4) 
This is architecturally the same bug-class root cause as Radiant Capital: a newly-launched, thinly-capitalized market whose exchange rate can be driven to an extreme by whoever creates/seeds it, after which normal rounding/division behavior in the trade/inject path can be exploited to skim value from later counterparties. Radiant's specific fix (mandating a non-trivial initial deposit before a market goes live) has no equivalent enforcement here — `ExchangeCreateActuator` accepts `firstTokenBalance = 1`.

### Impact Explanation
If exploitable end-to-end, an attacker could create a skewed Exchange pool, then repeatedly `ExchangeInject`/`ExchangeTransaction` against it (or induce other users/contracts to route TRX/TRC10 swaps through it) to have integer-truncation rounding consistently favor the attacker, resulting in unauthorized extraction of TRX or TRC10 token balances from counterparties — a concrete theft-of-funds impact reachable purely through standard, unprivileged `ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeTransactionContract` transactions.

### Likelihood Explanation
Likelihood is limited by two factors this analysis cannot fully resolve from static review alone: (1) `Exchange` pools are largely legacy/deprecated in favor of the newer order-book `Market*` actuators, reducing real-world usage and thus the pool of potential victims; and (2) exploitation requires a counterparty to voluntarily interact with the attacker's specific pool (inject into it or route a trade through it) rather than the pool being drained unilaterally, unlike Radiant's flash-loan self-contained attack. Without a demonstrated concrete rounding-direction proof (e.g., a specific ratio/quantity pair that provably transfers net value to the attacker across a sequence of inject/trade calls), this remains a plausible but unconfirmed analog.

### Recommendation
- Enforce a meaningful minimum initial liquidity (not just `> 0`) and/or a sane minimum ratio bound in `ExchangeCreateActuator.doValidate()` for both `firstTokenBalance` and `secondTokenBalance`.
- Audit `ExchangeInjectActuator.execute()`'s `floorDiv` computation against `doValidate()`'s `BigInteger` division for consistent, safe rounding direction (always round in favor of the pool, never the caller).
- Consider deprecating/disabling creation of new `Exchange` pools in favor of the order-book market if the Bancor-relay Exchange is no longer intended to be actively used, reducing the attack surface entirely.

### Proof of Concept
Conceptual sequence (requires confirmation of a concrete profitable ratio, which needs runtime verification, not asserted here as proven):
1. Attacker issues a TRC10 token with a very large supply (`AssetIssueContract`).
2. Attacker calls `ExchangeCreateContract` with `firstTokenId = TRX`, `firstTokenBalance = 1`, `secondTokenId = <own token>`, `secondTokenBalance = <near balanceLimit>` — accepted per [1](#0-0) .
3. Attacker (or an unsuspecting counterparty) calls `ExchangeInjectContract`/`ExchangeTransactionContract` against the pool; the `floorDiv`-based computation in `ExchangeInjectActuator.execute()` ( [2](#0-1) ) and the Bancor relay math in `ExchangeCapsule.transaction()` ( [4](#0-3) ) determine settlement amounts from the attacker-set skewed ratio, with rounding potentially compounding in the attacker's favor across repeated small operations.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L201-203)
```java
    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-231)
```java
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

    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }
```

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
