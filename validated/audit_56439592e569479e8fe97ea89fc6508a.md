## Finding

### Title
Exchange pool "price" derived from raw token-balance ratios uses imprecise floating-point validation, allowing value extraction via rounding on `ExchangeWithdrawActuator`/`ExchangeInjectActuator` - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The reported bug is that a protocol treats an AMM/liquidity-pool balance ratio as an authoritative "price" and lets that ratio (which can drift and be imprecise) gate real economic decisions. In java-tron's TRC10 `Exchange` (bancor-style pool) feature, the "price" of one token in terms of another is likewise derived purely from the two pool balances stored in `ExchangeCapsule`, and `ExchangeWithdrawActuator`/`ExchangeInjectActuator` compute the counter-party amount for a withdraw/inject from that ratio. Unlike the report's issue (price divergence from a real USD peg), the analogous java-tron weakness is that the *default* on-chain code path validates this ratio-derived amount using floating-point (`double`) arithmetic instead of exact arithmetic, which is a strictly weaker/imprecise re-derivation of the same "price" used in `execute()`.

### Finding Description
`ExchangeCapsule.getFirstTokenBalance()`/`getSecondTokenBalance()` are the sole source of "price" for a TRC10 exchange pool [1](#0-0) . In `ExchangeWithdrawActuator.execute()`, the amount of the counter-party token (`anotherTokenQuant`) returned to the creator is computed with exact `BigInteger` division (floor division) of the pool ratio [2](#0-1) .

However, `doValidate()` re-derives the *expected* `anotherTokenQuant` (via `divideToIntegralValue`, exact `BigDecimal`) and then performs a second precision-consistency check. When the governance-gated flag `allowHardenExchangeCalculation` is **not** enabled (the flag's default/pre-activation state, gated behind `ProposalUtil`/`DynamicPropertiesStore`, only flips to hardened `BigDecimal` math after being turned on chain-wide via `ProposalService`) [3](#0-2) , the validation instead converts the same ratio to `double` and computes the remainder with floating-point division and subtraction: [4](#0-3) [5](#0-4) 

This `double`-based remainder check is an imprecise proxy for the true pool exchange rate (analogous to using an unreliable proxy for "USD price" in the referenced report). Because `double` cannot exactly represent all values produced by large `BigDecimal` pool-balance ratios, the floating-point remainder computed here can silently differ from the true `BigInteger`/`BigDecimal` remainder actually used to move funds in `execute()` [2](#0-1) . A test explicitly demonstrates that the hardened (`BigDecimal`) path rejects certain inputs as "Not precise enough" that would otherwise pass under the un-hardened `double` path [6](#0-5) , confirming the two code paths compute meaningfully different acceptance boundaries for the same pool-ratio "price."

### Impact Explanation
Because the pool balances act as the "price oracle" for withdraw/inject and validation of that price is done with imprecise floating point by default, a creator-controlled account interacting with `ExchangeWithdrawContract` can potentially find `tokenQuant` values where the floating-point precision check underestimates the true rounding error, causing the exchange to release counter-party token amounts (`anotherTokenQuant`) that are more favorable than the true pool ratio dictates. Repeated exploitation drains the pool balances (and hence the TRC10 assets/TRX backing them) beyond what the true ratio would allow, i.e. unbacked extraction of value from the exchange pool — directly analogous to the reported "incorrect pricing" leading to unintended mint/burn behavior.

### Likelihood Explanation
The path is reachable by any account that created a TRC10 `Exchange` (only the exchange creator can call `ExchangeWithdrawContract`, per `doValidate()`'s creator check) [7](#0-6) , and only when `allowHardenExchangeCalculation` has not yet been activated by chain governance. Once activated network-wide, the `BigDecimal` hardened path is used and this specific imprecision is closed off. Exploitability further requires crafting specific balance/quant combinations where `double` rounding diverges from exact rounding within the 0.0001 relative tolerance — a non-trivial but automatable search given the pool balances are public on-chain state.

### Recommendation
Always require the exact `BigDecimal`/`BigInteger` precision check (the "hardened" path) rather than gating it behind an opt-in proposal, or remove the `double`-based branch entirely so both `ExchangeInjectActuator` and `ExchangeWithdrawActuator` validate against the same exact arithmetic used in `execute()`.

### Proof of Concept
Not independently reproduced with a concrete numeric input in this analysis; the concrete divergence is demonstrated by the repository's own test `hardenedPrecisionCheckFailsWhenImprecise`, which shows that withdrawing `9991` units of a 100M/200M pool passes the un-hardened `double` check differently from the hardened `BigDecimal` check [6](#0-5) , confirming the two validation paths for the same pool-ratio "price" are not equivalent.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L106-112)
```java
  public long getFirstTokenBalance() {
    return this.exchange.getFirstTokenBalance();
  }

  public long getSecondTokenBalance() {
    return this.exchange.getSecondTokenBalance();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L236-243)
```java
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L264-271)
```java
      } else {
        double remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeWithdrawActuatorTest.java (L1834-1866)
```java
  /**
   * Hardened mode: BigDecimal precision-loss check rejects imprecise input.
   */
  @Test
  public void hardenedPrecisionCheckFailsWhenImprecise() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();
    long exchangeId = 1;
    // Pool 100M/200M; withdrawing 9991 of "456" produces non-integer ratio
    String secondTokenId = "456";
    long quant = 9991L;

    ExchangeWithdrawActuator actuator = new ExchangeWithdrawActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, exchangeId, secondTokenId, quant));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.fail("Should fail with Not precise enough");
    } catch (ContractValidateException e) {
      Assert.assertEquals("Not precise enough", e.getMessage());
    } catch (Exception e) {
      Assert.fail("Unexpected exception: " + e.getMessage());
    } finally {
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    }
  }
```
