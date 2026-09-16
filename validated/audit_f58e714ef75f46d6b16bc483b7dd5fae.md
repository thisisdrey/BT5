This confirms the analog. `ExchangeWithdrawContract` has no `expected`/min-amount field, unlike `ExchangeTransactionContract` which explicitly has `expected` and enforces `"token required must greater than expected"`.## Title
Missing minimum-amount (slippage) protection in `ExchangeWithdrawContract`/`ExchangeInjectContract` unlike `ExchangeTransactionContract` - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The Bancor-style exchange pools in java-tron (`ExchangeCreateContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`, `ExchangeTransactionContract`) mirror the reported class of bug: a broadcastable transaction that mutates an AMM-like pool without a user-supplied minimum-received guard, while a sibling transaction type in the same family does have one.

### Finding Description
`ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — there is no field allowing the caller to specify a minimum acceptable amount of the "other" token to be returned [1](#0-0) . In `ExchangeWithdrawActuator.execute`, `anotherTokenQuant` is computed purely from the exchange pool's *current* on-chain balances at execution time via a constant-product-style ratio, and is transferred to the account with no comparison against any caller-supplied bound [2](#0-1) . The same pattern exists in `ExchangeInjectActuator`, which likewise has no minimum/maximum bound on `anotherTokenQuant` for the token amount required to inject [3](#0-2) .

This is in stark contrast to `ExchangeTransactionContract`, which explicitly includes an `expected` field [4](#0-3) , and whose actuator validates `anotherTokenQuant < tokenExpected` and rejects the transaction with `"token required must greater than expected"` if the slippage exceeds the caller's tolerance [5](#0-4) .

Because transactions sit in the mempool and are ordered by block producers, and because `ExchangeInjectContract`/`ExchangeTransactionContract`/`ExchangeWithdrawContract` transactions from other unprivileged accounts can be interleaved arbitrarily against the same `exchange_id` before a given `ExchangeWithdrawContract` is packed into a block, the pool ratio at validate-time (client construction time) can differ materially from the ratio at execute-time (block application time). Any account holding an exchange-pool LP-style position — which in java-tron's model is simply the exchange `creator_address`, since `ExchangeWithdrawContract` requires `accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())` [6](#0-5)  — has no on-chain mechanism to bound the resulting withdrawal amount, unlike a trader using `ExchangeTransactionContract`.

### Impact Explanation
An exchange creator withdrawing liquidity (or injecting additional liquidity) via `ExchangeWithdrawContract`/`ExchangeInjectContract` can receive (or be forced to contribute) an amount of the paired token far different from what they expected if other market participants front-run/sandwich the transaction with `ExchangeTransactionContract` swaps against the same pool between the time the withdraw transaction is signed and the time it's included in a block. This results in unbacked/asymmetric value transfer relative to the creator's intent — economically equivalent to the reported rToken-burn slippage issue — though it is bounded to Medium severity because it requires an existing exchange pool, an LP-holding account (the creator), and active trading against that same pool in the same window; it does not directly allow theft from arbitrary accounts.

### Likelihood Explanation
Likelihood is Medium: it requires (1) an active `Exchange` created via `ExchangeCreateContract`, (2) the creator issuing an `ExchangeWithdrawContract` or `ExchangeInjectContract`, and (3) an adversary (or natural market activity) submitting `ExchangeTransactionContract` transactions against the same `exchange_id` in the same block-inclusion window to shift the ratio unfavorably. This is straightforward for any unprivileged account to attempt since `ExchangeTransactionContract` is a standard broadcastable contract type reachable via the JSON-RPC/HTTP `ExchangeTransaction` wallet API [7](#0-6) , with no special permissions required.

### Recommendation
Add an explicit minimum/maximum bound field to `ExchangeWithdrawContract` (e.g., `another_token_min_received`) and to `ExchangeInjectContract` (e.g., `another_token_max_quant`), and enforce them in `ExchangeWithdrawActuator.doValidate`/`execute` and `ExchangeInjectActuator.doValidate`/`execute` respectively, mirroring the existing `tokenExpected` check already present in `ExchangeTransactionActuator`.

### Proof of Concept
1. Account `A` creates an exchange pool via `ExchangeCreateContract` (becomes `creator_address`).
2. `A` signs an `ExchangeWithdrawContract` for `exchange_id=X`, `token_id=firstTokenID`, `quant=Q`, expecting `anotherTokenQuant` ≈ current ratio-implied amount, and broadcasts it.
3. Before `A`'s transaction is packed into a block, attacker `B` broadcasts one or more `ExchangeTransactionContract` swaps against the same `exchange_id=X`, shifting `firstTokenBalance`/`secondTokenBalance` significantly (this is a valid, unprivileged transaction with no restriction preventing it from targeting the same pool).
4. When `A`'s `ExchangeWithdrawContract` is finally applied in `ExchangeWithdrawActuator.execute`, `anotherTokenQuant` is recomputed from the now-skewed `firstTokenBalance`/`secondTokenBalance` [8](#0-7) , and `A` receives an amount different from what was expected at signing time, with no on-chain check to reject the outcome.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L24-29)
```text
message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L31-37)
```text
message ExchangeTransactionContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
  int64 expected = 5;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L68-112)
```java
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, tokenQuant));
      } else {
        accountCapsule.addAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeWithdrawAnotherAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L65-83)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** protocol/src/main/protos/api/api.proto (L184-188)
```text
  rpc ExchangeWithdraw (ExchangeWithdrawContract) returns (TransactionExtention) {
  }

  rpc ExchangeTransaction (ExchangeTransactionContract) returns (TransactionExtention) {
  }
```
