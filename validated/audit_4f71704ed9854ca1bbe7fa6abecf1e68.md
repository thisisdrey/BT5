### Title
Missing Slippage/Minimum-Output Protection in ExchangeInject and ExchangeWithdraw Actuators Enables Front-Running of Bancor-style Liquidity Operations - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
TRON's built-in Bancor-style token exchange supports four operations: create, inject (add liquidity), withdraw (remove liquidity), and transaction (swap). The swap operation, `ExchangeTransactionContract`, includes an `expected` field that acts as a slippage guard, rejecting the transaction if the counter-token amount received is less than expected. [1](#0-0) 
However, the liquidity-management contracts `ExchangeInjectContract` and `ExchangeWithdrawContract` carry only `token_id` and `quant` — no equivalent minimum/maximum bound field exists for the calculated counterpart token amount. [2](#0-1) 

### Finding Description
`ExchangeInjectActuator` computes the required amount of the paired token (`anotherTokenQuant`) purely from the exchange's current on-chain balances at execution time, with no caller-supplied bound to protect against ratio changes between transaction submission and execution: [3](#0-2) 
The validate step similarly derives `anotherTokenQuant` from live pool state and only checks it is `> 0`, not that it stays within any range acceptable to the caller: [4](#0-3) 

`ExchangeWithdrawActuator` behaves the same way: the amount of the paired token returned to the withdrawer is computed from the current pool ratio in `execute()` and only sanity-checked for balance sufficiency and rounding precision (the "Not precise enough" check), never against a user-specified minimum: [5](#0-4) [6](#0-5) 

Both actuators restrict the caller to the exchange's creator address: [7](#0-6) 
but `ExchangeTransactionContract` (the swap) is callable by any unprivileged account, and directly mutates the same pool balances used to compute inject/withdraw ratios: [8](#0-7) 

This is the exact bug class from the external report: a Uniswap-style liquidity add/remove path that has no `amountMin`/`amountMax` protection, in contrast to a sibling swap function that does have one. Any unprivileged account can broadcast an `ExchangeTransactionContract` swap immediately before the creator's pending `ExchangeInjectContract`/`ExchangeWithdrawContract` is packed into a block, shifting `firstTokenBalance`/`secondTokenBalance` and thus the ratio used by the inject/withdraw calculation, then reverse the swap (or simply hold the manipulated position) after the inject/withdraw executes to extract value from the difference (classic sandwich attack).

### Impact Explanation
A malicious actor who observes a pending `ExchangeInjectContract` or `ExchangeWithdrawContract` transaction in the mempool can sandwich it with `ExchangeTransactionContract` swaps to manipulate the exchange's internal token ratio. Because neither inject nor withdraw enforces a minimum/maximum counterpart amount, the exchange creator can be forced to:
- Inject liquidity at a manipulated ratio, contributing a disproportionately large amount of one token for a smaller amount of the other than the fair-market ratio would dictate, or
- Withdraw at a manipulated ratio, receiving fewer tokens back than the pool's true value would otherwise provide.

The manipulator profits by reversing their swap after the inject/withdraw lands, extracting value from the exchange creator. This is a direct loss-of-funds impact matching the report's "Loss of funds due to slippage when extracting or restoring liquidity."

### Likelihood Explanation
Exploitation requires only ordinary, unprivileged `ExchangeTransactionContract` calls (which any account can submit) executed in the same block window as the victim's `ExchangeInjectContract`/`ExchangeWithdrawContract`. Transaction ordering within a block is controlled by the witness/packing node, and an attacker (or a colluding/observing party) monitoring the mempool for pending inject/withdraw transactions from a known exchange creator can front-run/back-run them with swaps. No special privilege, and no protocol upgrade or malicious SR/witness behavior, is required — a single pair of unprivileged transactions from an unprivileged account is sufficient. The likelihood is bounded slightly by the fact that only the exchange creator (not arbitrary users) can call inject/withdraw, so the attack targets a specific, identifiable account rather than any liquidity provider.

### Recommendation
Add a caller-specified bound analogous to `ExchangeTransactionContract.expected` to both `ExchangeInjectContract` and `ExchangeWithdrawContract`:
- For `ExchangeInjectContract`, add an `anotherTokenQuantMax` (or similar) field and validate that the computed `anotherTokenQuant` does not exceed it.
- For `ExchangeWithdrawContract`, add an `anotherTokenQuantMin` field and validate that the computed `anotherTokenQuant` is not less than it.

Enforce these checks in `doValidate()` of `ExchangeInjectActuator` and `ExchangeWithdrawActuator`, mirroring the existing pattern in `ExchangeTransactionActuator.doValidate()`: [9](#0-8) 

### Proof of Concept
1. Exchange creator C broadcasts `ExchangeWithdrawContract` to withdraw `tokenQuant` of `firstTokenID` from exchange E, expecting to receive `anotherTokenQuant` of `secondTokenID` computed from the current pool ratio `firstTokenBalance : secondTokenBalance`.
2. Attacker A observes this pending transaction and, before it is packed, submits an `ExchangeTransactionContract` swap against exchange E that shifts `firstTokenBalance`/`secondTokenBalance` unfavorably for C (e.g., inflates `firstTokenBalance` relative to `secondTokenBalance`).
3. When C's `ExchangeWithdrawContract` executes, `anotherTokenQuant` is recalculated in `execute()` using the now-manipulated balances [5](#0-4) , causing C to receive materially less `secondTokenID` than the pre-manipulation ratio implied, with no check to reject the reduced payout.
4. Attacker A then reverses their swap via a second `ExchangeTransactionContract`, restoring the pool ratio and capturing the value difference extracted from C's withdrawal.
5. The identical pattern applies to `ExchangeInjectContract`, where C can be forced to contribute a disadvantageous amount of the paired token when injecting liquidity.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-236)
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

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-91)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
