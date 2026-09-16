### Title
`ExchangeInjectActuator` liquidity injection has no minimum-received/slippage protection, enabling sandwich frontrunning of exchange creators - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
Java-tron's on-chain constant-product `Exchange` (bancor-style AMM pools created by `ExchangeCreateContract`) has two mutating, permissionless user operations: `ExchangeTransactionContract` (swap) and `ExchangeInjectContract` (add liquidity). Unlike the swap operation, which already carries an `expected` minimum-output field to defend against exactly the kind of frontrunning described in the external report, `ExchangeInjectContract` has no equivalent slippage/minimum bound at all. The paired-token amount required for an injection is computed strictly from whatever the pool ratio happens to be at execution time, which lets an attacker sandwich a legitimate injection transaction and extract value from the injecting account, mirroring the "not considering the slippage when minting… tokens" half of the referenced report.

### Finding Description
`ExchangeTransactionActuator` (the swap) requires the caller to supply `expected`, the minimum acceptable output, and enforces it in `doValidate`: [1](#0-0) 

By contrast, `ExchangeInjectActuator.doValidate` and `execute` compute the counter-token amount purely from the exchange's *current* on-chain balances, with no user-supplied minimum/maximum bound to protect against ratio manipulation between transaction broadcast and inclusion: [2](#0-1) [3](#0-2) 

The `ExchangeInjectContract` protobuf message itself carries only `owner_address`, `exchange_id`, `token_id`, and `quant` — no `expected` or bound field exists to be added by callers, confirmed by the fact that no `getExpected()` call exists anywhere in `ExchangeInjectActuator.java`, in contrast to its use throughout `ExchangeTransactionActuator.java`.

Anyone can create an exchange pool via `ExchangeCreateContract`, and only the exchange's creator (an ordinary, unprivileged account — not an SR/witness) may call `ExchangeInjectContract` (enforced in validate): [4](#0-3) 

This makes the entire attack path reachable purely through ordinary signed transactions from unprivileged accounts (an asset issuer/exchange creator issuing an inject transaction, and any other broadcaster racing to sandwich it).

### Impact Explanation
A frontrunner can observe a pending `ExchangeInjectContract` transaction in the mempool, submit an `ExchangeTransactionContract` swap immediately before it to skew the pool's `firstTokenBalance`/`secondTokenBalance` ratio, let the victim's inject execute at the manipulated ratio (forcing the victim to deposit a disadvantageous amount of the second token relative to the "fair" price), and then submit a reverse swap immediately after to restore the original ratio and pocket the price difference — a classic sandwich attack. This results in a concrete, unauthorized transfer of value from the injecting account to the attacker (theft of funds), matching the "Medium" severity class assigned to the original yAxis finding for the analogous missing-slippage-check pattern.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to see the pending inject transaction (feasible via mempool monitoring, which is normal MEV behavior and not a "malicious node/SR" precondition) and to be able to get two of their own transactions ordered around it within the same block — achievable via standard fee/bandwidth prioritization available to any broadcaster, not requiring any privileged network role.

### Recommendation
Add a `min_expected` (or `expected_second_token_quant`) field to `ExchangeInjectContract`, analogous to the `expected` field already present in `ExchangeTransactionContract`, and enforce it in `ExchangeInjectActuator.doValidate`/`execute` by rejecting the injection if the computed `anotherTokenQuant` deviates unfavorably from the caller-supplied bound (i.e., is greater than a caller-specified maximum, since the injector is providing, not receiving, `anotherTokenQuant`).

### Proof of Concept
Not independently reproducible in this environment (no code execution or transaction-broadcasting access available for this analysis); the vulnerability is inferred by code inspection of `ExchangeInjectActuator.java` and its protobuf contract definition. A concrete PoC would require: (1) create an exchange pool, (2) broadcast an `ExchangeInjectContract`, (3) have a second account race an `ExchangeTransactionContract` swap immediately before and after the victim's transaction within the same block to demonstrate the value extraction described above. Confirming exact block-level transaction ordering guarantees would require examining `Manager`'s transaction-ordering/packing logic, which was outside the scope of the code reviewed here.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-83)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L209-231)
```java
    BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
    BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
    BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
    long newTokenBalance;
    long newAnotherTokenBalance;

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
