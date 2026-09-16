## Title
Missing Slippage Protection in Exchange Liquidity Inject/Withdraw Enables Sandwich Attack on Exchange Creators - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

## Summary
java-tron's built-in Bancor-style Exchange supports liquidity add/remove operations (`ExchangeInjectContract`, `ExchangeWithdrawContract`) that compute the counterpart token amount proportionally from the *current* pool balances at execution time, exactly analogous to how the UniswapV3 locker in the external report recomputes deposit amounts from the pool's live liquidity/tick state during `_convertPositionToFullRange`. Unlike `ExchangeTransactionContract`, which carries an `expected` slippage-bound field [1](#0-0) , `ExchangeInjectContract` and `ExchangeWithdrawContract` have no slippage/minimum-output parameter at all, so the exchange creator has no way to bound how much of the counterpart token they will deposit or receive.

## Finding Description
`ExchangeInjectActuator.execute` computes `anotherTokenQuant` strictly from the pool's live `firstTokenBalance`/`secondTokenBalance` ratio at the moment of execution: [2](#0-1) 

Likewise, `ExchangeWithdrawActuator.execute` computes the returned counterpart amount from the same live ratio: [3](#0-2) 

Neither `ExchangeInjectContract` nor `ExchangeWithdrawContract` includes an `expected`/minimum-output bound field, in contrast to `ExchangeTransactionContract` which explicitly has one and is enforced in `doValidate`: [4](#0-3) 

The pool ratio itself can be moved arbitrarily by any account through `ExchangeTransactionContract`, which uses a Bancor curve processor to swap tokens and mutate `firstTokenBalance`/`secondTokenBalance`: [5](#0-4) 

This is structurally identical to the reported bug class: a liquidity-affecting operation (`_convertPositionToFullRange` in the report, `ExchangeInject`/`ExchangeWithdraw` here) recalculates deposit/withdrawal amounts from the *current* pool state with no caller-supplied bound, and the pool state is manipulable in the same block by a third party via an ordinary swap-type transaction (`ExchangeTransactionContract` here, a large swap in the report) before the victim's transaction executes, followed by a reverse swap afterward to capture the value the victim was forced to overpay or undercollect.

## Impact Explanation
An exchange creator who injects liquidity can be made to deposit a manipulated (unfavorable) amount of the counterpart token, or, when withdrawing, receive less of the counterpart token than the pool's fair-value ratio would dictate. The attacker profits by trading against the distorted pool before the victim's tx and reversing the trade afterward, extracting value contributed by the victim — a direct fund-theft primitive against the exchange creator, reachable by any unprivileged account capable of broadcasting `ExchangeTransactionContract` and `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions.

## Likelihood Explanation
Any account can create an Exchange and later inject/withdraw liquidity (the only permission check is that the caller equals the exchange's creator, confirmed at [6](#0-5)  and [7](#0-6) , which is not a privileged/SR/committee role). Any other account can freely submit `ExchangeTransactionContract` swaps against the same exchange to manipulate `firstTokenBalance`/`secondTokenBalance` immediately before and after the victim's inject/withdraw transaction, since there is no cooldown or per-block rate limiting on exchange trades. This requires only ordinary API/transaction-broadcast access.

## Recommendation
Add slippage-bound fields (e.g., `expected_another_token_quant` with min/max semantics) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce them in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` `doValidate`/`execute` the same way `expected` is enforced in `ExchangeTransactionActuator`.

## Proof of Concept
1. Exchange creator prepares an `ExchangeInjectContract` to inject `tokenQuant` of token A at the currently observed fair ratio.
2. Attacker broadcasts an `ExchangeTransactionContract` swap that heavily skews `firstTokenBalance`/`secondTokenBalance` for that exchange right before the creator's transaction is applied.
3. The creator's `ExchangeInjectContract` executes, computing `anotherTokenQuant` from the skewed ratio via `ExchangeInjectActuator.execute` lines 71-83, forcing the creator to deposit an inflated amount of token B relative to fair value (no `expected` bound exists to reject this).
4. Attacker submits a second `ExchangeTransactionContract` reverting the pool ratio back, extracting the excess value the creator was forced to contribute.
5. Net result: attacker profits at the exchange creator's expense purely due to the absence of a slippage-bound parameter on `ExchangeInjectContract`/`ExchangeWithdrawContract`.

### Citations

**File:** protocol/src/main/protos/api/api.proto (L184-188)
```text
  rpc ExchangeWithdraw (ExchangeWithdrawContract) returns (TransactionExtention) {
  }

  rpc ExchangeTransaction (ExchangeTransactionContract) returns (TransactionExtention) {
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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
