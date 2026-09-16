## Analysis

The Radiant report describes a class of bug where reward/asset accounting is updated to reflect a value that the underlying reserve cannot actually back, because the code fails to verify/enforce sufficiency before crediting the accounting state. The closest reachable analog in java-tron is in the on-chain **TRX/TRC10 Exchange (Bancor-style AMM)** order-execution path.

`ExchangeCapsule.transaction()` computes a trade via the constant-relay-supply formula and updates the pool's persisted balances, but the "resulting balance must not be negative" invariant is only enforced when the `hardenedCalc` (`allowHarden`) code path is used: [1](#0-0) 

Specifically: [2](#0-1) 

When `hardenedCalc` is `false` (legacy `ExchangeProcessor`, selected when the `allowHarden` chain parameter/proposal is not active), the negative-balance check is skipped entirely, and `newFirstTokenBalance`/`newSecondTokenBalance` are persisted unconditionally: [3](#0-2) 

This is invoked directly from `ExchangeTransactionActuator.execute()`, which is reachable by any account broadcasting a signed `ExchangeTransactionContract` (an ordinary order-placer transaction): [4](#0-3) 

The legacy `ExchangeProcessor.exchange()` computes `buyTokenQuant` using floating-point `Math.pow` on a synthetic "relay supply", which is a lossy, non-conservative model — unlike a true constant-product AMM, it does not intrinsically guarantee `buyTokenQuant <= buyTokenBalance`: [5](#0-4) 

`ExchangeTransactionActuator.doValidate()` only checks that the *trader's own* balance is sufficient and that the pool balance stays under an upper `balanceLimit`; it never checks that the computed `buyTokenQuant` for the counter-token is `<=` the exchange's actual counter-token reserve: [6](#0-5) 

### Title
Exchange pool balance can go negative (unbacked payout) when `allowHarden` is inactive — (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java)

### Summary
`ExchangeCapsule.transaction()` only rejects a trade that would drive either token reserve negative when the hardened math path (`allowHarden` on-chain switch) is active. On networks/heights where that proposal has not been activated, the legacy floating-point relay-supply calculation (`ExchangeProcessor`) is used and its result is persisted with no post-condition check, mirroring the "reserve empty but the accounting still records value" pattern in the reported bug.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())`. Inside `transaction()`, the invariant check `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` is gated by `hardenedCalc &&`, meaning it is completely skipped in the legacy branch. The legacy `ExchangeProcessor` uses `Math.pow` on a synthetic internal "supply" variable to derive `buyTokenQuant`; this is a heuristic approximation, not an exact conservation-guaranteeing formula, so for edge-case inputs (e.g., extreme `sellTokenQuant` relative to the pool's balances, or repeated trades that erode the counter-token balance to a very small value) it can return a `buyTokenQuant` that exceeds the exchange's actual counter-token balance. The actuator credits the trader with this `buyTokenQuant` via `addAssetAmountV2`/`setBalance` and persists the (now negative) exchange reserve without any revert, exactly as the reported bug credits a claimant with rewards that the reserve doesn't actually hold.

### Impact Explanation
A negative persisted token reserve in the `Exchange`/`ExchangeV2` capsule means the pool's recorded balance no longer matches the sum of tokens actually available to be withdrawn/traded by other participants (an unbacked-balance / insolvency condition, analogous to `MultiFeeDistribution` recording vested RDNT it never received). Any subsequent legitimate `ExchangeTransactionContract` or `ExchangeWithdrawContract` request drawing on that exhausted reserve either fails unexpectedly (denial of service for other traders) or, given the AMM math is now operating on a corrupted (negative) balance, could yield further miscalculated payouts, compounding loss of funds for other exchange participants.

### Likelihood Explanation
Reachable directly by any unprivileged account broadcasting an `ExchangeTransactionContract` — no special privilege is required. The condition is gated on `allowHarden()` not yet being enabled for the chain/network in question; on any deployment where the hardening proposal has not been activated (or historically, prior to its activation on mainnet), this legacy, unchecked path is live for every trade.

### Recommendation
Remove the `hardenedCalc &&` gate so the non-negative reserve invariant in `ExchangeCapsule.transaction()` is enforced unconditionally, or replace `ExchangeProcessor`'s floating-point relay-supply computation with the same conservative/exact math used by `SafeExchangeProcessor` regardless of the `allowHarden` switch.

### Proof of Concept
1. Create/inspect an `Exchange` pair whose counter-token (`buyTokenBalance`) reserve is very small relative to the sell side, or repeatedly trade to erode it, while `allowHarden` is not active for the network.
2. Broadcast an `ExchangeTransactionContract` with a `sellTokenQuant` chosen so that `ExchangeProcessor.exchange()`'s floating-point relay-supply math yields a `buyTokenQuant` greater than the current `buyTokenBalance`.
3. `ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(...)` with `hardenedCalc = false`; the `newSecondTokenBalance < 0` (or first) check at [7](#0-6)  is skipped, and the negative balance is written to the store while the trader's account is credited via `addAssetAmountV2`/`setBalance` in the actuator.
4. Confirm the exchange capsule's persisted `second_token_balance` (or `first_token_balance`) is negative, and that other participants can no longer complete legitimate trades/withdrawals against the drained reserve.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-91)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L194-215)
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
