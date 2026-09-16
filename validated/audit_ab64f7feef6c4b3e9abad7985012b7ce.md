### Title
Truncating integer division in the Exchange (bancor-style AMM) actuators permanently strands token balances once a reserve is driven to zero - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
The reported bug class is: a pool of value is accounted for proportionally, integer truncation causes the amount actually paid out to be less than the true entitlement, and the residual becomes permanently unreachable because there is no path to recover it once the "claim" condition can no longer be satisfied. The same structural pattern exists in java-tron's TRX/TRC10 `Exchange` (constant-reserve AMM) feature: `ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, and `ExchangeTransactionActuator` all move real balance out of a user's `AccountCapsule` and represent it only as bookkeeping numbers (`first_token_balance` / `second_token_balance`) inside `ExchangeCapsule`. There is no actual custodial account holding the deposited value — the `ExchangeCapsule`'s two integer fields are the sole ledger of what is owed back. Every calculation that converts one side of the pool into the other uses truncating integer division (`floorDiv`, `BigInteger.divide()`), which is systematically biased downward. Once either side of the pool is driven to exactly zero, every actuator that could return the leftover value refuses to run ("the exchange has been closed"), so any un-recovered dust in the *other* reserve becomes permanently frozen with no code path left to extract it — the same "surplus locked forever" outcome described in the external report for `stats_token`.

### Finding Description
`ExchangeCreateActuator.execute()` subtracts `firstTokenBalance`/`secondTokenBalance` directly from the creator's `AccountCapsule` balance/asset map and stores those two numbers as the only representation of the deposited value in `ExchangeCapsule`: [1](#0-0) 

