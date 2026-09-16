### Title
Missing slippage/minimum-output protection in `ExchangeInjectContract` and `ExchangeWithdrawContract` allows front-running that mints/burns proportional pool tokens at an attacker-manipulated ratio - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
`ExchangeTransactionContract` includes a caller-supplied `expected` field that is validated against the swap output, giving users explicit slippage protection [1](#0-0) , enforced in `ExchangeTransactionActuator.doValidate()` [2](#0-1) . However, `ExchangeInjectContract` and `ExchangeWithdrawContract` carry no such minimum/maximum parameter at all [3](#0-2) , so the proportional counter-token amount is always computed from whatever the pool ratio happens to be at execution time, with no way for the sender to bound it. This is analogous to the reported "hard-coded slippage" bug class: the transaction has no user-controllable/protocol-enforced tolerance and blindly accepts the exchange-pool's live price.

### Finding Description
In `ExchangeInjectActuator.execute()`, the counter-token amount to be pulled from the account is computed purely from the current on-chain pool ratio (`firstTokenBalance`/`secondTokenBalance`) with no lower/upper bound supplied by the transaction sender: [4](#0-3) 

`ExchangeWithdrawActuator.execute()` has the same pattern for withdrawals - `anotherTokenQuant` is derived solely from the live pool ratio (`bigFirstTokenBalance`/`bigSecondTokenBalance`) at execution time: [5](#0-4) 

Because a broadcasting user cannot supply any `expected`/minimum/maximum parameter for Inject or Withdraw (unlike Transaction, which has `expected`), any other unprivileged account can submit an `ExchangeTransactionContract` swap immediately before the victim's Inject/Withdraw transaction is packed into the same block (front-running via mempool visibility), shifting `firstTokenBalance`/`secondTokenBalance` and thus the ratio used by the victim's Inject/Withdraw computation. The victim ends up injecting/withdrawing a materially different, attacker-favorable amount of the counter-token than they intended, without any on-chain check to reject the unfavorable execution.

### Impact Explanation
- For `ExchangeInjectContract`: an attacker can skew the pool ratio right before a victim's injection, causing the victim to be forced to contribute a larger amount of the counter-token than the fair-value ratio at the time they signed the transaction, transferring value to the attacker (who can then reverse their skewing swap after the injection to restore/profit from the ratio).
- For `ExchangeWithdrawContract`: only the exchange creator can call withdraw (`account is not creator` check) [6](#0-5) , limiting who is affected, but the creator still receives an unbounded (attacker-manipulable) split of tokens on withdrawal since there is no min-out check comparable to `expected` in Transaction.
- This results in unauthorized transfer of value between exchange participants purely by transaction ordering, a fund-loss condition reachable by any account that can submit an `ExchangeTransactionContract` immediately adjacent to a targeted Inject transaction.

### Likelihood Explanation
Likelihood is moderate-to-high for `ExchangeInjectContract`: any account can create/observe a pending Inject transaction in the mempool and front-run it with a swap, since Inject has zero fee-based friction (`calcFee()` returns 0) and no `expected` parameter to reject unfavorable execution. For Withdraw the same manipulation is possible but the victim set is limited to the exchange's creator account.

### Recommendation
Add an `expected`/minimum-output (and ideally maximum-input) field to `ExchangeInjectContract` and `ExchangeWithdrawContract`, mirroring `ExchangeTransactionContract.expected`, and validate the computed counter-token amount against it in `ExchangeInjectActuator.doValidate()`/`execute()` and `ExchangeWithdrawActuator.doValidate()`/`execute()` before mutating balances, rejecting the transaction if the live pool ratio produces an amount outside the caller-specified tolerance.

### Proof of Concept
Not independently reproducible from static analysis alone; the vulnerability is inferred from the actuator code paths above, where `ExchangeInjectActuator`/`ExchangeWithdrawActuator` lack any caller-supplied bound comparable to `ExchangeTransactionContract.expected`, unlike the sibling exchange contract that explicitly implements this check. Confirming actual exploitability (e.g., measuring the achievable value transfer via a sandwiched Inject) would require running a local java-tron network and submitting a crafted `ExchangeTransactionContract` immediately before/after a target `ExchangeInjectContract`/`ExchangeWithdrawContract`.

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
