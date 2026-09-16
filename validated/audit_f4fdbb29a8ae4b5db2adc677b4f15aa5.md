### Title
Sandwich/MEV attacks against the TRC10 bancor-relay `Exchange` bonding curve enable risk-free extraction of user slippage - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
The `Exchange` module implements a bancor-relay style bonding curve (`ExchangeCapsule.transaction`) that any account can trade against via `ExchangeTransactionContract`/`ExchangeTransactionActuator`. Like the bonding-curve sale contract described in the external report, the only slippage protection is a single-sided `expected` parameter checked once at validation time, with no minimum holding period or same-block/same-account restriction between trades. This allows a bot to sandwich a victim's trade — buying ahead of it and selling immediately afterward in the same block — capturing the victim's entire price-impact surplus with no price risk, exactly as described in the referenced report.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, ...)` which invokes the bancor curve math in `ExchangeProcessor`/`SafeExchangeProcessor` (`exchangeToSupply`/`exchangeFromSupply`), moving the pool's `firstTokenBalance`/`secondTokenBalance` continuously with every trade [1](#0-0) . The only protection against adverse price movement is the `expected` field, validated once in `doValidate()`: `if (anotherTokenQuant < tokenExpected) throw new ContractValidateException(...)` [2](#0-1) . This is functionally identical to the `minimumPayout`/`msg.value` slippage guard in the sound-protocol bonding curve referenced in the report: it bounds the caller's own worst-case outcome, but does nothing to prevent a third party from moving the curve immediately before and after the caller's transaction within the same block.

Because Tron blocks batch many transactions and a block producer (or any actor able to influence transaction ordering within the pending pool prior to block packing) can sequence transactions as `attacker buy -> victim trade -> attacker sell`, an unprivileged transaction broadcaster can extract the full price-impact surplus a victim was willing to tolerate under their `expected` slippage setting, with zero price risk — the attacker's buy and sell occur atomically within the same block against a deterministic curve. There is no cooldown, no maximum trade-size-per-block, and no last-trade timestamp check comparable to the `samBurn()` `block.timestamp` freeze the report's remediation added, so the analogous 1-block-freeze mitigation is entirely absent from `ExchangeCapsule`/`ExchangeTransactionActuator`.

### Impact Explanation
Any TRC10 `Exchange` pool created via `ExchangeCreateActuator` is exposed. A sandwiching bot can systematically extract consumer surplus from every user trade that has to tolerate any slippage buffer, effectively taxing all traders on every trade and unbacked-profit-shifting value out of ordinary users' accounts and into the attacker's, at no risk to the attacker. This is a fund-theft-by-design condition satisfying the "unauthorized... theft of funds" bar, scoped to users of the `Exchange` bonding-curve feature.

### Likelihood Explanation
Any account can issue `ExchangeTransactionContract` transactions with no special privileges (single signed transaction, reachable via `Wallet`/gRPC `TransactionExtention` and HTTP `/wallet/exchangetransaction` endpoints). Because the curve state (`firstTokenBalance`/`secondTokenBalance`) and the pending/candidate transaction set are both observable off-chain, an attacker only needs the ability to have their buy and sell land in the same block, immediately surrounding a target trade — a standard MEV technique that does not require validator/committee collusion, only ordinary transaction submission timing. This makes the attack straightforward and repeatable against any active `Exchange` pool with liquidity and volume.

### Recommendation
Introduce a minimum holding/cooldown period (e.g., one block, mirroring the `block.timestamp` freeze applied to `samBurn()` in the referenced fix) between a buy and sell of the same token pair by the same account against the same `Exchange`, tracked via a per-account/per-exchange "last trade" timestamp or block number checked in `ExchangeTransactionActuator.doValidate()`. Alternatively, consider bounding per-block price impact on a given `Exchange` pool, or moving toward a batch-auction/uniform-clearing-price model for the AMM to remove the atomic sandwich primitive entirely.

### Proof of Concept
1. Attacker observes a pending `ExchangeTransactionContract` from a victim that will buy asset `B` from pool `P` (first token `A`, second token `B`) using a generous `expected` slippage bound.
2. Attacker submits, targeted to land in the same block immediately before the victim's transaction, a large buy of `B` (sell `A`) via `ExchangeTransactionActuator`, moving `firstTokenBalance`/`secondTokenBalance` unfavorably for the victim (`ExchangeCapsule.transaction`, `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java:64-69`).
3. The victim's transaction executes against the now-worse curve state, still passing its `expected` check because the victim padded slippage.
4. Attacker submits a third transaction, immediately after the victim's, selling `B` back for `A` against the curve, which has now moved favorably back toward equilibrium, realizing a risk-free profit equal to the victim's slippage buffer — with no cooldown or freeze in `ExchangeCapsule`/`ExchangeTransactionActuator` to prevent this sequence within one block.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
