### Title
Legacy Exchange Balance Calculation Lacks Negative-Balance Guard, Enabling Unbacked Balance/Fund Drain via `ExchangeTransactionContract` - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeCapsule.transaction()` only validates that resulting pool balances are non-negative when the node runs in the "hardened" calculation mode (`hardenedCalc == true`). When `hardenedCalc` is `false` (the default/legacy `ExchangeProcessor` floating-point path, controlled by the `allowHardenExchangeCalculation` dynamic property), no such invariant check exists, allowing a crafted, anonymous `ExchangeTransactionContract` to push an exchange pool's `firstTokenBalance`/`secondTokenBalance` negative or otherwise inconsistent with backing assets.

### Finding Description
`ExchangeCapsule.transaction()` computes `newFirstTokenBalance`/`newSecondTokenBalance` using either the legacy floating-point `ExchangeProcessor` (double-precision `Math.pow`) or, when hardened mode is enabled, `SafeExchangeProcessor`/`StrictMathWrapper`. The post-computation safety check is gated on `hardenedCalc`: [1](#0-0) 

```
newFirstTokenBalance = hardenedCalc ? StrictMathWrapper.addExact(...) : firstTokenBalance + sellTokenQuant;
...
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException("Exchange balance must be >=0 after transaction");
}
```

When `hardenedCalc` is `false` (default, since `allowHardenExchangeCalculation()` must be explicitly enabled — see `AbstractExchangeActuator.allowHarden()`), the legacy `ExchangeProcessor` performs the bancor-style calculation entirely in `double` arithmetic: [2](#0-1) 

Floating-point `Math.pow` computations with extreme ratios of `quant`/`balance` (e.g., very large `sellTokenQuant` relative to `sellTokenBalance`, up to `Long.MAX_VALUE`, since `tokenQuant` is only bounded by the caller's asset/TRX balance and `dynamicStore.getExchangeBalanceLimit()` on the *sell* side only, not on the *buy* side payout) can produce a `buyTokenQuant` that exceeds the actual `buyTokenBalance` in the pool. Because the negative-balance check is skipped entirely in the non-hardened path, `newSecondTokenBalance = secondTokenBalance - buyTokenQuant` (or vice versa) can go negative and is written back to the store unchecked: [3](#0-2) 

The caller, `ExchangeTransactionActuator`, is reachable directly from an unprivileged, anonymously signed transaction (any account holding the sell-side asset/TRX and enough balance to cover `calcFee()`), and both `doValidate()` and `execute()` call the same unguarded `transaction()` method: [4](#0-3) [5](#0-4) 

`execute()` then unconditionally credits the attacker's account with `anotherTokenQuant` via `addAssetAmountV2`/`setBalance`, crediting more of the "bought" asset than the exchange pool actually possesses, effectively minting an unbacked balance / draining the exchange counter-asset reserve (TRX or TRC10 token) that legitimately belongs to other exchange participants — directly analogous to a hot-wallet balance/accounting exploit of the kind described in the M2 incident report (unauthorized extraction of value from wallets/pools due to unchecked accounting logic).

### Impact Explanation
A successful exploit lets an attacker extract more of a token/TRX from an `Exchange`/`ExchangeV2` liquidity pool than is actually backed, at the expense of other holders of that exchange pair, and leaves the on-chain pool balance negative or economically inconsistent. This is a critical unbacked-balance/fund-theft class bug reachable purely by broadcasting a signed `ExchangeTransactionContract` from any funded account — no special permissions, SR/witness status, or off-chain trust required.

### Likelihood Explanation
Exploitability depends on whether `allowHardenExchangeCalculation` is disabled on the target network (the default/legacy state, based on it being an opt-in dynamic property guarded by `AbstractExchangeActuator.allowHarden()`), and on the attacker being able to construct extreme sell/buy ratios against an existing exchange pool with sufficiently small counter-liquidity. Given TRC10 exchanges are user/SR-creatable and their pool sizes are attacker-influenced via `ExchangeCreateContract`/`ExchangeInjectContract`, an attacker can set up a thinly-liquid pool and then execute a large sell against it, producing the negative-balance condition. This is directly and repeatably triggerable by a single crafted transaction sequence.

### Recommendation
Remove the `hardenedCalc &&` gate in `ExchangeCapsule.transaction()` so the non-negative balance invariant (`newFirstTokenBalance < 0 || newSecondTokenBalance < 0` → throw `ContractValidateException`) is enforced unconditionally, regardless of whether the hardened/legacy processor is used. Additionally, validate in `ExchangeTransactionActuator.doValidate()` that the computed `anotherTokenQuant` does not exceed the current opposite-side pool balance before mutating state, for both hardened and legacy paths.

### Proof of Concept
1. Attacker (or attacker-controlled account) creates (or uses an existing) `Exchange` pair via `ExchangeCreateContract` with a very small balance on one side (e.g., `secondTokenBalance = 100`).
2. Ensure `allowHardenExchangeCalculation` is disabled (default state) so `ExchangeTransactionActuator` uses the legacy `ExchangeProcessor`.
3. Broadcast an `ExchangeTransactionContract` selling a very large quantity of `firstTokenId` (bounded only by the attacker's own asset/TRX balance and `getExchangeBalanceLimit()` on the sell side) against the thin `secondTokenBalance`.
4. The floating-point bancor calculation in `ExchangeProcessor.exchangeFromSupply` can return a `buyTokenQuant` exceeding `secondTokenBalance`; since `hardenedCalc` is `false`, `ExchangeCapsule.transaction()` skips the `>= 0` check and writes a negative `secondTokenBalance`, while `execute()` still credits the attacker the full computed `anotherTokenQuant` via `addAssetAmountV2`, resulting in the attacker withdrawing more value than the pool held.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

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
