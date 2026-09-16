### Title
Floating-point Bancor-curve arithmetic in the on-chain Exchange (`ExchangeTransactionContract`) allows precision-manipulation drain of exchange reserves - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The PancakeBunny incident exploited a reward-minting formula that derived value from an instantaneous, attacker-controllable pool ratio. The closest reachable analog in java-tron is the built-in Bancor-relay based token `Exchange` feature, whose default (non-hardened) pricing math is computed with Java `double` floating-point arithmetic rather than fixed-precision integer/BigDecimal math. Any unprivileged account can create and repeatedly trade against an `Exchange` pool via a signed `ExchangeTransactionContract`, and the resulting rounding behavior of the double-based curve is attacker-influenceable across a sequence of self-chosen trade sizes.

### Finding Description
`ExchangeCapsule.transaction()` selects between two `Processor` implementations depending on the `allowHarden` flag: the legacy `ExchangeProcessor`, which performs the Bancor "supply" conversion using `double` math and `Maths.pow`, and `SafeExchangeProcessor`, which uses `BigDecimal`. [1](#0-0) 

The legacy processor computes both legs of a trade (`exchangeToSupply` / `exchangeFromSupply`) using `double` precision and `(long) issuedSupply` truncation: [2](#0-1) 

This code path is reached directly from `ExchangeTransactionActuator.execute()`, which is invoked whenever any account broadcasts a signed `ExchangeTransactionContract` referencing an existing exchange id: [3](#0-2) 

Unlike `ExchangeInjectActuator`/`ExchangeWithdrawActuator`, which apply an explicit BigDecimal-based "Not precise enough" rounding-error bound before crediting/debiting the counter-token, `ExchangeTransactionActuator.doValidate()` only checks that the computed `anotherTokenQuant` meets the caller-supplied `expected` minimum — it applies no independent bound on rounding drift introduced by the double-precision curve itself: [4](#0-3) 

The hardened, BigDecimal-based path (`SafeExchangeProcessor`) exists in the codebase but is only used when `allowHarden()`/`allowHardenExchangeCalculation` is enabled via a chain-wide committee proposal — i.e., it is opt-in and not guaranteed active on any given deployment. [5](#0-4) 

Because an attacker fully controls the sequence, direction, and size of their own trades against a pool they can also seed via `ExchangeCreateContract`/`ExchangeInjectContract`, and because the pricing state (`firstTokenBalance`/`secondTokenBalance`) is mutated and re-read on every trade within the same processor instance, an attacker can choose trade sizes that maximize the cumulative double-precision truncation error in their favor across many self-submitted transactions, extracting more value than the true Bancor curve would allow — conceptually the same class of bug as PancakeBunny's manipulable ratio-based minting, but expressed as floating-point rounding abuse rather than deposit-timing abuse.

### Impact Explanation
If exploitable at scale, an attacker could repeatedly extract token value from a shared `Exchange` pool beyond what the intended constant-product/Bancor curve permits, at the expense of other liquidity participants — a theft-of-funds outcome reachable purely through ordinary signed transactions (`ExchangeCreateContract` + `ExchangeTransactionContract`), matching the required "unauthorized... theft... of funds" impact bar.

### Likelihood Explanation
Likelihood depends on how large the exploitable rounding-error margin actually is per trade and whether it exceeds gas/bandwidth cost of the attack across many iterations — this was not independently quantified in my review. The mitigation (`SafeExchangeProcessor`) already exists in the codebase, which suggests the maintainers are aware precision issues in the legacy double-based curve are a real concern, but it is proposal-gated rather than default-on, so unpatched deployments remain exposed until the corresponding committee proposal is activated.

### Recommendation
- Confirm whether `allowHardenExchangeCalculation` is enabled by default on the target network; if not, activate it via committee proposal or make `SafeExchangeProcessor` the unconditional default.
- Add an explicit BigDecimal-based rounding-error bound check to `ExchangeTransactionActuator.doValidate()` (mirroring the "Not precise enough" guard already present in `ExchangeWithdrawActuator`/`ExchangeInjectActuator`) so double-precision drift cannot be leveraged for value extraction regardless of the harden flag.
- Audit `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` for worst-case rounding bias across the full range of realistic pool sizes and trade quantities.

### Proof of Concept
Not independently constructed/verified — I was not able to quantify a concrete profitable rounding-error sequence within the scope of this review (would require simulating `ExchangeProcessor.exchange()` across chosen trade sizes/pool ratios to demonstrate net attacker profit exceeding fees/bandwidth cost). This is flagged as a plausible bug-class analog based on code structure and default configuration rather than a confirmed, quantified exploit.

---
**Caveat on confidence:** I could not fully verify (a) the genesis/default value of the `allowHardenExchangeCalculation` proposal on the target chain, or (b) whether the magnitude of double-precision rounding error is large enough to be profitably exploitable after transaction fees. These would need to be confirmed with a live simulation before treating this as a confirmed, actionable vulnerability rather than a reachable-but-unquantified bug class.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L119-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath)
      throws ContractValidateException {
    return transaction(sellTokenID, sellTokenQuant, useStrictMath, false);
  }

  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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
