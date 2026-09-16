### Title
Exchange creator can front/back-run trader transactions via `ExchangeInjectActuator`/`ExchangeWithdrawActuator` to alter bancor pool depth and extract value from a sandwiched `ExchangeTransactionContract` trade - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
TRON's on-chain Bancor-style Exchange feature prices trades from `ExchangeCapsule.firstTokenBalance`/`secondTokenBalance` via `ExchangeProcessor.exchange()` [1](#0-0) . These two balances play the same role as TermMax's virtual reserves derived from `market.lsf`. The exchange's creator (an ordinary account, verified only by address equality, not a privileged system role) can call `ExchangeInjectActuator` or `ExchangeWithdrawActuator` at any time to instantly change these balances [2](#0-1) [3](#0-2) . Because a single block can contain the creator's inject/withdraw transaction sandwiched between two independent trader transactions submitted via `ExchangeTransactionActuator` (buy then sell), the pool depth changes exactly when it matters, letting the creator (or a colluding trader) extract value from the difference between the buy-side slippage and the sell-side slippage — the same "change the pricing curve's virtual reserve mid-flight" pattern described in the TermMax `setLsf` report.

### Finding Description
`ExchangeTransactionActuator.execute` computes `anotherTokenQuant` by calling `exchangeCapsule.transaction(...)`, which in turn invokes `ExchangeProcessor.exchange(sellTokenBalance, buyTokenBalance, sellTokenQuant)` — a bancor-relay formula whose output strongly depends on the ratio of trade size to pool balance [4](#0-3) [5](#0-4) .

The pool's `firstTokenBalance`/`secondTokenBalance` are not immutable settlement state derived purely from trades: the exchange's creator can unilaterally and instantly modify them via `ExchangeInjectActuator` (adds liquidity, proportionally increasing depth) [6](#0-5)  or `ExchangeWithdrawActuator` (removes liquidity, proportionally decreasing depth) [7](#0-6) . Both actuators only check `accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())` [8](#0-7) [3](#0-2)  — this is an ordinary signed transaction from the address that happened to call `ExchangeCreateActuator`, not a system-privileged role, and nothing prevents the creator from being the same party executing the sandwiching trades.

This is structurally analogous to the TermMax bug: in TermMax, `market.owner` calls `setLsf` to change the divisor used to derive virtual reserves from real reserves, altering the AMM curve's apparent liquidity between a front-run buy and a back-run sell. In java-tron's Exchange, the creator calls `ExchangeInjectActuator`/`ExchangeWithdrawActuator` to directly rescale the real balances that feed the same bancor curve, altering the effective slippage of a `sell` executed after a prior `buy` (or vice versa), all within the same block via `ExchangeTransactionContract` calls that any unprivileged trader can broadcast. `expected` (slippage-min-out) field on `ExchangeTransactionContract` only bounds the trader's own downside; it does not stop the creator from restructuring pool depth between the trader's own two legs of a round-trip trade, nor does it protect other traders whose transactions land between the creator's inject/withdraw and the price move.

Concretely: an attacker who is (or colludes with) the exchange creator can:
1. Buy asset X with a small amount relative to current pool depth via `ExchangeTransactionContract` (incurs bancor slippage against a shallow pool).
2. Immediately inject a large, proportional amount of liquidity via `ExchangeInjectContract` (does not move the bancor price ratio, but changes `firstTokenBalance`/`secondTokenBalance` depth) [6](#0-5) .
3. Sell X back via a second `ExchangeTransactionContract` — the same nominal trade size now represents a much smaller fraction of the (now deeper) pool, so it experiences far less bancor slippage than the initial buy did, and part of the difference is realized as profit at the expense of the pool/other participants, funded implicitly by the injected liquidity's supply-token accounting.
4. Optionally withdraw the injected liquidity back out via `ExchangeWithdrawContract` to recover the deposit, closing the loop within one block.

Because `ExchangeInjectActuator`/`ExchangeWithdrawActuator` execute unconditionally against whatever `firstTokenBalance`/`secondTokenBalance` exist at call time, and `ExchangeTransactionActuator` has no protection against depth manipulation between legs (only a per-call `expected` minimum), this reachable creator-controlled reserve-resizing primitive lets value be extracted from the AMM exactly as `setLsf` does in TermMax.

### Impact Explanation
Impact is **High**: value is siphoned from the on-chain Bancor exchange pool (and implicitly from other liquidity/participants in that exchange) into the attacker's account balance/assets, an unauthorized transfer of value achieved purely through sequenced, permitted transaction calls (`ExchangeInjectContract`, `ExchangeTransactionContract`, `ExchangeWithdrawContract`). No consensus/node compromise is needed — it is pure economic value leakage from a market mechanism reachable by ordinary signed transactions.

### Likelihood Explanation
Likelihood is **Medium**: TRON's Exchange contracts are legacy/rarely used compared to newer DeFi-style DEXs, and executing the sandwich profitably requires the attacker to control (or collude with) the exchange's creator address and requires enough pool imbalance/trade sizing to make the round-trip profitable net of fees. However, because the creator role has no special trust assumption beyond "whoever called `ExchangeCreateContract`," and there is no cooldown, time-lock, or proportional-injection restriction preventing this sequence within a single block, any creator (including one set up purely to run this attack) can execute it at will.

### Recommendation
- Disallow `ExchangeInjectActuator`/`ExchangeWithdrawActuator` from executing in the same block as, or immediately adjacent to, `ExchangeTransactionActuator` calls against the same exchange, or enforce a minimum time/block delay between liquidity changes and trades.
- Alternatively, redesign settlement so that a trade's execution price/slippage is computed atomically against a reserve snapshot that cannot be altered by a unilateral creator action mid-sequence (e.g., require injections/withdrawals to be proportional-only changes that provably cannot alter realized slippage for pending trades, or require multi-party/timelocked authorization for reserve changes, analogous to removing TermMax's mutable `lsf` in favor of immutable, separately-deployed pools).
- Consider adding a slippage/price-impact bound check comparing the trade's realized rate against a TWAP or pre-block snapshot rather than solely trusting the current mutable balances at execution time.

### Proof of Concept
Not independently executed (index-only investigation); reasoning is derived directly from the code paths cited above:
1. `ExchangeCreateActuator` lets any account create an Exchange with `first_token_balance`/`second_token_balance`, becoming its `creatorAddress` [9](#0-8) .
2. Attacker (as trader) submits `ExchangeTransactionContract` selling `tokenA` for `tokenB` on a shallow pool, incurring bancor slippage [10](#0-9) .
3. Attacker (as creator) submits `ExchangeInjectContract` immediately after, proportionally scaling `firstTokenBalance`/`secondTokenBalance` up [6](#0-5) .
4. Attacker (as trader) submits a second `ExchangeTransactionContract` selling `tokenB` back for `tokenA` against the now-deeper pool, realizing less slippage than legs 2 and 3 combined would predict for a static pool, netting a profit; the injected liquidity can then be withdrawn back via `ExchangeWithdrawContract` [7](#0-6) .

Because no test harness or forge-equivalent script was run against this repository during this analysis, the exact numeric profit was not empirically measured; further validation (e.g., a JUnit test analogous to `ExchangeInjectActuatorTest`/`ExchangeWithdrawActuatorTest`) would be needed to quantify concrete profit for specific balance/quant values.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L174-177)
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L104-116)
```java
      {
        // only save to new asset store
        ExchangeCapsule exchangeCapsuleV2 =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsuleV2.setBalance(firstTokenBalance, secondTokenBalance);
        exchangeV2Store.put(exchangeCapsuleV2.createDbKey(), exchangeCapsuleV2);
      }
```
