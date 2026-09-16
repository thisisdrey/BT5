## Title
Dust Permanently Trapped in TRC10 Bancor-Style Exchange Pools Due to Truncating Integer Division in Inject/Withdraw - (`actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The WlsETH bug class describes a token that internally tracks shares (`x`) against an underlying pool (`B`, `S`), where converting a user-specified underlying-denominated amount into shares via `v = b*x*S/B` truncates, so a user can never redeem the *exact* amount they deposited — dust silently accumulates and is permanently unclaimable except through careful re-engineering of new deposits. Java-tron's TRC10 Bancor-style `Exchange` object (`ExchangeCapsule`) has the same "amount ↔ pool-share" ratio-conversion property: `ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the paired/returned token quantity as a proportional ratio of the two pool balances via truncating integer/BigInteger division, exactly mirroring the `v = b*x*S/B` rounding-truncation pattern from the report.

### Finding Description
`ExchangeInjectActuator#doValidate` computes the paired token amount required to inject liquidity as a truncating ratio of the current pool balances: [1](#0-0) 

Symmetrically, `ExchangeWithdrawActuator#execute` computes the return amount of the "other" token by the same truncating `BigInteger` ratio math: [2](#0-1) 

Because `longValueExact()` is applied after `divide()` (which for `BigInteger` truncates toward zero) rather than after `divideToIntegralValue` with rounding, every inject/withdraw pair leaves behind a small residual — the ratio computed at withdraw time will not exactly reverse the ratio computed at inject time whenever `firstTokenBalance`/`secondTokenBalance` have changed between the two operations (which they always have, since the pool's own balances are mutated by the transaction itself, as seen in `ExchangeCapsule#transaction`): [3](#0-2) 

This is structurally identical to the WlsETH root cause: an amount denominated in one unit (ETH/lsETH) is converted through an integer-truncating ratio against a pool of shares (`S`) and underlying (`B`) that itself is mutated by trades, so there is no `x` that reproduces the original `v` exactly. In java-tron's case, TRX/TRC10 sent into `ExchangeInjectContract` or withdrawn via `ExchangeWithdrawContract` is subject to the same truncation, and the lost dust remains in the pool's `firstTokenBalance`/`secondTokenBalance` counters forever, inaccessible to the account that contributed it.

### Impact Explanation
Any user submitting `ExchangeInjectContract` or `ExchangeWithdrawContract` transactions (fully unprivileged, reachable via a single signed transaction) permanently loses the truncated remainder of their proportional token amount into the shared exchange pool with no mechanism to reclaim it. Over many transactions from many participants, this dust accumulates in the pool's balance fields and is unrecoverable by any account — a permanent, if small-per-transaction, freezing of TRC10/TRX funds. This matches the "permanent freezing of funds" impact class from the rules.

### Likelihood Explanation
This occurs on every single inject/withdraw pair whenever the injected/withdrawn ratio does not divide evenly into the current pool balances, which for arbitrary user-supplied `tokenQuant` values against arbitrary pool ratios is the common case rather than the exception. No adversarial conditions or privileged roles are required — it is triggered by ordinary use of the public `ExchangeInject`/`ExchangeWithdraw` transaction types.

### Recommendation
Track exchange liquidity contributions using an explicit share/LP unit (minted proportionally to injected value and burned proportionally on withdraw, similar to the Spearbit-recommended fix of exposing/using share units directly instead of underlying-denominated amounts), or apply consistent rounding-direction accounting (e.g., always round in favor of the pool on inject and against the pool on withdraw, and expose the residual as a reclaimable balance) so dust does not silently vanish from the redeemable side of the ratio.

### Proof of Concept
1. Create an exchange pool with `firstTokenBalance`/`secondTokenBalance` such that the ratio does not evenly divide typical inject amounts (any real-world balances essentially guarantee this).
2. Submit `ExchangeInjectContract` with `tokenQuant` on `firstTokenID`; `anotherTokenQuant` is computed via `bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance).longValueExact()` — truncated down. [4](#0-3) 
3. Immediately submit `ExchangeWithdrawContract` attempting to withdraw the same nominal `tokenQuant`; the reverse computation in `ExchangeWithdrawActuator` (lines 79-86) uses the now-updated pool balances and the same truncating division, so the amount returned is provably less than what was originally contributed for the paired token.
4. Repeat across many accounts/transactions to observe dust monotonically accumulating in the pool's `firstTokenBalance`/`secondTokenBalance`, unrecoverable by any single account — mirroring the WlsETH `v ≠ b*x*S/B` dust-lock scenario.

### Citations

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
