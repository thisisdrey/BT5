### Title
Missing slippage control in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` allows front-running/sandwich attacks - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
TRON's on-chain Bancor-style AMM (`ExchangeInjectContract`/`ExchangeWithdrawContract`) computes the "paired" token amount (`anotherTokenQuant`) required/returned entirely from the exchange's live reserve ratio at execution time, with no caller-supplied minimum/maximum bound. This is the exact bug class described in the external report for `AlgebraPool.mint`/`burn`: `liquidityDesired`/`amountRequired` are used only to compute output, never to bound it. In contrast, java-tron's own `ExchangeTransactionContract` (the swap path) *does* carry an `expected` field that is explicitly checked, showing the project is aware of the need for slippage protection but omitted it for inject/withdraw.

### Finding Description
`ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `exchange_id`, `token_id`, and `quant` — no expected/min/max parameter for the second token: [1](#0-0) 

In `ExchangeInjectActuator.doValidate`, `anotherTokenQuant` (the amount of the second token the creator must also deposit) is derived purely from the current pool ratio and only bounded by a balance-limit and non-negativity check — there is no comparison against a caller-supplied minimum/maximum: [2](#0-1) 

Likewise, `ExchangeWithdrawActuator.doValidate` computes `anotherTokenQuant` (the amount of the second token the creator receives back) purely from the live ratio, with checks only for balance sufficiency and rounding precision, not for a caller-specified floor: [3](#0-2) 

This is precisely analogous to the reported AlgebraPool issue where `mint`/`burn` use `liquidityDesired`/`amountRequired` only to calculate output but never to enforce a slippage bound. By contrast, java-tron's swap path (`ExchangeTransactionContract`) explicitly includes an `expected` field and enforces it: [4](#0-3) 

This shows the omission in inject/withdraw is an inconsistency/gap rather than an intentional design choice.

### Impact Explanation
An exchange's creator (the only account authorized to inject/withdraw, enforced at [5](#0-4)  and [6](#0-5) ) broadcasts an `ExchangeInjectContract`/`ExchangeWithdrawContract` expecting a certain paired-token amount based on the reserve ratio observed off-chain. Since exchange creation itself is permissionless (any account can call `ExchangeCreateActuator`), any unprivileged actor can become an exchange creator and be exposed to this. Any other unprivileged account (or the creator's own transaction being reordered) can submit `ExchangeTransactionContract` swaps in the same block window to shift the pool ratio before the inject/withdraw executes:
- For inject: the attacker can shift the ratio so the creator's fixed `tokenQuant` deposit forces a much larger-than-expected `anotherTokenQuant` deduction from the creator's second-token balance.
- For withdraw: the attacker can shift the ratio so the creator receives a much smaller-than-expected `anotherTokenQuant` for the same withdrawn `tokenQuant`.

This is a concrete, unauthorized loss of funds via sandwich/front-running against a broadcastable transaction type, matching the "unauthorized account operation / theft of funds" bar.

### Likelihood Explanation
Any account can create an exchange (permissionless) and then must inject/withdraw liquidity, exposing itself to this gap. Any other account can freely submit `ExchangeTransactionContract` swaps against the same exchange to manipulate the ratio before the victim's inject/withdraw lands, requiring only ordinary balances and no special privilege — the same MEV/front-running conditions described in the source report apply directly to block producers or ordinary transaction submitters racing to be included ahead of the victim's transaction.

### Recommendation
Add explicit slippage-bound fields to `ExchangeInjectContract` and `ExchangeWithdrawContract` (e.g., `expected` minimum for inject cost cap / withdrawal minimum receive), and enforce them in `ExchangeInjectActuator.doValidate` / `ExchangeWithdrawActuator.doValidate` the same way `ExchangeTransactionActuator` already enforces `tokenExpected` at [4](#0-3) .

### Proof of Concept
1. Attacker (or exchange creator, if attacker uses a different account) observes the target exchange has reserves `firstTokenBalance = A`, `secondTokenBalance = B`.
2. Creator broadcasts `ExchangeInjectContract{exchangeId, tokenId=first, quant=Q}` expecting `anotherTokenQuant ≈ B*Q/A` of the second token to be deducted.
3. Before this transaction is packed, attacker broadcasts an `ExchangeTransactionContract` swap against the same exchange that shifts the ratio so `secondTokenBalance` rises relative to `firstTokenBalance` (e.g., attacker sells the first token into the pool).
4. When the creator's inject transaction executes, `ExchangeInjectActuator.execute` recomputes `anotherTokenQuant` at [7](#0-6)  using the now-manipulated ratio, deducting a much larger amount of the second token from the creator than intended, with no check to reject execution — exactly as the external report describes for `AlgebraPool.mint`/`burn` lacking slippage enforcement.
5. Attacker reverses the swap afterward to restore the ratio and pocket the difference, completing a sandwich attack.

### Citations

**File:** Tron protobuf protocol document.md (L1384-1420)
```markdown
     - message `ExchangeInjectContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to inject.
    
       `quant`: token amount to inject.
    
      ```java
      message ExchangeInjectContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
    
     - message `ExchangeWithdrawContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to withdraw.
    
       `quant`: token amount to withdraw.
    
      ```java
      message ExchangeWithdrawContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-231)
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

    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-243)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
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
