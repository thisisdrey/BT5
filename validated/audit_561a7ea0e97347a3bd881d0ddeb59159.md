Confirmed: `ExchangeTransactionActuator.execute` calls `accountCapsule.setBalance(newBalance)` unconditionally at line 78 (so the fee is always applied), but `ExchangeWithdrawActuator.execute` never calls `accountCapsule.setBalance(newBalance)` unconditionally — it only applies `newBalance` when `tokenID` or `anotherTokenID` equals `TRX_SYMBOL_BYTES` (lines 91-104). Since `ExchangeCreateActuator.doValidate()` does not require either token of a pair to be TRX (it only rejects `firstTokenID == secondTokenID`, allowing two arbitrary TRC10 tokens), an exchange pair with neither token being TRX can legitimately exist. Withdrawing from such a pair executes `ExchangeWithdrawActuator` with no code path ever invoking `accountCapsule.setBalance(...)`, so `newBalance` (balance minus `calcFee()`) is computed but discarded, and the account's actual balance persisted via `accountStore.put(...)` is unchanged — the exchange withdraw fee is silently never charged.

### Title
Exchange withdraw fee silently bypassed for non-TRX token pairs - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeWithdrawActuator.execute` computes the withdraw fee into a local `newBalance` variable but only ever writes it back to the account via `accountCapsule.setBalance(...)` inside the `Arrays.equals(tokenID, TRX_SYMBOL_BYTES)` / `Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)` branches. For an exchange pair where neither token is TRX (which `ExchangeCreateActuator` allows to be created), neither branch executes, so the fee deduction is never persisted, letting any account withdraw from such a pair for free, repeatedly.

### Finding Description
`ExchangeWithdrawActuator.execute` computes: [1](#0-0) 
`newBalance = subtractExact(accountCapsule.getBalance(), calcFee())`, and only calls `accountCapsule.setBalance(addExact(newBalance, tokenQuant/anotherTokenQuant))` inside the two `TRX_SYMBOL_BYTES` equality checks. If both `tokenID` and `anotherTokenID` resolve to non-TRX assets, both credits go through the `addAssetAmountV2` else-branches instead, and `accountCapsule.setBalance` is never invoked — leaving `accountCapsule.getBalance()` at its pre-fee value when persisted via `accountStore.put(...)`.

This is reachable because `ExchangeCreateActuator.doValidate` only forbids `firstTokenID == secondTokenID`; it does not require either token to be `TRX_SYMBOL_BYTES`: [2](#0-1) 
So any unprivileged account can create a TRC10/TRC10 exchange pair, then repeatedly submit `ExchangeWithdrawContract` transactions against it, always taking the non-TRX branch and never paying `calcFee()` (`getExchangeWithdrawFee()`).

By contrast, the sibling actuator `ExchangeTransactionActuator.execute` applies the fee unconditionally by calling `accountCapsule.setBalance(newBalance)` before the token-specific branching: [3](#0-2) 
confirming the withdraw actuator's omission of that unconditional `setBalance(newBalance)` call is the root-cause divergence/bug, structurally analogous to the reported "missing mint/credit step" pattern — here a required balance-debit step is skipped under a specific reachable condition.

### Impact Explanation
An attacker who creates (or uses) an exchange pair composed of two non-TRX TRC10 tokens can invoke `ExchangeWithdrawContract` (and the pattern generalizes to any repeated withdraw calls) without ever paying the configured exchange withdraw fee, since the fee subtraction is computed but discarded. This lets an unprivileged transaction broadcaster perform free, unlimited exchange-withdraw operations, which normally cost TRX and are meant to be burned/sent to the blackhole to compensate for the resource/state consumed. This constitutes an unbacked-economics condition: state-mutating operations proceed without the network fee being collected, undermining the fee/resource accounting model the chain relies on for all TRC10 exchange operations.

### Likelihood Explanation
High likelihood of triggering: any account can call `ExchangeCreateContract` to create a TRC10/TRC10 pair (no special permission required), then call `ExchangeWithdrawContract` on it. This requires no privileged role, no race condition, and no unusual chain state — only a token pair with neither leg being TRX, which is explicitly permitted by `ExchangeCreateActuator`'s validation logic.

### Recommendation
In `ExchangeWithdrawActuator.execute`, apply `accountCapsule.setBalance(newBalance)` unconditionally immediately after computing `newBalance`, before the `tokenID`/`anotherTokenID` branching — mirroring the pattern already used in `ExchangeTransactionActuator.execute` (`accountCapsule.setBalance(newBalance);` at line 78) — so the withdraw fee is always deducted regardless of which tokens are involved in the exchange pair.

### Proof of Concept
1. Attacker account A calls `ExchangeCreateContract` with `firstTokenId = TOKEN_X`, `secondTokenId = TOKEN_Y` (both non-TRX TRC10 tokens A owns), satisfying `ExchangeCreateActuator.doValidate` (no TRX requirement, only `firstTokenID != secondTokenID`).
2. Account A (or any account holding a matching order/balance) calls `ExchangeWithdrawContract` on this exchange with `tokenId = TOKEN_X`.
3. In `ExchangeWithdrawActuator.execute`, `tokenID = TOKEN_X`, `anotherTokenID = TOKEN_Y` — neither equals `TRX_SYMBOL_BYTES`, so both credit branches take the `addAssetAmountV2` path; `accountCapsule.setBalance(...)` is never called.
4. `accountStore.put(accountCapsule.createDbKey(), accountCapsule)` persists the account with its balance unchanged (fee not deducted), while `ret.setStatus(fee, code.SUCESS)` reports the transaction as successful and having charged `fee`.
5. Repeating step 2 indefinitely lets the attacker perform unlimited withdraw operations on non-TRX exchange pairs without ever paying `calcFee()` (`getExchangeWithdrawFee()`).

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L91-104)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L188-203)
```java
    if (dynamicStore.getAllowSameTokenName() == 1) {
      if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES) && !isNumber(firstTokenID)) {
        throw new ContractValidateException("first token id is not a valid number");
      }
      if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES) && !isNumber(secondTokenID)) {
        throw new ContractValidateException("second token id is not a valid number");
      }
    }

    if (Arrays.equals(firstTokenID, secondTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }

    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L77-91)
```java
      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```
