Confirmed: both `ExchangeInjectActuator` and `ExchangeWithdrawActuator` restrict execution to the exchange's creator address, and the actual on-chain price (the ratio of `firstTokenBalance`/`secondTokenBalance`) used to compute swap output in `ExchangeCapsule.transaction()` is exactly what these creator-only calls mutate. [1](#0-0) [2](#0-1) 

### Title
Exchange creator can front-run a trader's ExchangeTransactionContract by injecting/withdrawing liquidity to manipulate the swap price - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
java-tron's Bancor-style token exchange (`ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract`) lets the exchange's `creatorAddress` freely rebalance the pool's `first_token_balance`/`second_token_balance` at any time via `ExchangeInjectActuator` and `ExchangeWithdrawActuator`. These balances are the sole "price feed" consulted by `ExchangeCapsule.transaction()` when a trader submits an `ExchangeTransactionContract`. Because the creator can observe a pending trade in the mempool and submit an inject/withdraw transaction that lands first (either organically or by paying higher energy/priority), they can shift the exchange rate against the trader immediately before the trade executes — directly analogous to the reported "owner updates the oracle right before flash()" issue.

### Finding Description
The swap price is derived purely from `exchangeCapsule.getFirstTokenBalance()`/`getSecondTokenBalance()` inside `ExchangeCapsule.transaction()`: [3](#0-2) 

`ExchangeInjectActuator.execute()` and `ExchangeWithdrawActuator.execute()` mutate exactly these balances (via `exchangeCapsule.setBalance(...)`) and are only gated by an ownership check performed in `doValidate()`, not by any timelock, cooldown, or commit-reveal scheme: [4](#0-3) [5](#0-4) 

A trader broadcasts `ExchangeTransactionContract` specifying `token_id`, `quant`, and a minimum `expected` output as slippage protection. `ExchangeTransactionActuator.doValidate()` recomputes the output at validation time and only rejects the trade if the output falls below `expected`: [6](#0-5) 

Because `expected` is chosen by the trader based on the pool state they observed when *building* the transaction (not the state at block-inclusion time), the creator can watch the pending `ExchangeTransactionContract` in the mempool, then submit their own `ExchangeInjectContract`/`ExchangeWithdrawContract` to shift the ratio unfavorably for the trader — as long as the resulting quote still clears the trader's `expected` floor, the trade still executes at a worse rate than the fair price the trader observed, and the creator profits from the ensuing arbitrage/reversal. This mirrors the reported bug class: a single privileged party (there, the swapper owner controlling the oracle; here, the exchange creator controlling pool balances) can update the pricing input immediately before a counterparty's already-broadcast transaction executes, extracting value with no cooldown or commit-reveal protection.

### Impact Explanation
A trader executing an `ExchangeTransactionContract` can receive a worse-than-expected (though still `expected`-satisfying) amount of tokens, while the exchange creator captures the difference by sandwiching the trade with inject/withdraw calls. This is a direct theft-of-funds vector for exchange counterparties, satisfying the "concrete unauthorized... theft of funds" impact bar, since ordinary users interacting with these public, unprivileged `ExchangeTransactionContract` calls lose value to a privileged, single-actor-controlled operation with no rate limiting.

### Likelihood Explanation
Likelihood is elevated because: (1) the exchange creator role requires no special resources beyond having created the exchange; (2) inject/withdraw and transaction contracts are ordinary, cheap, broadcastable transactions with no cooldown between them; (3) mempool visibility of the victim's `ExchangeTransactionContract` (with its `quant`/`expected` values) is trivial for any node operator or the creator running their own node; (4) the `expected` floor set by a typical trader (often loosely set, or defaulted low by wallets/SDKs) frequently leaves room for meaningful slippage extraction.

### Recommendation
- Enforce a minimum time delay (cooldown) between an exchange creator's inject/withdraw operations and any following trade execution within the same block or a short window, or disallow inject/withdraw and transaction contracts from the same exchange in the same block.
- Consider bounding the maximum single-block price movement an inject/withdraw can cause, or require injects/withdraws to be proportional (preserving the exchange rate) rather than allowing balance changes that alter the ratio.
- Alternatively, expose an oracle-independent execution price commitment (e.g., trader signs off on the exact pool state/block height) so any pool state change invalidates the pending trade rather than merely tightening the `expected` outcome.

### Proof of Concept
1. Exchange creator creates an exchange with `ExchangeCreateContract` for `TokenA`/`TokenB` with balances `X`/`Y`.
2. Victim submits `ExchangeTransactionContract` to sell `q` of `TokenA`, computing `expected` off-chain from the current visible ratio `X/Y`.
3. Creator observes the victim's transaction in the mempool and submits `ExchangeWithdrawContract` (creator-only, per `ExchangeInjectActuator`/`ExchangeWithdrawActuator` validation at [2](#0-1) ) that reduces `Y` relative to `X`, worsening the effective rate for `TokenA→TokenB` swaps, while still keeping the victim's quote above their `expected` floor.
4. Block producer includes creator's withdraw transaction before the victim's `ExchangeTransactionContract` (same block ordering by fee/priority, or across consecutive blocks the creator can time).
5. `ExchangeTransactionActuator.execute()` computes `anotherTokenQuant` via `exchangeCapsule.transaction(...)` using the now-worsened balances, giving the victim less `TokenB` than the true pre-manipulation rate implied, per [7](#0-6) .
6. Creator can subsequently re-inject to restore the ratio and repeat, extracting value from the victim's trade each cycle.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-89)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-145)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-69)
```java
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
