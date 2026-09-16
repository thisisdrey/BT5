### Title
Unguarded floating-point exchange math allows draining/negative-balance corruption of on-chain Exchange (Bancor) pools - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java])

### Summary
`ExchangeTransactionActuator` lets any account with a signed `ExchangeTransactionContract` trade against a TRX/TRC10 Bancor-style liquidity pool held in `ExchangeCapsule`. The actual pool math is delegated to `ExchangeCapsule.transaction()`, which picks between two calculation engines based on the committee-controlled flag `allowHardenExchangeCalculation`: the legacy `ExchangeProcessor` (double-precision floating point) or the new `SafeExchangeProcessor` (BigDecimal + explicit non-negative invariant check). Only the hardened path enforces `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` as an error condition; the legacy, default-enabled path performs no such check anywhere in `execute()` or `validate()`. [1](#0-0) 

### Finding Description
`ExchangeCapsule.transaction()` computes `buyTokenQuant` via `Processor.exchange()` and then updates `firstTokenBalance`/`secondTokenBalance` with plain arithmetic in the non-hardened branch: [2](#0-1) 

The invariant check `if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0))` is only executed when `hardenedCalc` is true. When it is false — the default/legacy path exercised whenever the committee has not enabled `allowHardenExchangeCalculation` — the resulting balances are stored unconditionally, even if negative. [3](#0-2) 

The legacy `ExchangeProcessor` performs the Bancor-formula math with `double`/`Math.pow`: [4](#0-3) 

`ExchangeTransactionActuator.doValidate()` only checks that the pool's token IDs match, that `firstTokenBalance`/`secondTokenBalance` are non-zero, that the new *sold* token balance doesn't exceed `getExchangeBalanceLimit()`, and that `anotherTokenQuant >= tokenExpected` (a floor set by the caller, not a floor tied to real pool solvency): [5](#0-4) 

Because the caller fully controls `tokenID`, `tokenQuant`, and can probe `anotherTokenQuant` off-chain by re-running the (public) Bancor formula, an attacker can construct a sequence of trades against a shallow/imbalanced pool (e.g., one where the counter-token's balance is very small relative to the sell quantity) so that floating-point error in `exchangeToSupply`/`exchangeFromSupply` yields a `buyTokenQuant` that exceeds the true remaining `buyTokenBalance`. In the legacy path nothing stops the resulting `newSecondTokenBalance` (or `newFirstTokenBalance`) from going negative — the pool then reports a negative (unbacked) balance while the attacker's `AccountCapsule` is credited with real, transferable TRX/TRC10 tokens by `ExchangeTransactionActuator.execute()`: [6](#0-5) 

This is functionally the same bug class as the external report's root problem (a payment/exchange system whose accounting logic can be manipulated to move out more value than was legitimately deposited) — here it manifests in java-tron's on-chain Bancor exchange rather than an off-chain payment processor.

### Impact Explanation
A successful drain leaves `ExchangeCapsule.firstTokenBalance`/`secondTokenBalance` negative (an unbacked/impossible on-chain balance), while the attacker's account is credited with real asset balance obtained from `addAssetAmountV2`/`setBalance`. Because `ExchangeCapsule` is persisted via `Commons.putExchangeCapsule`, this corrupted state propagates permanently into consensus state, and other users interacting with the same exchange pool will have their own trades computed against the corrupted (negative) pool. This satisfies the "unbacked balance / theft of funds" bar for High/Critical severity.

### Likelihood Explanation
Reachable directly by any account holding a small amount of TRX/TRC10 tokens by broadcasting standard `ExchangeTransactionContract` transactions — no special privilege, SR/witness role, or off-chain component needed. The only gate is that `allowHardenExchangeCalculation` (a chain parameter toggled by committee proposal) must not yet be enabled; this is the default configuration and matches how other "allow*" flags in java-tron start disabled until a `ProposalService`/committee vote turns them on, so any network prior to that specific governance action is exposed. Exploitability further depends on finding pool states with an extreme balance skew, which is feasible because pools are user-created/injectable (`ExchangeCreateActuator`, `ExchangeInjectActuator`), letting an attacker set up the vulnerable ratio themselves.

### Recommendation
- Perform the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` check unconditionally in `ExchangeCapsule.transaction()`, not only when `hardenedCalc` is true, so the legacy path also rejects transactions that would corrupt the pool.
- Alternatively, make `SafeExchangeProcessor`/hardened math the sole, non-optional implementation for `ExchangeTransactionActuator` and `ExchangeWithdrawActuator`, removing the floating-point legacy path entirely rather than gating it behind a proposal flag.
- Add an explicit `ContractValidateException` in `doValidate()` if the computed `anotherTokenQuant` would exceed the current opposing-token balance, independent of the hardened flag.

### Proof of Concept
1. Attacker calls `ExchangeCreateActuator`/`ExchangeInjectActuator` (or targets an existing pool) to establish a pool with a very small `secondTokenBalance` relative to `firstTokenBalance` (allowed by `ExchangeCreateActuator`/`ExchangeInjectActuator`, subject only to `getExchangeBalanceLimit()`).
2. Attacker (with `allowHardenExchangeCalculation` unset — the default) submits an `ExchangeTransactionContract` selling `firstTokenId` with a `tokenQuant` chosen so that `exchangeToSupply`/`exchangeFromSupply`'s double-precision rounding computes a `buyTokenQuant` (`anotherTokenQuant`) that is greater than or equal to the pool's actual `secondTokenBalance`.
3. `ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(...)` with `hardenedCalc=false`; the resulting `newSecondTokenBalance = secondTokenBalance - buyTokenQuant` goes negative but is stored unchecked via `Commons.putExchangeCapsule`.
4. The attacker's account is credited `anotherTokenQuant` of `secondTokenId`/TRX via `accountCapsule.addAssetAmountV2`/`setBalance`, realizing value that was never backed by the pool, while the on-chain `Exchange` record now shows an impossible negative balance.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-167)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L86-93)
```java
      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L194-221)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
