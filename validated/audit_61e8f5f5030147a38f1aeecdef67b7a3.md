Confirmed: `ExchangeInjectContract` and `ExchangeWithdrawContract` both lack an `expected`/slippage-bound field, unlike `ExchangeTransactionContract` which has `expected` [1](#0-0) . This confirms the analog.

### Title
Missing Slippage Protection in `ExchangeInjectActuator` Allows Front-Run Loss of Paired Token During Bancor-Style Liquidity Injection - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`ExchangeInjectActuator` lets any exchange creator inject one token into their TRX/TRC10 bancor-style pool by specifying `tokenId` and `quant`, but the amount of the *paired* token that gets debited (`anotherTokenQuant`) is computed on-the-fly from the pool's live balance ratio at execution time, with no user-supplied minimum/maximum bound, exactly mirroring the `MetapoolRouter.addLiquidityOneETHKeepYt` finding where one side of a two-sided operation lacks slippage protection.

### Finding Description
`ExchangeInjectContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` [2](#0-1) . In `doValidate()`/`execute()`, the actuator computes `anotherTokenQuant` purely from `firstTokenBalance`/`secondTokenBalance` read from the exchange's current on-chain state at the moment the transaction executes: [3](#0-2) 
The only guard is `anotherTokenQuant <= 0` and a balance-sufficiency check [4](#0-3) . There is no way for the caller to bound how much of the second token they are willing to have debited (no `minAnotherTokenQuant`/`maxAnotherTokenQuant`), unlike `ExchangeTransactionActuator`, which enforces `anotherTokenQuant < tokenExpected` reverting the trade [5](#0-4) . `ExchangeWithdrawActuator`'s "Not precise enough" check similarly derives its bound from the same live pool state rather than from a user-specified value, so it does not prevent economic front-running either.

Since `ExchangeInject` execution is fully dependent on pool state at block-inclusion time (which any account can shift beforehand via `ExchangeTransactionContract` trades against the same pool), a broadcaster who signs an inject transaction expecting a certain paired-token debit based on the current ratio can have that ratio manipulated between broadcast and execution (e.g., MEV/front-running, sandwiching, or a large trade landing first in the same block), causing significantly more of the second token to be deducted than the signer intended when they set `quant` for the first token.

### Impact Explanation
The owner/creator of the exchange (an ordinary permissionless account — any account can call `ExchangeCreateContract`) can have an unbounded, unintended amount of their second-token balance debited when injecting liquidity, resulting in direct loss of asset funds analogous to the slippage loss described in the report. This is a concrete loss-of-funds condition for an unprivileged, permissionless actor.

### Likelihood Explanation
Reachable by any account that owns an exchange and calls `ExchangeInjectContract` — no special privilege required beyond having created the exchange, which itself is permissionless. Exploitation requires only a preceding or intervening `ExchangeTransactionContract` (or another inject/withdraw) shifting the pool ratio, which any account can broadcast; this is a normal MEV/front-running scenario rather than a witness/consensus attack.

### Recommendation
Add an explicit expected/slippage-bound field to `ExchangeInjectContract` (mirroring `ExchangeTransactionContract.expected`) and enforce it in `ExchangeInjectActuator.doValidate()`, rejecting the transaction if the computed `anotherTokenQuant` deviates unfavorably from the caller's specified bound.

### Proof of Concept
1. Account A creates an exchange pool (`ExchangeCreateContract`) with tokens X/Y.
2. A signs and broadcasts `ExchangeInjectContract` injecting `quant` of X, expecting to also debit `Y0` of Y based on the pool ratio at signing time.
3. Before A's transaction is included, another account (or A's own subsequent action, or a bot) broadcasts an `ExchangeTransactionContract` trade against the same pool that shifts the X:Y ratio.
4. A's inject transaction executes using the new ratio in `ExchangeInjectActuator.execute()` [6](#0-5) , debiting a much larger `anotherTokenQuant` of Y than A expected, since no `expected`/minimum field exists to cause a revert.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-29)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-99)
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-227)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L229-256)
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

    if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(anotherTokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(anotherTokenID, anotherTokenQuant, dynamicStore)) {
        throw new ContractValidateException("another token balance is not enough");
      }
    }
```
