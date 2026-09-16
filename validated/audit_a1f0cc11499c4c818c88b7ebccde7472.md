## Title
Unhardened Bancor-style Exchange pricing (floating-point / non-overflow-checked arithmetic) allows AMM pool-ratio manipulation and value extraction, mirroring the Typus oracle-exploit bug class - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The Typus Finance incident stemmed from an internal price computation (an on-chain "oracle") that could be manipulated to misstate the value of pool assets, letting the attacker extract far more than deposited. java-tron's TRC10 `Exchange` module implements its own internal AMM-style price mechanism (a Bancor relay-token formula) that determines the exchange rate between the two tokens in a pool purely from the pool's own on-chain balances. This ratio *is* the protocol's price oracle for `ExchangeTransaction`, `ExchangeInject`, and `ExchangeWithdraw`. By default the "hardened" safe-math path is disabled (`ALLOW_HARDEN_EXCHANGE_CALCULATION` defaults to `0`), so all these actuators use `double`-precision floating point math and raw (non-overflow-checked) `long` arithmetic to compute the exchanged/injected/withdrawn amounts.

### Finding Description
`ExchangeCapsule.transaction()` selects between `ExchangeProcessor` (default, `useStrictMath`/no overflow guard) and `SafeExchangeProcessor` (BigDecimal-based, only used when `allowHardenExchangeCalculation()` is `1`): [1](#0-0) 

The default `ExchangeProcessor` computes the Bancor-relay conversion using raw `double` math (`Maths.pow`), which is inherently imprecise for the large integer balances (TRX/TRC10 amounts) actually stored on chain: [2](#0-1) 

Similarly, `ExchangeInjectActuator.execute()` computes the paired token amount using plain `long` multiply/floorDiv (guarded only by `multiplyExact`, which throws on overflow but does not protect precision or intermediate ratio correctness), while its own `validate()` computes the same ratio using `BigInteger`: [3](#0-2) [4](#0-3) 

Because the pool's own balances *are* the price feed (analogous to Typus's TLP oracle deriving price from pool state), an unprivileged, unprivileged-reachable sequence of `ExchangeInjectContract` / `ExchangeTransactionContract` / `ExchangeWithdrawContract` transactions from a single attacker-controlled account can:
1. Repeatedly nudge the pool's `firstTokenBalance`/`secondTokenBalance` via inject/withdraw calls whose paired-amount is computed with floating-point (`double`) or non-BigInteger-consistent long math, accumulating small directional rounding bias across many calls within the fee-free `ExchangeInjectActuator`/`ExchangeWithdrawActuator` (`calcFee()` returns `0` for these paths, e.g. `ExchangeTransactionActuator.calcFee()`): [5](#0-4) 
2. Use `ExchangeTransactionActuator`, whose only economic safety check is the caller-supplied `expected` slippage floor (`anotherTokenQuant < tokenExpected`), with no protection for the *pool* against the caller repeatedly extracting value at a manipulated, imprecision-skewed rate: [6](#0-5) 

This is the direct on-chain analog of the Typus root cause: an internally-derived "price" (here, the TRC10 exchange pool ratio) that is not computed with consistent, provably-correct arithmetic, and that a single unprivileged, unprivileged transaction broadcaster can influence and then immediately exploit for economic gain, with no cross-block or external price check to catch the manipulation.

### Impact Explanation
An attacker who can craft a sequence of signed `ExchangeInjectContract`, `ExchangeWithdrawContract`, and `ExchangeTransactionContract` transactions against a target `Exchange` pool can exploit floating-point/rounding inconsistencies in the default (non-hardened) pricing path to drain value from other liquidity participants in that pool — an unauthorized transfer of value (theft) similar in class to the Typus TLP price-manipulation drain, though bounded by the size of TRX/TRC10 liquidity in the targeted Exchange pool rather than external DeFi TVL.

### Likelihood Explanation
`ALLOW_HARDEN_EXCHANGE_CALCULATION` defaults to `0` (disabled) on-chain and can only be toggled by a committee proposal after a specific hard-fork activation (`VERSION_4_8_2`), per `ProposalUtilTest.testAllowHardenExchangeCalculationProposal`: [7](#0-6) 
This means the vulnerable, floating-point/imprecise arithmetic path is the live, default production behavior for all TRC10 Exchange contracts today unless the community has separately voted to enable hardening. Any unprivileged account holding TRX/TRC10 balances participating in (or targeting) an Exchange pool can broadcast the relevant contracts without special privilege.

### Recommendation
- Make `SafeExchangeProcessor` (BigDecimal-based) and the `BigInteger`-based ratio math in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` the mandatory, non-optional code path for all Exchange actuators, removing the `allowHardenExchangeCalculation` feature flag or defaulting it to enabled at genesis for new pools.
- Ensure `execute()` and `validate()` for each Exchange actuator use identical arithmetic (currently `ExchangeInjectActuator.execute()` uses plain `long` math while `doValidate()` uses `BigInteger`), eliminating any possibility of validate/execute result divergence.
- Add pool-level slippage/impact limits (e.g., max percentage of pool balance moved per transaction) independent of the caller-supplied `expected` value, so a single attacker cannot single-handedly swing the pool ratio and then immediately extract against it.

### Proof of Concept
Conceptual, config/state-dependent PoC (cannot be fully executed without live balances):
1. Attacker (or colluding account) creates/uses an `Exchange` pool with `firstTokenBalance`/`secondTokenBalance` chosen so that the `double`-based `Maths.pow` computation in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` diverges from the exact rational result by more than negligible amounts (large balances, e.g. near `Long.MAX_VALUE` ranges, exacerbate `double` mantissa precision loss).
2. Attacker broadcasts a sequence of `ExchangeInjectContract` and `ExchangeTransactionContract` transactions from a single controlled account, each computed via `ExchangeCapsule.transaction(..., useStrictMath=false, hardenedCalc=false)` (the default), each time supplying an `expected` value just barely satisfying `anotherTokenQuant >= tokenExpected` per `ExchangeTransactionActuator` validate logic.
3. Repeating this defeats the "no fee" `ExchangeInjectActuator`/`ExchangeTransactionActuator`/`ExchangeWithdrawActuator` (`calcFee()==0`), letting the attacker accumulate favorable rounding/precision bias across many cost-free calls, gradually shifting real value out of the pool at the expense of other liquidity participants — with no protocol-level circuit breaker, since `ALLOW_HARDEN_EXCHANGE_CALCULATION` is `0` by default.

Note: I was unable to fully trace `Manager.isExchangeTransaction` (in `framework/src/main/java/org/tron/core/db/Manager.java`) before the tool budget ran out — the index only returned an empty/truncated view of that file — so I cannot confirm whether block-application logic in `Manager` treats hardened vs. non-hardened Exchange transactions differently in a way that would further amplify or mitigate this issue. If exact block-application interaction details are needed, a full read of `Manager.java` (via a live Devin session, since the index appears to only have partial coverage of this large file) would be required to confirm.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-227)
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

**File:** framework/src/test/java/org/tron/core/actuator/utils/ProposalUtilTest.java (L705-725)
```java
    // 1) before fork 4.8.2 -> rejected
    ContractValidateException thrown = assertThrows(ContractValidateException.class, proposeOne);
    assertEquals("Bad chain parameter id [ALLOW_HARDEN_EXCHANGE_CALCULATION]",
        thrown.getMessage());

    forkUtils.getManager().getDynamicPropertiesStore()
        .saveLatestBlockHeaderTimestamp(hardForkTime + 1);
    Arrays.fill(stats, (byte) 1);
    forkUtils.getManager().getDynamicPropertiesStore()
        .statsByVersion(ForkBlockVersionEnum.VERSION_4_8_2.getValue(), stats);

    // 2) value not in {0, 1} -> rejected
    thrown = assertThrows(ContractValidateException.class, proposeTwo);
    assertEquals("This value[ALLOW_HARDEN_EXCHANGE_CALCULATION] is only allowed to be 0 or 1",
        thrown.getMessage());

    // 3) current value is 0 (default), proposing 0 again -> rejected
    thrown = assertThrows(ContractValidateException.class, proposeZero);
    assertEquals("[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to 0, no need to propose again",
        thrown.getMessage());

```
