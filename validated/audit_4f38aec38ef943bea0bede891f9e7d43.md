## Title
Missing slippage/deadline protection in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` allows sandwich-style value extraction from liquidity providers - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeInjectContract` (deposit/add-liquidity) and `ExchangeWithdrawContract` (withdraw/remove-liquidity) carry no deadline field and no user-specified minimum/maximum bound on the counter-token amount computed at execution time. This is the exact analog of the reported bug class: unlike Uniswap's `addLiquidity`, which lets the caller pass `amountAMin`/`amountBMin` and a `deadline`, java-tron's on-chain exchange deposit/withdraw operations compute the paired-token amount purely from the exchange pool's ratio *at execution time*, with no way for the signer to bound acceptable slippage.

### Finding Description
`ExchangeInjectContract` only defines `owner_address`, `exchange_id`, `token_id`, `quant` — no expected/min amount and no deadline, as documented in the protobuf spec (`Tron protobuf protocol document.md:1384-1401`). The same is true for `ExchangeWithdrawContract` (`Tron protobuf protocol document.md:1403-1420`).

In `ExchangeInjectActuator.execute`, the reciprocal amount (`anotherTokenQuant`) is derived solely from the exchange's current on-chain balances at the moment the transaction executes, and is then unconditionally deducted from the signer's account balance: [1](#0-0) 

`doValidate()` for inject only checks that quantities are positive and within `getExchangeBalanceLimit()` — there is no `tokenExpected`/min-received-style check comparable to what `ExchangeTransactionActuator` enforces: [2](#0-1) 

Likewise, `ExchangeWithdrawActuator.execute` computes `anotherTokenQuant` from the live pool ratio and credits it to the caller with no min-amount protection: [3](#0-2) 

Its validation only checks precision/rounding tolerance, never a caller-supplied minimum acceptable output: [4](#0-3) 

By contrast, `ExchangeTransactionActuator` — the swap/trade actuator — does implement exactly the kind of slippage bound the report recommends, taking a `tokenExpected` value and reverting if the computed output is below it: [5](#0-4) 

This asymmetry shows the protocol has already recognized and mitigated this exact bug class for trades, but never extended the same protection to deposit (`inject`) and withdraw operations, mirroring precisely the "deposit-withdraw-trade" gap described in the external report.

### Impact Explanation
Because `ExchangeInjectContract`/`ExchangeWithdrawContract` have no deadline and no min/max bound, any account whose transaction is broadcast can be sandwiched: an attacker (or block producer) observes the pending inject/withdraw transaction, executes `ExchangeTransactionContract` trades before it to shift the pool ratio, lets the victim's inject/withdraw execute at the now-unfavorable ratio, then trades back afterward to restore the ratio and pocket the difference. For `ExchangeInjectContract`, this forces the victim to contribute more of the second token than they intended when signing. For `ExchangeWithdrawContract`, this causes the victim to receive fewer paired tokens than expected. This is a concrete unauthorized value transfer from ordinary liquidity providers to the attacker, reachable by any unprivileged transaction broadcaster.

### Likelihood Explanation
Any account can create these exchanges and any account can trade against them via `ExchangeTransactionContract`, which is a public, permissionless contract type. Standard mempool visibility together with control over transaction ordering (achievable by any block-producing/witness-adjacent actor, or simply by racing transactions) is sufficient to execute the sandwich; no special privilege beyond normal transaction submission is required.

### Recommendation
Add an analogous `expected`/minimum-output field (and optionally a deadline/expiration field distinct from the generic transaction expiration) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce it in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` the same way `tokenExpected` is enforced in `ExchangeTransactionActuator`.

### Proof of Concept
1. Attacker monitors mempool for a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction from a victim on exchange `X`.
2. Attacker submits an `ExchangeTransactionContract` trade against exchange `X` to shift `firstTokenBalance`/`secondTokenBalance` unfavorably relative to the victim's expectation.
3. Victim's inject/withdraw transaction executes using the now-skewed ratio computed in `ExchangeInjectActuator.execute`/`ExchangeWithdrawActuator.execute` (lines cited above) — there is no check rejecting an unfavorable ratio.
4. Attacker reverses the trade with another `ExchangeTransactionContract`, restoring the ratio and capturing the value difference extracted from the victim.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
