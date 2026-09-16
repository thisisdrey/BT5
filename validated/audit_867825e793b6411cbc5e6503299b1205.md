## Title
No slippage/minimum-output protection on `ExchangeInjectContract` / `ExchangeWithdrawContract` allows sandwich-style value extraction from TRC10 Bancor-style exchanges - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
java-tron's built-in TRC10 "Exchange" (a Bancor-style constant-product-like AMM) has three interaction types: `ExchangeTransactionContract` (swap), `ExchangeInjectContract` (add liquidity) and `ExchangeWithdrawContract` (remove liquidity). Only the swap contract carries a caller-supplied minimum-output field (`expected`), which is enforced in `ExchangeTransactionActuator`. The liquidity-inject and liquidity-withdraw contracts have **no equivalent minimum/slippage field at all**, so the actuators compute the counterpart token amount purely from whatever the pool ratio happens to be at execution time.

### Finding Description
`ExchangeTransactionContract` includes an `expected` field [1](#0-0) , and `ExchangeTransactionActuator.doValidate()` rejects the transaction if the actual output is below that caller-specified minimum: `if (anotherTokenQuant < tokenExpected) { throw ... "token required must greater than expected"; }` [2](#0-1) .

In contrast, `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `owner_address`, `exchange_id`, `token_id`, and `quant` — no expected/minimum field exists [3](#0-2) .

`ExchangeInjectActuator.execute()` computes the paired token amount to be pulled from the caller purely from the live pool balances at execution time (`anotherTokenQuant = floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` or the mirrored branch), with no upper bound the caller can enforce [4](#0-3) . The validate path recomputes the same ratio-based value and only checks it's `> 0` and that the account can afford it — never that it matches caller expectations [5](#0-4) .

Similarly, `ExchangeWithdrawActuator.execute()` computes the amount of the *other* token returned to the withdrawing account purely from the live pool ratio at execution time, with no caller-supplied floor: `anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)...` [6](#0-5) . The only sanity checks in validate concern arithmetic precision ("Not precise enough"), not a caller-defined minimum acceptable receive amount [7](#0-6) .

Because the exchange pool ratio (`firstTokenBalance`/`secondTokenBalance`) can be moved arbitrarily within the same block by an attacker broadcasting an `ExchangeTransactionContract` swap immediately before the victim's inject/withdraw transaction is packed (both are ordinary broadcastable transaction types with no ordering guarantee for the victim), a witness/attacker (or any user racing transactions) can force the pool into a temporarily skewed ratio, causing:
- On `ExchangeWithdrawContract`: the LP-creator (the only account authorized to inject/withdraw, per `!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())` check) receives far less of the paired token than the pool's un-manipulated ratio would have given.
- On `ExchangeInjectContract`: the creator is forced to contribute a disproportionately large amount of the paired token to maintain the (manipulated) ratio, effectively donating value to the attacker who reverses their swap afterward.

This is the direct analog of the reported AMM issue: `amount0Min`/`amount1Min` hardcoded to zero on Uniswap `addLiquidity`/`removeLiquidity`/`swap` calls exposes the caller to slippage/sandwich loss; here, `ExchangeInjectContract`/`ExchangeWithdrawContract` structurally have no such minimum field at all, exposing the sole authorized creator account to the same class of loss on every inject/withdraw call.

### Impact Explanation
An attacker who can predict or induce ordering of transactions in a block (e.g. by broadcasting a swap in the same block, or via block producers reordering their own included transactions) can cause the exchange creator to withdraw/inject at a manipulated price, resulting in a real, unbacked loss of TRX/TRC10 token value for the victim in a single block — a concrete unauthorized value transfer between accounts, not merely a la resource-only issue.

### Likelihood Explanation
Exploitation requires only two ordinary signed transactions in the same block: an `ExchangeTransactionContract` swap followed by the victim's pre-existing `ExchangeInjectContract`/`ExchangeWithdrawContract`. No special privilege is needed beyond being any transaction broadcaster; a malicious/opportunistic actor (or even a super representative reordering transactions it packs) can trigger this whenever they observe a pending inject/withdraw transaction in the mempool, making likelihood Medium.

### Recommendation
Add a caller-specified minimum-amount field (analogous to `expected` in `ExchangeTransactionContract`) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce it in `ExchangeInjectActuator.doValidate()` / `ExchangeWithdrawActuator.doValidate()` by rejecting the transaction when the computed `anotherTokenQuant` (inject: max token required; withdraw: min token returned) violates the caller's bound, mirroring the existing `tokenExpected` check in `ExchangeTransactionActuator`.

### Proof of Concept
1. Attacker observes victim's pending `ExchangeWithdrawContract(exchangeId, tokenId=firstTokenId, quant=Q)` in mempool.
2. Attacker broadcasts `ExchangeTransactionContract` swapping a large amount of `secondTokenId` into `firstTokenId`, skewing `firstTokenBalance`/`secondTokenBalance` so that `secondTokenBalance/firstTokenBalance` is much lower than before, and ensures it lands in the same block ahead of the victim's transaction.
3. Victim's `ExchangeWithdrawActuator.execute()` computes `anotherTokenQuant = secondTokenBalance * Q / firstTokenBalance` [8](#0-7)  using the now-manipulated ratio, returning far less `secondTokenId` than the pre-manipulation ratio would have.
4. Attacker reverses the swap in a subsequent transaction, restoring the ratio and pocketing the difference extracted from the victim's withdrawal — with no `expected`/minimum field on the contract to have prevented step 3 from executing at an unacceptable rate.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L228-243)
```java
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
