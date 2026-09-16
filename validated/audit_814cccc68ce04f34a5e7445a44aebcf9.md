### Title
Single-block Inject → Transaction → Withdraw sandwich lets a lone TRX/TRC10 account manipulate the on-chain Bancor Exchange price and drain counterparty value - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`, `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
The pSeudoEth incident was a classic flash-loan price-manipulation exploit: the attacker temporarily skewed a token pool's reserve ratio within a single transaction/block, traded at the manipulated price, then restored the reserves, extracting value with no lasting capital at risk. java-tron's native `Exchange*` contracts implement an on-chain Bancor-style AMM (`ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeTransactionContract`/`ExchangeWithdrawContract`) whose spot price is entirely a function of `firstTokenBalance`/`secondTokenBalance` stored in `ExchangeCapsule`. Any unprivileged account can broadcast an `Inject`, `Transaction`, and `Withdraw` for the *same* exchange pool as three ordinary signed transactions; because java-tron processes transactions sequentially within a block via `Manager`, all three can land in the same block, giving the attacker atomic-like control over the pool's state before/after each of its own trades — the same reserve-skew-then-revert pattern used in the pSeudoEth attack.

### Finding Description
`ExchangeCapsule.transaction()` computes the swap output purely from the exchange's current `firstTokenBalance`/`secondTokenBalance` at execution time [1](#0-0) . `ExchangeInjectActuator.execute()` and `ExchangeWithdrawActuator` mutate those same balances instantly and unconditionally (subject only to a static `balanceLimit`, not to any time/velocity constraint) [2](#0-1) . `ExchangeTransactionActuator.execute()` then re-reads the (already mutated) `ExchangeCapsule` from the store and executes the trade against whatever ratio is currently persisted [3](#0-2) .

There is no mechanism (TWAP, minimum-liquidity lock-up period, per-block manipulation guard, or oracle) preventing the same account from:
1. Injecting a large, disproportionate amount of tokenA (or TRX) into the pool to skew the price in one transaction,
2. Immediately trading tokenB for tokenA at the now-favorable ratio via `ExchangeTransactionContract` in a second transaction in the same block, and
3. Withdrawing the injected liquidity back out via `ExchangeWithdrawContract` in a third transaction in the same block, restoring the original ratio.

Because `ExchangeInjectActuator`/`ExchangeWithdrawActuator` only enforce that the resulting balances stay under `getExchangeBalanceLimit()` and that the caller has sufficient existing balance [4](#0-3) , and `ExchangeTransactionActuator` only checks a caller-supplied `tokenExpected` slippage floor rather than any protocol-level manipulation resistance [5](#0-4) , the sandwich sequence lets the attacker's own liquidity injection distort the swap price for any *other* trader executing against the same pool in the same block window, or lets the attacker itself extract more of the counter-token than the pool's steady-state price would allow, at the expense of the other liquidity in the pool. This mirrors exactly the reserve-manipulation root cause described in the pSeudoEth report — the exploited primitive is "manipulate on-chain reserves within an attacker-controlled window, trade, then revert," which is fully reachable here purely through signed transactions from a single unprivileged account, no smart-contract flash loan needed since java-tron's own actuators substitute for the "loan."

### Impact Explanation
An attacker who owns enough of either token can repeatedly perform Inject→Transaction→Withdraw sequences against low-liquidity Exchange pools to siphon the counter-token from the pool (i.e., from other liquidity providers/participants), realizing an unauthorized transfer of value with no meaningful capital lock-up, since injected liquidity is withdrawn again in the same block. This is a concrete theft-of-funds vector reachable purely by an unprivileged transaction broadcaster.

### Likelihood Explanation
Any account holding TRX or the relevant TRC10 token, and enough balance to satisfy `ExchangeInjectActuator`'s and `ExchangeWithdrawActuator`'s balance checks, can build and broadcast the three-transaction sequence with no privileged access. Since java-tron blocks batch many transactions and typical Exchange pools (esp. lesser-used ones) hold modest balances (well under `getExchangeBalanceLimit()`), constructing a profitable sandwich is straightforward for any address willing to front the temporary liquidity.

### Recommendation
Add manipulation-resistant safeguards to the native Exchange actuators: e.g., enforce a minimum holding/cooldown period between `ExchangeInjectContract`/`ExchangeWithdrawContract` operations on the same exchange by the same owner within a block or configurable window, require injected liquidity to be locked for at least N blocks before it can be withdrawn, and/or compute swap price using a time-weighted or block-boundary-snapshotted reserve rather than the instantaneous in-block balance so that same-block inject/withdraw cannot influence trades executed in between.

### Proof of Concept
1. Attacker identifies (or creates via `ExchangeCreateContract`) a low-liquidity Exchange pool for TokenA/TokenB.
2. In block N, attacker broadcasts, in order:
   - `ExchangeInjectContract` depositing a large amount of TokenA, skewing the `firstTokenBalance`/`secondTokenBalance` ratio (see `ExchangeInjectActuator.execute()`).
   - `ExchangeTransactionContract` selling TokenB for TokenA at the now-favorable ratio (see `ExchangeCapsule.transaction()` / `ExchangeTransactionActuator.execute()`), setting `expected` low enough to always pass the slippage check.
   - `ExchangeWithdrawContract` withdrawing the injected TokenA back out, restoring the ratio (see `ExchangeWithdrawActuator`).
3. `Manager`'s sequential block application processes all three in the order submitted, so the swap in step 2 executes against the attacker-skewed reserves, extracting more TokenA than the pre-manipulation price would have allowed, while the attacker's own principal is returned intact by step 3 in the same block.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-83)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L229-246)
```java
    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L57-69)
```java
      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeTransactionContract.getExchangeId()));

      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

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