`ExchangeWithdrawActuator.doValidate()`/`execute()` compute the amount of the "other" token to release using pure integer (`BigInteger`) truncating division of the pool ratio: [2](#0-1) 

`ExchangeInjectActuator.execute()` performs the analogous computation with `floorDiv`: [3](#0-2) 

`ExchangeCapsule.transaction()` (used by `ExchangeTransactionActuator` for AMM-style trades) likewise mutates the two reserve integers using an internal `Processor` (constant-product/bancor math with integer division), and the result can legally drive one side of the pool to exactly zero: [4](#0-3) 

Crucially, every actuator that is capable of extracting value from the pool (`ExchangeWithdraw`, `ExchangeInject`, `ExchangeTransaction`) requires **both** reserves to be nonzero, otherwise it rejects the transaction as "the exchange has been closed": [5](#0-4) [6](#0-5) 

Because the division used to compute `anotherTokenQuant`/`anotherTokenID` amounts is always rounded down (`divide`, `floorDiv`, `divideToIntegralValue`), repeated `Withdraw`/`Inject`/`Transaction` operations (or a single large trade that consumes nearly all of one reserve) can leave one reserve at exactly `0` while the other reserve retains a nonzero residual balance. At that point:
- `ExchangeWithdrawActuator` and `ExchangeInjectActuator` cannot be used (division by the now-zero reserve throws `ArithmeticException`, and validation explicitly blocks it once either side is `0`).
- `ExchangeTransactionActuator` is blocked by the same "exchange has been closed" check.
- There is no dedicated "close exchange / sweep remaining balance" actuator in the codebase to redeem the stranded reserve back to the creator or any other account.

The residual value recorded in the surviving reserve field is therefore permanently unreachable — mirroring exactly the reported pattern where `claimed_supply < sale_supply` causes the un-distributed difference to be permanently stuck in `stats_token` with no recovery path.

### Impact Explanation
Any TRX or TRC10 tokens deposited into an `Exchange` pool (via `ExchangeCreateContract` or `ExchangeInjectContract`) represent real, previously-spendable balance deducted from user accounts. Once truncation drives one reserve to zero while the other remains positive, that positive residual is permanently frozen — it can never be withdrawn, traded, or otherwise reclaimed by the exchange creator or any other party, since no actuator can operate on an exchange whose ledger has a zero reserve, and the codebase provides no exchange-close/refund path. This is a genuine permanent loss of previously real, custodied value, equivalent in kind to the reported `stats_token` fund-loss issue, though the magnitude per occurrence is bounded by integer rounding error (at most a few of the smallest token units per operation, but it accumulates over the pool's lifetime and can total the entire remaining reserve if the pool is drained to the truncation boundary).

### Likelihood Explanation
Reaching this state does not require any privileged access — any account holding TRX/TRC10 can call `ExchangeCreateContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`, and `ExchangeTransactionContract` directly. An exchange creator (or any trader interacting with the pool via repeated small trades/withdrawals) can deterministically engineer a sequence of operations that truncates one reserve down to zero while leaving nonzero residual in the other, since all the relevant divisions are integer/floor divisions with no remainder redistribution. This makes the scenario reachable by a single unprivileged party without cooperation from anyone else, though it may take multiple transactions to line up the rounding.

### Recommendation
- Add an explicit "close exchange" mechanism that atomically settles the remaining balance of both reserves back to the exchange creator once one reserve legitimately reaches zero (economic exhaustion), rather than simply freezing further operations.
- Alternatively, when either reserve is reduced to zero as a result of `Withdraw`/`Inject`/`Transaction`, force the corresponding actuator to also flush and return the other reserve's residual balance in the same transaction.
- Consider switching from floor/truncating division to rounding conventions that don't systematically bias in the protocol's favor, and add invariant checks/tests that assert the sum of amounts withdrawable from an exchange over its lifetime equals the sum initially deposited (accounting for legitimate trading fees, if any).

### Proof of Concept
1. Account A calls `ExchangeCreateContract` to create an exchange with `firstTokenBalance = F0` (TRX) and `secondTokenBalance = S0` (TRC10 token `X`), depositing real balance out of A's account — see `ExchangeCreateActuator.execute()`.
2. Account A (or any trader) repeatedly calls `ExchangeWithdrawContract`/`ExchangeInjectContract`/`ExchangeTransactionContract` with quantities chosen so that the integer-truncating division in `ExchangeCapsule.transaction()` / `ExchangeWithdrawActuator` / `ExchangeInjectActuator` rounds `anotherTokenQuant` down on each call.
3. Choose withdrawal/trade sizes such that on the final operation the first reserve (`firstTokenBalance`) is driven to exactly `0` while the second reserve (`secondTokenBalance`) retains a small positive residual `r > 0` (achievable because each computed "other side" amount is `floor(ratio * quant)`, which can under-shoot the true proportional amount by up to 1 unit per call, and the caller controls the exact input `quant`).
4. Attempt any further `ExchangeWithdrawContract`, `ExchangeInjectContract`, or `ExchangeTransactionContract` referencing this exchange ID: all fail — `ExchangeWithdrawActuator.doValidate()` and `ExchangeTransactionActuator.doValidate()` throw `ContractValidateException("Token balance in exchange is equal with 0, the exchange has been closed")`; `ExchangeInjectActuator` similarly throws or divides by zero.
5. The residual `r` recorded in `secondTokenBalance` remains in the `ExchangeCapsule`/`ExchangeV2Store` forever, with no actuator in the codebase capable of extracting it back to account A or anyone else — a permanent loss of the previously-real deposited value.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L60-90)
```java
      long newBalance = subtractExact(accountCapsule.getBalance(), fee);

      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, firstTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(firstTokenID, firstTokenBalance, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, secondTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(secondTokenID, secondTokenBalance, dynamicStore, assetIssueStore);
      }

      long id = addExact(dynamicStore.getLatestExchangeNum(), 1);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
      if (dynamicStore.getAllowSameTokenName() == 0) {
        //save to old asset store
        ExchangeCapsule exchangeCapsule =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsule.setBalance(firstTokenBalance, secondTokenBalance);
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L209-212)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L194-197)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```
