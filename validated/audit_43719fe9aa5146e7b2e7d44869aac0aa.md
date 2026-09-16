### Title
No Slippage Protection When Injecting or Withdrawing Liquidity in TRC10 Exchange Pools - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
The `ExchangeInjectContract` and `ExchangeWithdrawContract` messages, which add and remove liquidity from a TRC10 bancor-style exchange pool, carry no minimum/expected-output (slippage) parameter, unlike `ExchangeTransactionContract` (a trade), which explicitly carries an `expected` field checked at validation time. This omission lets the pool's exchange ratio move between transaction signing and block inclusion, causing the liquidity provider/withdrawer to receive an amount of the paired token that is worse than intended, with no on-chain guard to reject it.

### Finding Description
`ExchangeTransactionContract` (a swap) has an `expected` field [1](#0-0)  that is enforced in `ExchangeTransactionActuator.doValidate()`: the actuator computes `anotherTokenQuant` from the current pool state and rejects the transaction if it's below the caller-supplied `tokenExpected` [2](#0-1) .

In contrast, `ExchangeInjectContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — no expected/minimum counterpart amount [3](#0-2) . `ExchangeInjectActuator.execute()` computes `anotherTokenQuant` purely from the pool's balances at execution time (`floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)`) and unconditionally debits that computed amount from the caller's account with no way for the caller to bound how much of the second asset they are willing to contribute [4](#0-3) .

Likewise, `ExchangeWithdrawContract` has the identical shape — no minimum-received parameter [5](#0-4)  — and `ExchangeWithdrawActuator.execute()` computes `anotherTokenQuant` from the live pool ratio and credits it to the caller with no floor on the amount they actually receive [6](#0-5) . The only checks in `ExchangeWithdrawActuator.doValidate()` are that the computed amount is positive and "precise enough" relative to the ratio at validation time, not a caller-specified bound guaranteeing an acceptable ratio [7](#0-6) .

Because any account can submit `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions, and pool ratios are moved by ordinary `ExchangeTransactionContract` swaps within the same or adjacent blocks (which any account can also submit), an attacker who can influence transaction ordering within a block (e.g. an SR/witness packing the block) can sandwich a victim's inject/withdraw transaction: swap the pool ratio unfavorably before the victim's inject/withdraw executes, let the victim's actuator compute `anotherTokenQuant` at the manipulated ratio, then swap back for profit. This mirrors exactly the reported Aloe `Borrower.sol` `modify` issue, where Uniswap `mint`/`burn` calls lack `amount0Min`/`amount1Min` slippage bounds while the swap path does have them.

### Impact Explanation
A liquidity provider calling `ExchangeInjectContract` can be forced to contribute an inflated amount of the second token relative to fair value, or a party calling `ExchangeWithdrawContract` can receive less of the paired token than the pool's fair ratio would imply at submission time — both resulting in a direct, unrecoverable loss of TRX/TRC10 asset value to whoever manipulated the ratio in between. This is a concrete unauthorized value transfer/theft-of-funds vector reachable by any account issuing ordinary transactions, satisfying the "unauthorized account operation / theft of funds" bar.

### Likelihood Explanation
Exploitation requires the ability to place a swap transaction before the victim's inject/withdraw transaction and another swap after it within the same block (or across adjacent blocks under SR/witness control of ordering), which is a standard sandwich pattern achievable by anyone able to submit multiple transactions with appropriately chosen fees/ordering, or trivially by block producers. The `ExchangeTransactionActuator`'s own `expected` field demonstrates the protocol authors recognized the need for slippage protection for swaps but did not extend the same protection to inject/withdraw operations, making this a straightforward, reachable gap rather than a theoretical one.

### Recommendation
Add a minimum-received (for withdraw) / maximum-required (for inject) parameter to `ExchangeInjectContract` and `ExchangeWithdrawContract`, analogous to the existing `expected` field in `ExchangeTransactionContract`, and enforce it in `ExchangeInjectActuator.doValidate()`/`execute()` and `ExchangeWithdrawActuator.doValidate()`/`execute()` by rejecting the transaction if the computed `anotherTokenQuant` falls outside the caller-specified bound.

### Proof of Concept
1. Pool holds `firstTokenBalance` = 100,000,000 and `secondTokenBalance` = 200,000,000.
2. Victim submits `ExchangeInjectContract` intending to inject `firstTokenQuant` at the current ~1:2 ratio, expecting to pay ~2x in `secondTokenId`.
3. Before the victim's transaction is applied, an attacker submits an `ExchangeTransactionContract` swap that skews the ratio heavily (e.g., dumps `secondTokenId` into the pool), as computed in `ExchangeCapsule.transaction` and applied via `Commons.putExchangeCapsule` [8](#0-7) .
4. Victim's `ExchangeInjectActuator.execute()` now computes `anotherTokenQuant` off the skewed ratio, per `floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` [9](#0-8) , forcing the victim to pay far more `secondTokenId` than intended, with no `expected`/minimum field in the contract to reject this [3](#0-2) .
5. Attacker submits a reverse swap afterward to restore the ratio and pocket the difference, completing the sandwich — with no on-chain guard anywhere in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` preventing this, unlike `ExchangeTransactionActuator`'s `tokenExpected` check.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-22)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L93-98)
```java
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L65-99)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-104)
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
