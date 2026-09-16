Confirmed: any TRX-holding account can permissionlessly create a liquidity pool via `ExchangeCreateActuator` [1](#0-0) , becoming the pool creator who is the only account authorized to call `ExchangeWithdrawContract` [2](#0-1) . No special/SR privilege is required, so this is reachable by an ordinary asset-issuer/order-placer via a signed transaction.

### Title
Missing slippage protection (`minOut`/`expected`) in `ExchangeWithdrawContract` allows AMM liquidity withdrawals to be sandwiched for value extraction - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract`/`ExchangeWithdrawActuator` lets the creator of a Bancor-style TRC10 exchange pool withdraw liquidity proportionally to the current pool ratio, but the contract has no user-supplied minimum-output parameter, unlike the sibling `ExchangeTransactionContract` (swap) which explicitly carries an `expected` field for exactly this purpose.

### Finding Description
`ExchangeTransactionContract` (the AMM swap operation) includes an `expected` field that is validated against the actual computed output before execution, protecting the caller from adverse price movement between signing and inclusion: [3](#0-2)  and enforced in `ExchangeTransactionActuator.doValidate()`: [4](#0-3) .

By contrast, `ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — there is no `expected`/`minOut` field: [5](#0-4) . In `ExchangeWithdrawActuator.doValidate()`/`execute()`, the paired-token amount the withdrawer receives (`anotherTokenQuant`) is computed strictly from the pool balances *at execution time* using the current ratio, with only a "precision" sanity check (`allowHarden`) but no user-specified floor: [6](#0-5)  and the same unconstrained computation is repeated in `execute()`: [7](#0-6) .

Because block/transaction ordering is not controlled by the withdrawer, an attacker (or any other actor, including the exchange creator's own front-runner) can submit `ExchangeInjectContract` or `ExchangeTransactionContract` transactions against the same `exchange_id` that get included immediately before the pending `ExchangeWithdrawContract` transaction in the same block, skewing `firstTokenBalance`/`secondTokenBalance` so the withdrawer receives a much smaller `anotherTokenQuant` than they expected when they signed the transaction — with no on-chain mechanism to reject the unfavorable outcome.

### Impact Explanation
This directly parallels the reported bug class: a withdraw-type operation lacking a `minOut`/slippage-floor parameter that a paired swap operation in the same subsystem does have. The exchange creator can be forced to accept an arbitrarily worse token distribution on withdrawal, resulting in concrete loss of funds (fewer tokens received than the pool state implied when the transaction was signed). This is a Medium-severity value-extraction/fund-loss issue reachable purely by transactions any account can broadcast (no SR/witness/malicious-node privilege needed).

### Likelihood Explanation
Likelihood is moderate: it requires an adversary (or MEV-seeking party) to observe a pending `ExchangeWithdrawContract` transaction and land a same-block `ExchangeTransactionContract`/`ExchangeInjectContract` transaction immediately before it that shifts the pool ratio unfavorably. This is a standard sandwich-attack pattern against AMM-style pools and does not require any special network position beyond normal mempool visibility and the ability to pay a transaction fee.

### Recommendation
Add an `expected`/`min_first_token_amount` and `min_second_token_amount` (or a single `minOut`) field to `ExchangeWithdrawContract`, and validate the computed `anotherTokenQuant` (and/or `tokenQuant` returned) against it in `ExchangeWithdrawActuator.doValidate()`/`execute()`, mirroring the existing protection already implemented in `ExchangeTransactionActuator`.

### Proof of Concept
1. Attacker monitors the mempool for a pending `ExchangeWithdrawContract` from a pool creator on `exchange_id = X` withdrawing `tokenQuant` of `firstTokenID`.
2. Attacker submits an `ExchangeTransactionContract` (or `ExchangeInjectContract`) against the same `exchange_id` with a higher priority/energy price so it lands first in the block, shifting `firstTokenBalance`/`secondTokenBalance` unfavorably (e.g., pumping `firstTokenBalance` relative to `secondTokenBalance`).
3. When the victim's `ExchangeWithdrawContract` executes, `ExchangeWithdrawActuator` recomputes `anotherTokenQuant = secondTokenBalance * tokenQuant / firstTokenBalance` using the now-skewed balances: [8](#0-7) , yielding a smaller `anotherTokenQuant` than the victim expected, with no `expected` field to reject the trade.
4. The victim's transaction still succeeds (no revert), permanently locking in the loss.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L36-53)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    ExchangeStore exchangeStore = chainBaseManager.getExchangeStore();
    ExchangeV2Store exchangeV2Store = chainBaseManager.getExchangeV2Store();
    try {
      final ExchangeCreateContract exchangeCreateContract = this.any
          .unpack(ExchangeCreateContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeCreateContract.getOwnerAddress().toByteArray());
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
