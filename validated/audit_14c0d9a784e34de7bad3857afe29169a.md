### Title
`ExchangeWithdrawActuator` allows withdrawing liquidity without a minimum-received guard, exposing LP withdrawers to front-run price movement - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The `ExchangeWithdrawContract` used by TRC10 AMM liquidity providers to withdraw funds from a bancor-style `Exchange` pool has no analog of the `expected`/`minOutputAmount` slippage guard that java-tron already added to `ExchangeTransactionContract` for regular trades. A liquidity-provider withdrawal computes the counter-token amount purely from the pool's *current* balances at execution time, with no way for the submitter to bound the minimum amount they are willing to accept.

### Finding Description
`ExchangeTransactionActuator` (the buy/sell-token actuator) was hardened against this exact bug class: it carries a `getExpected()` field and validates `anotherTokenQuant >= tokenExpected` before executing the trade: [1](#0-0) 

`ExchangeWithdrawActuator`, which lets an exchange's creator withdraw liquidity, has no equivalent field or check. Its `validate()`/`doValidate()` computes `anotherTokenQuant` strictly from whatever the pool balances happen to be at the moment of execution: [2](#0-1) 

and `execute()` performs the transfer using that same on-the-fly computed value, again with no minimum bound supplied by the submitter: [3](#0-2) 

Because the pool ratio (`firstTokenBalance`/`secondTokenBalance`) can be shifted between transaction submission and block inclusion — e.g. by an `ExchangeTransactionContract` trade or another `ExchangeInjectContract`/`ExchangeWithdrawContract` operation landing first in the same block — the withdrawer has no way to guarantee a floor on `anotherTokenQuant`. This is the exact "burn() has no slippage control" pattern from the external report, but reachable via a plain signed `ExchangeWithdrawContract` transaction rather than a smart-contract call.

`ExchangeInjectActuator` (deposit side) suffers the same absence of a minimum-received check, but the report's analog specifically concerns a "sell"/withdrawal path where the caller receives less collateral than expected, which maps most directly onto `ExchangeWithdrawActuator`.

### Impact Explanation
Impact is Medium: the exchange creator withdrawing liquidity can receive fewer counter-tokens than they intended when they signed the transaction, because the AMM pool ratio can move against them before their transaction is packed into a block. This causes an unbacked/incorrect balance outcome for the withdrawer (getting less value out of the pool than expected) without any recourse, since there is no `minOutputAmount`/`expected` parameter to enforce a floor, unlike the sibling `ExchangeTransactionContract`.

### Likelihood Explanation
Likelihood is Medium: exploitation requires that the pool's price move (via a trade, inject, or another withdraw) between the time the withdraw transaction is broadcast and when it is executed — a normal, plausible occurrence for any actively-traded TRC10 exchange pair, and one that is trivially amplified by a searcher/attacker deliberately front-running the withdrawal with their own `ExchangeTransactionContract` trade.

### Recommendation
Add an `expected`/`minOutputAmount`-style field to `ExchangeWithdrawContract` (and ideally `ExchangeInjectContract`) mirroring the existing `expected` field and check already present in `ExchangeTransactionContract`, and enforce it in `ExchangeWithdrawActuator.doValidate()`:
```diff
- long anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
-     .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
+ long anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
+     .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
+ if (anotherTokenQuant < contract.getExpected()) {
+   throw new ContractValidateException("token required must greater than expected");
+ }
```

### Proof of Concept
1. An `Exchange` pool exists for TRX/TokenA with balances `firstTokenBalance`, `secondTokenBalance`.
2. The pool creator broadcasts `ExchangeWithdrawContract{exchangeId, tokenId=TRX, quant=X}` expecting to receive roughly `Y = secondTokenBalance * X / firstTokenBalance` of TokenA, computed off-chain from the pool state they observed.
3. Before this transaction is packed, another actor broadcasts an `ExchangeTransactionContract` trade against the same pool (or the SR/block-producer includes such a trade first), shifting `firstTokenBalance`/`secondTokenBalance`.
4. When the withdraw transaction executes in `ExchangeWithdrawActuator.execute()` [3](#0-2) , `anotherTokenQuant` is recomputed from the now-shifted balances, yielding an amount lower than `Y`, with no `expected`/minimum check to reject the outcome — unlike `ExchangeTransactionActuator`, which would reject via the `tokenExpected` check [1](#0-0) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-90)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-227)
```java
    BigDecimal bigFirstTokenBalance = new BigDecimal(String.valueOf(firstTokenBalance));
    BigDecimal bigSecondTokenBalance = new BigDecimal(String.valueOf(secondTokenBalance));
    BigDecimal bigTokenQuant = new BigDecimal(String.valueOf(tokenQuant));
    final boolean allowHarden = allowHarden();
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
```
