## Title
Missing slippage protection in `ExchangeWithdrawContract` allows front-running of liquidity withdrawal from Bancor-style exchange pools - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
java-tron's built-in Bancor-style AMM ("Exchange") lets a pool creator withdraw liquidity via `ExchangeWithdrawContract`. Unlike `ExchangeTransactionContract` (the swap contract), which includes an `expected` field enforcing a minimum-received check, `ExchangeWithdrawContract` has no equivalent slippage-protection field. The amount of the "other" token returned on withdrawal is computed proportionally from the exchange's *current* token balances at execution time, so it can be manipulated by a swap sandwiched immediately before the withdrawal transaction is packed into a block.

### Finding Description
`ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — no minimum-expected-amount field for the counter-asset: [1](#0-0) 

Contrast this with `ExchangeTransactionContract`, which explicitly documents and implements an `expected` minimum-amount safeguard: [2](#0-1) [3](#0-2) 

In `ExchangeWithdrawActuator.execute`, the counter-token quantity (`anotherTokenQuant`) returned to the creator is derived purely from the exchange's live `firstTokenBalance`/`secondTokenBalance` ratio at execution time, with no bound the caller can enforce: [4](#0-3) 

`doValidate()` in the same actuator confirms there is no `expected`/minimum check analogous to the one in `ExchangeTransactionActuator` — it only checks precision/rounding tolerance ("Not precise enough"), not a user-supplied floor: [5](#0-4) 

Any account holding a balance of either token in the pool can broadcast an `ExchangeTransactionContract` (swap) against the same `exchange_id`, which mutates `firstTokenBalance`/`secondTokenBalance` via the Bancor formula in `ExchangeCapsule.transaction`, executed by `ExchangeTransactionActuator.execute`: [6](#0-5) 

This is the exact bug class described in the external report: the withdrawer cannot express a minimum acceptable amount of the counter-asset, so an attacker who observes the pending `ExchangeWithdrawContract` in the mempool can submit a large swap beforehand (and reverse it afterward within the same block window/arbitrage cycle) to shift the pool ratio unfavorably, capturing value from the withdrawing creator — mirroring the missing `amount0Min`/`amount1Min` check in Uniswap's liquidity-removal path.

### Impact Explanation
The exchange creator (an asset issuer who created the trading pair) can be sandwiched: their withdrawal executes against a manipulated balance ratio and returns a much smaller amount of the counter-token than the pool state implied when they signed the transaction, resulting in a quantifiable, unrecoverable loss of funds. This matches the "theft of funds" bar since the loss is directly transferable to the attacker via the sandwich swap.

### Likelihood Explanation
Any unprivileged account holding a balance of either token in the exchange can broadcast `ExchangeTransactionContract` to shift the ratio; exploitation requires only mempool visibility/front-running capability and no special privilege, no fork-height gating, and no cooperation from the victim beyond issuing a normal withdrawal. `ExchangeWithdrawContract` is restricted to the pool creator, but exchange creation is permissionless (any account can create an exchange and later withdraw), so this affects a broad class of ordinary users.

### Recommendation
Add a caller-specified minimum-expected-amount field (analogous to `expected` in `ExchangeTransactionContract`) to `ExchangeWithdrawContract`, and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()` by reverting when the computed `anotherTokenQuant` (and/or `tokenQuant` returned) falls below the caller's specified floor, mirroring the check already present in `ExchangeTransactionActuator`.

### Proof of Concept
1. Account A creates an exchange pool with tokens X/Y and later wants to withdraw liquidity via `ExchangeWithdrawContract` (`token_id = X`, `quant = N`).
2. Attacker observes A's pending withdraw transaction and, in the same or an earlier block, submits `ExchangeTransactionContract` swaps against the pool to skew `firstTokenBalance`/`secondTokenBalance` so that the Y amount computed for A's withdrawal (`anotherTokenQuant` in `ExchangeWithdrawActuator.execute`, lines 77-89) is minimized.
3. A's `ExchangeWithdrawContract` executes with no way to enforce a floor on the Y amount received (no `expected` field exists), unlike the analogous swap path.
4. Attacker reverses the swap afterward, extracting the value difference from the imbalance, while A receives far less token Y than the pool ratio implied when they signed the transaction.

### Citations

**File:** Tron protobuf protocol document.md (L1403-1420)
```markdown
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

**File:** Tron protobuf protocol document.md (L1422-1442)
```markdown
     - message `ExchangeTransactionContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to sell.
    
       `quant`: token amount to sell.
    
       `expected`: expected minimum number of tokens.
    
      ```java
      message ExchangeTransactionContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
          int64 expected = 5;
      }
      ```
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-96)
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

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L63-89)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-244)
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
