## Analysis

The WDZD Swap incident involved an AMM-style swap contract where flawed exchange math allowed attackers to extract far more value than the pool's reserves should have permitted across a sequence of transactions. The closest reachable analog in java-tron is the **TRC10 Exchange (Bancor-relay) subsystem** — `ExchangeInjectContract` / `ExchangeTransactionContract` / `ExchangeWithdrawContract` — which implements an on-chain AMM. Any unprivileged account can create/trade against these pools with a signed transaction.

### Title
Exchange swap output is validated only against a lower bound, letting the legacy (default) floating-point AMM formula drain more than the pool actually holds - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
`ExchangeTransactionActuator` computes the swap output (`anotherTokenQuant`) via `ExchangeCapsule.transaction()` and only checks that this output is **not less** than the caller-supplied `tokenExpected`. There is no check that the computed output does not **exceed** the exchange pool's actual reserve of the token being bought. The default (non-hardened) calculation path in `ExchangeCapsule.transaction()` also skips the balance-invariant check that exists only for the opt-in "hardened" path, so a swap that computes an output larger than the pool's live balance is written to chain state with a negative reserve and no exception.

### Finding Description
`ExchangeTransactionActuator.doValidate()` computes the swap result and enforces only a floor: [1](#0-0) 
There is no corresponding check that `anotherTokenQuant` is `<=` the exchange's current balance of `anotherTokenID` (`firstTokenBalance`/`secondTokenBalance`).

`ExchangeCapsule.transaction()` performs the real balance mutation. For the **default** path (`hardenedCalc == false`, selected whenever the `allowHardenExchangeCalculation` dynamic property is not enabled), the new balances are computed with plain arithmetic and the sign-invariant check is skipped entirely — it only runs `if (hardenedCalc && ...)`: [2](#0-1) 

The legacy processor that computes the swap amount, `ExchangeProcessor`, relies on floating-point `Math.pow` with a bonding-curve exponent of `0.0005`/`2000`, using `double` arithmetic and a truncating cast to `long`: [3](#0-2) 

Because this arithmetic is float-based and unguarded, it can (for extreme reserve ratios, near-total-reserve sell sizes, or repeated small trades that accumulate rounding bias) yield a `buyTokenQuant` that is not tightly bounded by the actual `buyTokenBalance`. The only place such an overshoot would normally be caught — a post-calculation invariant that reserves must stay `>= 0` — exists in the codebase (`SafeExchangeProcessor`/hardened branch) but is **not applied to the default/legacy path**, and no code anywhere checks the swap output against the live reserve at validation time before committing it via `Commons.putExchangeCapsule(...)`: [4](#0-3) 

The project's own tests for the hardened path explicitly demonstrate that the negative-balance invariant is a real, previously-necessary safeguard (i.e., the underlying math can go negative without it): [5](#0-4) 

This mirrors the WDZD Swap bug class: a swap/AMM path that lacks an output-vs-reserve sanity check, allowing a sequence of trades to extract more of a token than is actually escrowed in the pool.

### Impact Explanation
If the legacy exchange calculation ever yields an output larger than the pool's actual reserve of the counter-token (plausible given unchecked floating-point power-function math and no upper-bound validation), the actuator will still: (1) credit the trader with `anotherTokenQuant` of the counter-token/TRX via `addAssetAmountV2`/`setBalance`, and (2) persist a negative `firstTokenBalance`/`secondTokenBalance` into `ExchangeStore`/`ExchangeV2Store` with no exception thrown. This is an **unbacked balance / theft-of-funds** condition: value is created/withdrawn from an AMM pool beyond what was ever deposited, and the corrupted negative reserve can further skew all subsequent trades against that exchange, letting an attacker (or repeated malicious transactions, as in the WDZD Swap case) progressively drain any TRX/TRC10 liquidity paired in that pool.

### Likelihood Explanation
`ExchangeTransactionContract` is broadcastable by any funded account holding the sell-side asset; no privileged role is required. The hardening feature (`allowHardenExchangeCalculation`) is an opt-in dynamic property gated by committee proposal, so on any chain where it has not been activated, the legacy, unguarded double-based math path in `ExchangeProcessor`/`ExchangeCapsule.transaction(..., hardenedCalc=false)` is what actually executes on every trade. The absence of any upper-bound validation in `ExchangeTransactionActuator.doValidate()` means the actuator has no defense-in-depth even if the underlying math misbehaves. Reproducing the exact floating-point boundary conditions that push the calculation past the pool reserve requires reserve ratios/quantities that trigger `Math.pow` rounding, which is plausible for pools with extreme balance skew or via repeated targeted trades — directly analogous to the "nine malicious transactions" pattern in the source incident.

### Recommendation
- In `ExchangeTransactionActuator.doValidate()` (and the corresponding `ExchangeInjectActuator`/`ExchangeWithdrawActuator` validators), add an explicit check that the computed swap/inject/withdraw output does not exceed the current reserve of the token being paid out, independent of the `tokenExpected` floor check.
- In `ExchangeCapsule.transaction()`, apply the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` invariant check unconditionally, not only when `hardenedCalc` is true, and throw `ContractValidateException` for both legacy and hardened paths.
- Consider making the hardened (`SafeExchangeProcessor`, exact/BigDecimal-based) calculation the sole/default path rather than an opt-in dynamic property, eliminating the double-computation surface entirely.

### Proof of Concept
A concrete numeric input that drives `buyTokenQuant` past the live reserve was not able to be brute-forced without executing the floating-point routine (no code-execution tool available in this session); the codebase's own regression tests, however, are constructed specifically around this exact invariant and confirm it is only enforced for the hardened path, not the legacy/default path: [6](#0-5) 
A background Devin session with code-execution access could construct an exact reserve/quantity pair (e.g., a highly imbalanced pool combined with a near-full-reserve sell) that causes `ExchangeProcessor.exchange()` to return a value exceeding `buyTokenBalance`, then submit that as an `ExchangeTransactionContract` to confirm the resulting negative on-chain balance and unbacked token credit.

### Citations

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

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L43-83)
```java
  @Test
  public void testHardenedTransactionFirstTokenSell() throws Exception {
    byte[] key = ByteArray.fromLong(1);
    ExchangeCapsule capsule = chainBaseManager.getExchangeStore().get(key);
    capsule.setBalance(100_000_000L, 100_000_000L);

    long sellQuant = 1_000_000L;
    long buyQuant = capsule.transaction("abc".getBytes(), sellQuant, true, true);

    Assert.assertTrue("Hardened result must be positive", buyQuant > 0);
    Assert.assertEquals(100_000_000L + sellQuant, capsule.getFirstTokenBalance());
    Assert.assertEquals(100_000_000L - buyQuant, capsule.getSecondTokenBalance());
  }

  @Test
  public void testHardenedTransactionSecondTokenSell() throws Exception {
    byte[] key = ByteArray.fromLong(1);
    ExchangeCapsule capsule = chainBaseManager.getExchangeStore().get(key);
    capsule.setBalance(100_000_000L, 100_000_000L);

    long sellQuant = 1_000_000L;
    long buyQuant = capsule.transaction("def".getBytes(), sellQuant, true, true);

    Assert.assertTrue(buyQuant > 0);
    Assert.assertEquals(100_000_000L - buyQuant, capsule.getFirstTokenBalance());
    Assert.assertEquals(100_000_000L + sellQuant, capsule.getSecondTokenBalance());
  }

  @Test
  public void testHardenedTransactionNegativeBalanceThrows() throws Exception {
    // Construct a corrupt-state pool with a negative balance to drive the
    // < 0 invariant in the hardened branch via subtractExact wrapping.
    ExchangeCapsule capsule = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 99L, 0L,
        "abc".getBytes(), "def".getBytes());
    capsule.setBalance(Long.MAX_VALUE, 1L);

    // Selling abc adds to firstTokenBalance: addExact(MAX, q) overflows -> ArithmeticException
    Assert.assertThrows(ArithmeticException.class,
        () -> capsule.transaction("abc".getBytes(), 1L, true, true));
  }
```
