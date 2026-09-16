### Title
Missing slippage protection on `ExchangeWithdraw` allows unprivileged front-running/sandwich attacks that reduce the exchange creator's returned token amount - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeTransactionContract` — TRON's Bancor-style TRC10 AMM swap — protects the trader with an explicit `expected` minimum-output check enforced in `ExchangeTransactionActuator`. However, `ExchangeWithdrawContract`, which lets the exchange creator withdraw liquidity at the current pool ratio, has no equivalent minimum-output/slippage field or check, exposing the creator to loss of funds when the pool ratio is manipulated between transaction submission and execution — the same root-cause class as the reported "no slip price check" oracle issue (executing a trade against a price that can move before/at execution with no floor guaranteed to the beneficiary).

### Finding Description
`ExchangeTransactionContract` carries an `expected` field, and `ExchangeTransactionActuator.doValidate()` explicitly reverts if the computed output is below it: [1](#0-0) 

In contrast, `ExchangeWithdrawContract` only has `owner_address`, `exchange_id`, `token_id`, `quant` — no minimum-expected-amount field at all, confirmed via the protocol doc: [2](#0-1) 

`ExchangeWithdrawActuator.doValidate()` computes `anotherTokenQuant` purely from the current on-chain reserve ratio (`bigSecondTokenBalance.multiply(bigTokenQuant).divideToIntegralValue(bigFirstTokenBalance)` or the symmetric case) at validation time, with no user-supplied floor to protect against ratio movement: [3](#0-2) 

`ExchangeWithdrawActuator.execute()` then re-derives the balances and pays out `anotherTokenQuant` unconditionally: [4](#0-3) 

Because `ExchangeTransactionContract` (a normal, unprivileged, broadcastable transaction) can be submitted by any account to swap and shift `firstTokenBalance`/`secondTokenBalance` of the same exchange pair immediately before the creator's withdraw transaction executes, an attacker can sandwich the withdraw: swap to skew the ratio, let the withdraw be processed at the skewed ratio (receiving less of the token whose reserve was inflated), then swap back to restore the ratio and capture the difference. This mirrors the reported bug class exactly — the value paid out is derived from a price that is not protected by any beneficiary-set minimum, so it can be manipulated to reduce what the beneficiary (here, the exchange creator) receives.

### Impact Explanation
The exchange creator withdrawing liquidity can receive materially less of one token than the pool's "fair" ratio implies, while the attacker profits the difference — a direct loss of funds for the withdrawer, analogous to the "Swapper owner receives less token" impact in the source report. Since `ExchangeWithdrawContract` requires no special privilege from the attacker (any account can submit `ExchangeTransactionContract`), this is exploitable by an ordinary unprivileged transaction broadcaster.

### Likelihood Explanation
Exploitation requires only ordinary `ExchangeTransactionContract` broadcasts around a known/observable `ExchangeWithdrawContract` transaction in the mempool — no special node, SR, or protocol privilege is needed. The attack is a standard sandwich pattern and is economically motivated whenever the exchange pool is large enough relative to the withdrawal to make the ratio shift profitable, making likelihood moderate to high for actively used TRC10 Bancor exchanges.

### Recommendation
Add an `expected`/minimum-received field to `ExchangeWithdrawContract` (mirroring `ExchangeTransactionContract.expected`), and have `ExchangeWithdrawActuator.doValidate()`/`execute()` reject the transaction if the computed `anotherTokenQuant` falls below the caller-specified floor, exactly as is already done for `ExchangeTransactionActuator`.

### Proof of Concept
1. Exchange creator broadcasts `ExchangeWithdrawContract` to withdraw `tokenQuant` of `firstTokenID`, expecting `anotherTokenQuant ≈ secondTokenBalance * tokenQuant / firstTokenBalance` based on the pool state they observed.
2. Before this transaction is packed/executed, an attacker broadcasts an `ExchangeTransactionContract` selling a large amount of `secondTokenID` into the same exchange, inflating `secondTokenBalance` and deflating the resulting ratio the withdraw will see (or vice versa depending on direction).
3. The withdraw executes per `ExchangeWithdrawActuator.execute()`/`doValidate()` at the now-skewed ratio, since there is no `expected` check, and the creator receives less `secondTokenID` than intended: [5](#0-4) 
4. The attacker then submits a reverse `ExchangeTransactionContract` to restore the pool ratio, capturing the value difference extracted from the creator's withdrawal.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L63-112)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L185-227)
```java
    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();

    long anotherTokenQuant;

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }

    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("withdraw token quant must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

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
```
