### Title
Missing Slippage Protection in `ExchangeInjectContract` and `ExchangeWithdrawContract` Allows Frontrunning of TRC10 Liquidity Operations - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java], [File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator`, which add and remove liquidity from java-tron's built-in TRC10 bancor-style `Exchange` pools, compute the paired-token amount (`anotherTokenQuant`) purely from the pool's *current on-chain* token ratio at execution time. Unlike `ExchangeTransactionActuator` (the swap actuator), which enforces a caller-supplied `expected` minimum output [1](#0-0) , neither the inject nor the withdraw contract exposes any minimum/maximum bound parameter, so the pool ratio can be manipulated between transaction submission and execution.

### Finding Description
`ExchangeInjectActuator.execute`/`doValidate` derives `anotherTokenQuant` proportionally from `firstTokenBalance`/`secondTokenBalance` at execution time, with no user-supplied bound on the resulting ratio: [2](#0-1) 

Similarly, `ExchangeWithdrawActuator.doValidate` computes `anotherTokenQuant` from the live pool ratio and only checks internal rounding precision ("Not precise enough"), not any caller-specified min/max: [3](#0-2) 

By contrast, `ExchangeTransactionActuator` (swaps) requires a `tokenExpected` field and rejects the trade if the computed output is below it: [4](#0-3) 

Any account can permissionlessly move the pool ratio by calling `ExchangeTransactionContract` (a public, unprivileged swap) immediately before a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction from the exchange's creator is applied in the same block, since Manager applies transactions in the order they land in the block being built. Because inject/withdraw have no slippage floor/ceiling, the creator's transaction executes at the attacker-manipulated ratio, exactly the same bug class described in the external report for Uniswap-style `amount0Min`/`amount1Min` being zero.

### Impact Explanation
An attacker who front-runs a pending `ExchangeInjectContract` or `ExchangeWithdrawContract` can force the exchange creator to deposit a disproportionately large amount of the paired token (inject) or receive a disproportionately small amount of the paired token (withdraw), extracting value from the creator's account in a single block via ordinary swap transactions. This is a concrete loss-of-funds condition for the exchange creator, reachable by any unprivileged account issuing standard `ExchangeTransactionContract` transactions — no special privileges required.

### Likelihood Explanation
Any account that creates a TRC10 `Exchange` (via `ExchangeCreateContract`) and later injects/withdraws liquidity is exposed; the counterparty need only observe the mempool for pending inject/withdraw transactions and race a swap ahead of them, which is straightforward MEV/frontrunning behavior achievable by any network participant with a broadcastable transaction.

### Recommendation
Add explicit slippage-bound fields (e.g., `expected`/`min_another_token`, analogous to the existing `expected` field in `ExchangeTransactionContract`) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and validate the computed `anotherTokenQuant` against that caller-supplied bound in `ExchangeInjectActuator.doValidate` and `ExchangeWithdrawActuator.doValidate`, rejecting the transaction if the ratio has moved unfavorably beyond the caller's tolerance.

### Proof of Concept
1. User A creates a TRC10 exchange pool with tokens X/Y and is the pool creator.
2. User A broadcasts an `ExchangeInjectContract` to add liquidity proportional to the current pool ratio.
3. Attacker B observes the pending transaction and broadcasts an `ExchangeTransactionContract` swap that shifts the X/Y ratio, ensuring it is included and executed before A's inject transaction in the same block.
4. When A's `ExchangeInjectActuator.execute` runs, `anotherTokenQuant` is computed against the manipulated ratio (lines 71-83 of `ExchangeInjectActuator.java`), causing A to deposit far more of the paired token than intended, with no `expected`/min check to reject the unfavorable execution.
5. Attacker B can then reverse the swap (or simply keep the extracted value), profiting at User A's expense — the same slippage exploitation pattern described in the referenced Uniswap report, but reachable here via ordinary TRC10 `Exchange` transactions.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L173-221)
```java
    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();
    long tokenExpected = contract.getExpected();

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }
    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("token quant must greater than zero");
    }

    if (tokenExpected <= 0) {
      throw new ContractValidateException("token expected must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
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

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-243)
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
