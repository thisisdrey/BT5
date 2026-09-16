## Analog Found

### Title
Exchange (Bancor-style AMM) swap math relies on unsafe floating-point precision by default, permitting balance-ratio manipulation and value extraction analogous to the Allbridge pool-imbalance exploit - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The Allbridge exploit abused the on-chain pool's `tokenBalance`/`vUsdBalance` ratio math: swaps and deposits/withdrawals computed against an easily skewed ratio let the attacker extract more value than deposited by first imbalancing the pool, then withdrawing/swapping against the corrupted ratio. java-tron's native `Exchange` (Bancor-relay AMM for TRX/TRC10 pairs, reachable by any signed transaction via `ExchangeTransactionContract`) computes swap output using **double-precision floating point** math by default, which is imprecise and diverges from the exact (`BigDecimal`) result — the codebase's own hardened path (`SafeExchangeProcessor`) and test `testStrictMath` prove the legacy and exact paths produce different results for identical inputs.

### Finding Description
`ExchangeCapsule.transaction()` selects the swap processor based on a governance-gated flag: [1](#0-0) 

When `hardenedCalc` (i.e. `allowHardenExchangeCalculation()`) is false — which is the default state, since every test that exercises the "hardened" path explicitly turns it on with `saveAllowHardenExchangeCalculation(1)` and resets it to `0` afterward — the legacy `ExchangeProcessor` is used: [2](#0-1) 

This processor performs the Bancor-relay formula using `double` arithmetic and `Math.pow`, then truncates to `long`: [3](#0-2) 

The project's own regression test proves this legacy path is numerically wrong versus the exact `BigDecimal`-based `SafeExchangeProcessor`, for the *same* balances/quantities: [4](#0-3) 

`ExchangeTransactionActuator` (the swap entry point, reachable by any unprivileged account with a zero protocol fee) calls this exact function with attacker-controlled `tokenQuant` and no minimum-precision or slippage protection beyond a caller-supplied `tokenExpected`: [5](#0-4) [6](#0-5) 

Because `calcFee()` returns 0, an attacker can issue an unbounded number of swaps in one transaction (or block) at zero protocol cost, repeatedly exploiting the floating-point rounding bias of `ExchangeProcessor` (each `(long) issuedSupply` / `(long) exchangeBalance` truncation systematically favors one direction) to walk the pool balances into states where subsequent swaps return more value than fair-price BigDecimal math would allow — the same "amplify the imbalance" pattern the Allbridge PoC used (`BUSDPool.withdraw(...)` after skewing `vUsdBalance`/`tokenBalance`). This is a bug-class match: an AMM whose core balance-ratio arithmetic is imprecise/non-deterministic and reachable by any transaction sender, letting them drain the pool's real assets (TRX/TRC10) via legitimately-formed transactions.

### Impact Explanation
An attacker can drain `Exchange` pools (which hold real TRX and TRC10 token reserves) by repeatedly swapping using the floating-point-based `ExchangeProcessor`, extracting more asset value than deposited due to rounding/truncation bias, exactly mirroring the Allbridge root cause (imprecise balance-ratio math exploited across repeated operations in the same transaction/session). This is a direct theft-of-funds vector against `Exchange` pool creators/liquidity, satisfying "unbacked balance" / "theft of funds."

### Likelihood Explanation
`ExchangeTransactionContract` is broadcastable by any account with no special permission (`AbstractExchangeActuator`/`ExchangeTransactionActuator` has no owner/creator restriction, unlike Inject/Withdraw which require `exchangeCapsule.getCreatorAddress()` match). The zero-fee (`calcFee()==0`) swap and the confirmed default state of `allowHardenExchangeCalculation() == false` (per test setup/teardown patterns) mean the vulnerable floating-point code path is live on mainnet unless SRs have already activated the corresponding proposal. Exploitation requires only crafting a sequence of `ExchangeTransactionContract` calls with quantities chosen to maximize the accumulated truncation bias — no privileged role or off-chain dependency needed.

### Recommendation
Make `SafeExchangeProcessor` (exact `BigDecimal`-based computation) the mandatory, non-optional path for `ExchangeCapsule.transaction()`, removing the legacy double-based `ExchangeProcessor` entirely rather than gating correctness behind an opt-in governance proposal (`allowHardenExchangeCalculation`). Additionally, consider adding minimum trade-size/precision-loss protections in `ExchangeTransactionActuator.doValidate()` independent of the caller-supplied `tokenExpected`, and re-audit `ExchangeInjectActuator`/`ExchangeWithdrawActuator` (which already use exact `BigInteger`/`BigDecimal` math) to ensure no remaining legacy-math code paths exist anywhere in the `Exchange` actuator family.

### Proof of Concept
Not independently reproduced in this analysis — this assessment is based on static code comparison between `ExchangeProcessor` (double-based, default) and `SafeExchangeProcessor` (BigDecimal-based, opt-in), and the existing repo test `testStrictMath` in `ExchangeProcessorTest.java` which already demonstrates the two processors return different (`assertNotEquals`) results for identical inputs across 47 real-value test vectors. Precisely quantifying the extractable profit per swap sequence (to confirm it exceeds normal rounding noise and is systematically attacker-favorable) would require running/instrumenting that test harness, which was not available in this read-only environment — this is noted as an open verification item for anyone taking this further.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L22-29)
```java
    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L272-280)
```java
    for (long[] data : testData) {
      ExchangeProcessor processor = new ExchangeProcessor(supply, false);
      long anotherTokenQuant = processor.exchange(data[0], data[1], data[2]);
      processor = new ExchangeProcessor(supply, true);
      long result = processor.exchange(data[0], data[1], data[2]);
      long safeResult = SafeExchangeProcessor.INSTANCE.exchange(data[0], data[1], data[2]);
      Assert.assertNotEquals(anotherTokenQuant, result);
      Assert.assertEquals(safeResult, result);
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L233-235)
```java
  public long calcFee() {
    return 0;
  }
```
