Confirmed: `ExchangeCreateActuator.doValidate` only requires `firstTokenBalance > 0` and `secondTokenBalance > 0` (up to `getExchangeBalanceLimit()`), with no minimum-liquidity floor, so any account can create an arbitrarily thin pool such as 1 unit vs. 1 unit [1](#0-0) .

### Title
Bancor-style TRC10 Exchange spot ratio can be manipulated in a thin pool to extract disproportionate value on Inject/Withdraw - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
Java-tron's built-in TRC10 `Exchange` (bancor-relay AMM) prices every trade purely off the pool's own two on-chain reserve balances, with no external reference price, no minimum-liquidity requirement, and no time-weighting whatsoever — it is strictly worse than the Uniswap V3 TWAP referenced in the external report, since it is a raw, single-block spot price. `ExchangeCreateActuator` lets anyone create a pool with only `balance > 0` on each side [2](#0-1) , and `ExchangeInjectActuator`/`ExchangeWithdrawActuator` compute the "other side" amount strictly from the current reserve ratio at the moment they execute [3](#0-2) .

### Finding Description
`ExchangeCapsule.transaction()` implements the bancor-relay swap formula solely from `firstTokenBalance`/`secondTokenBalance` held in the capsule; the resulting price moves proportionally with trade size relative to reserves [4](#0-3) . Any unprivileged account can:
1. Call `ExchangeCreateContract` to spin up a pool with minimal reserves (e.g. `1` unit of TRX vs `1` unit of a TRC10 token — validation only checks `> 0`) [2](#0-1) .
2. Send a single large `ExchangeTransactionContract` swap that drastically skews `firstTokenBalance`/`secondTokenBalance` because the pool is thin [5](#0-4) .
3. Immediately (same or next transaction, same block) call `ExchangeInjectContract` or `ExchangeWithdrawContract` — both of which read the now-skewed `firstTokenBalance`/`secondTokenBalance` ratio directly to compute `anotherTokenQuant` [6](#0-5) [7](#0-6) .

Because there is no LP-share accounting in this design — `ExchangeWithdrawContract` can only be sent by the pool `creator` [8](#0-7)  and simply pulls tokens proportional to whatever the *current* spot ratio says — a creator who first swaps to skew the ratio, then withdraws, can extract far more of one token than their original deposit represented, at the expense of value injected into the pool by any other party via `ExchangeInjectContract` (which is open to anyone). This is the direct analog of the reported bug class: an attacker manipulates a low-liquidity AMM spot price and then uses that manipulated price as the "oracle" input to a second, differently-priced operation to realize a profit.

### Impact Explanation
An attacker can drain value from a TRC10 Exchange pool that other users have added liquidity to via `ExchangeInjectContract`, by momentarily distorting the reserve ratio with a large swap and withdrawing at the distorted rate. This is a concrete unauthorized extraction of pooled TRX/TRC10 asset value (theft of funds) reachable purely by broadcasting standard, unprivileged `ExchangeCreateContract` / `ExchangeTransactionContract` / `ExchangeInjectContract` / `ExchangeWithdrawContract` transactions — no special permissions, SR/witness status, or off-chain conditions required.

### Likelihood Explanation
Likelihood is high for any pool with low reserves relative to available capital, since `ExchangeCreateActuator` enforces no minimum liquidity floor, and the swap formula's price impact grows without bound as reserves shrink. Any two-step (or even single-block) sequence of `ExchangeTransactionContract` followed by `ExchangeWithdrawContract`/`ExchangeInjectContract` from the pool creator is sufficient — no need to interact with any external price feed as in the original Uniswap-based report.

### Recommendation
Enforce a protocol-level minimum liquidity/reserve threshold in `ExchangeCreateActuator.doValidate()`, and/or require `ExchangeInjectActuator`/`ExchangeWithdrawActuator` to use a time-weighted or otherwise manipulation-resistant reference ratio (or restrict inject/withdraw to only be evaluated using the ratio recorded at the start of the block/transaction bundle) rather than the instantaneous post-swap reserve ratio. Consider also tracking proportional LP shares instead of allowing the creator to withdraw against a spot-derived ratio.

### Proof of Concept
1. Attacker account `A` broadcasts `ExchangeCreateContract` creating pool `P` with `firstTokenBalance = 1 TRX`, `secondTokenBalance = 1` of TRC10 token `X` (passes validation since both are `> 0`) — see `ExchangeCreateActuator.doValidate` [2](#0-1) .
2. Victim `V` adds meaningful liquidity via `ExchangeInjectContract`, e.g. depositing `1,000 TRX` and receiving the computed pair amount of `X` at the (still roughly 1:1) ratio, per `ExchangeInjectActuator.execute` [6](#0-5) .
3. Attacker `A` sends `ExchangeTransactionContract` selling a large amount of TRX into `P`, using `ExchangeCapsule.transaction()`'s bancor formula to sharply skew `firstTokenBalance`/`secondTokenBalance` in `A`'s favor because the pool (even after `V`'s injection) is thin relative to the swap size [4](#0-3) .
4. Being the pool `creator`, `A` immediately sends `ExchangeWithdrawContract` for a chosen `tokenQuant`; `ExchangeWithdrawActuator.execute` computes `anotherTokenQuant` from the now-skewed ratio via `bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)`, letting `A` pull out a disproportionate amount of the token `V` deposited [9](#0-8) .
5. `A` nets a profit at `V`'s expense, all using only unprivileged, self-broadcast transactions (`ExchangeCreateContract`, `ExchangeTransactionContract`, `ExchangeWithdrawContract`), and no external oracle interaction needed since the "oracle" (the pool's own spot ratio) is directly attacker-controlled.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L201-208)
```java
    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (firstTokenBalance > balanceLimit || secondTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L63-89)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeWithdrawContract.getTokenId().toByteArray();
      long tokenQuant = exchangeWithdrawContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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
