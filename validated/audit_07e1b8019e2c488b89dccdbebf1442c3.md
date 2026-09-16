### Title
Exchange creator can sandwich/front-run `ExchangeTransactionContract` trades via `ExchangeInjectContract`/`ExchangeWithdrawContract` - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
TRON's on-chain constant-product exchange lets the pool `creator` unilaterally rebalance the pool's reserves at any time via `ExchangeInjectContract`/`ExchangeWithdrawContract`, while any user can trade against the pool via `ExchangeTransactionContract`. Because the trade price is computed purely from the live reserves at execution time, a malicious/compromised exchange creator can front-run a victim's pending trade transaction (visible in the mempool) with an inject/withdraw transaction that shifts the price, and reverse it afterward, extracting value from the victim — the same "owner controls pool state trades depend on and can move it right before the user's tx lands" pattern described in the report.

### Finding Description
`ExchangeInjectActuator.execute` and `ExchangeWithdrawActuator.execute` let only the exchange's `creatorAddress` change `firstTokenBalance`/`secondTokenBalance` of the pool [1](#0-0)  and the analogous logic in `ExchangeWithdrawActuator` [2](#0-1) , and validation enforces that only the creator may call them (`account[...] is not creator`) [3](#0-2) .

Meanwhile, any user can call `ExchangeTransactionContract` to trade against the pool. The resulting output amount is computed from whatever the reserves are *at execution time* via `ExchangeCapsule.transaction`, using the constant-product formula in `ExchangeProcessor`: [4](#0-3) . The only user protection is the `expected` minimum-output parameter checked in `ExchangeTransactionActuator.doValidate` [5](#0-4) .

Since pending transactions (including trade transactions) are visible before block inclusion, and the creator fully controls the pool's balances, the creator can:
1. Observe a pending `ExchangeTransactionContract` from a victim.
2. Broadcast `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions to shift reserves unfavorably for the victim (e.g. reduce the buy-side liquidity right before the victim's trade executes, worsening their execution price up to the slippage tolerance the victim configured), profiting from the price impact, then reversing the injection/withdrawal afterward.

This mirrors the reported bug class exactly: a privileged address (`owner` in Swap/Aave, `creator` in the TRON exchange) that controls state relied upon by a pending user transaction can front-run that transaction to its own advantage.

### Impact Explanation
High — a malicious or compromised exchange creator can systematically extract value from every trader against their exchange pool by sandwiching trades, resulting in direct loss of funds for users trading TRX/TRC10 assets through the on-chain exchange feature.

### Likelihood Explanation
Low-to-Medium — it requires the exchange creator account to be malicious or compromised, and requires ordering control (mempool visibility + faster/prioritized broadcast, achievable by a witness/SR or by simply issuing the counter-transaction before the victim's trade confirms). This mirrors the report's stated "Low" likelihood given it needs a malicious/compromised privileged party, but is realistically exploitable since exchange creation is permissionless and any user can become a "creator" of a pool that unsuspecting traders route through.

### Recommendation
Require injects/withdraws and trades to respect stricter atomicity/ordering guarantees, e.g., disallow the creator from changing reserves within the same block as pending trades against that exchange, or bound the maximum single-block reserve change a creator can perform, and/or strengthen `expected`-based slippage checks to also validate against the pool state as of the start of the block. More fundamentally, consider deprecating the legacy on-chain `Exchange`/`Market` mechanism in favor of designs where price-affecting privileged operations cannot be interleaved with user trades within the same confirmation window.

### Proof of Concept
1. Attacker creates an exchange pool via `ExchangeCreateContract` as its `creatorAddress`.
2. Victim broadcasts `ExchangeTransactionContract` to buy token B with token A, with `expected` set to the minimum acceptable output based on the pool's currently observed reserves.
3. Attacker observes the victim's pending transaction and immediately broadcasts `ExchangeWithdrawContract` (or `ExchangeInjectContract`) to shift reserves so that, per `ExchangeCapsule.transaction`'s constant-product formula, the victim's trade executes at a worse price (still above `expected` due to the tolerance the victim set, but below the pre-manipulation fair price).
4. Attacker's manipulation transaction is confirmed before/alongside the victim's trade (attacker controls ordering as the only party who can perform this action, and can retry across blocks until ordering favors them).
5. Attacker reverses the injection/withdrawal, pocketing the price-impact difference extracted from the victim's trade. [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L57-83)
```java
      ExchangeCapsule exchangeCapsule;
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeInjectContract.getExchangeId()));
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L59-89)
```java
      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeWithdrawContract.getExchangeId()));

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

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeWithdrawActuatorTest.java (L865-874)
```java
    try {
      actuator.validate();
      actuator.execute(ret);
      fail();
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertEquals("account[41548794500882809695a8a687866e76d4271a1abc]"
              + " is not creator",
          e.getMessage());
    } catch (ContractExeException e) {
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
