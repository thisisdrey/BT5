### Title
`ExchangeWithdrawContract` has no minimum-output/slippage parameter, letting withdrawers receive far less of the other token than expected - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
The `ExchangeWithdrawActuator` computes the counter-asset amount returned to an exchange creator solely from the current on-chain token ratio in the `ExchangeCapsule`, with no user-supplied minimum acceptable output. Unlike `ExchangeTransactionContract`, which carries an `expected` field enforced by `ExchangeTransactionActuator`, `ExchangeWithdrawContract` has no analogous field, so a withdrawer cannot protect against receiving less than expected if the pool ratio is manipulated between transaction submission and inclusion.

### Finding Description
`ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant`: [1](#0-0) 

By contrast, `ExchangeTransactionContract` explicitly carries an `expected` field that is checked and reverted on in `ExchangeTransactionActuator`: [2](#0-1) [3](#0-2) 

In `ExchangeWithdrawActuator`, `anotherTokenQuant` (the amount of the counter token returned to the withdrawer) is derived purely from the current pool balances at execution time, with only an internal rounding/"precision" check — never a user-specified floor: [4](#0-3) [5](#0-4) 

Because `ExchangeCapsule.transaction()` implements a bancor-style AMM whose price ratio moves with every `ExchangeTransactionContract` trade, an unprivileged order placer can submit a large trade against the same `exchange_id` in the same block, immediately before the victim's `ExchangeWithdrawContract` is applied. This shifts `firstTokenBalance`/`secondTokenBalance` so that the withdrawer's `anotherTokenQuant` is computed at a much worse ratio than what they observed and expected when broadcasting their transaction. There is no `expected`/`minCollateralAmount`-style parameter for `ExchangeWithdrawContract`, so the withdraw transaction cannot revert on unfavorable execution — it always succeeds at whatever ratio exists in the block-application order chosen by the block producer, transferring the reduced amount via `accountCapsule.addAssetAmountV2`/`setBalance`: [6](#0-5) 

This is directly analogous to the ShortCollateral liquidation bug: the caller (there, a liquidator; here, an exchange-liquidity withdrawer) always receives "whatever is left" rather than being guaranteed a minimum, because no floor/slippage-check parameter exists on the contract.

### Impact Explanation
A malicious or opportunistic trader can sandwich an exchange-liquidity-provider's withdraw transaction by trading against the pool right before it lands (transaction ordering within a block is influenced by fee/priority, which any account can pay for), skewing the ratio so the withdrawer receives significantly less of the counter asset than expected, while the attacker profits from the price movement. This is a concrete loss-of-funds scenario for any account holding exchange liquidity (`ExchangeCapsule`), reachable purely through ordinary signed transactions (`ExchangeTransactionContract` + `ExchangeWithdrawContract`), matching the "theft/loss of funds via unprotected slippage" impact class from the source report.

### Likelihood Explanation
Likelihood is moderate-to-high: exploitation requires only ordinary account privileges and standard `ExchangeTransactionContract`/`ExchangeWithdrawContract` transactions, no special permissions, and no reliance on malicious SRs/witnesses/peers. The attacker only needs to observe a pending withdraw (e.g., via mempool/API) and submit a trade with sufficient fee to be ordered first in the same block — well within the reach of any unprivileged transaction broadcaster.

### Recommendation
Add an `expected` (minimum-output) field to `ExchangeWithdrawContract`, mirroring `ExchangeTransactionContract.expected`, and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()` by reverting with `ContractValidateException` if the computed `anotherTokenQuant` is less than the caller-specified minimum.

### Proof of Concept
1. Account A creates/holds liquidity in exchange `E` (first_token/second_token balances `F`, `S`).
2. Account A broadcasts `ExchangeWithdrawContract{exchange_id=E, token_id=first, quant=q}`, expecting `anotherTokenQuant ≈ S*q/F` based on the state they last observed.
3. Before A's transaction is applied, an attacker B broadcasts a large `ExchangeTransactionContract` trade against `E` with higher fee/priority so it is packed earlier in the same block, sharply changing `F`/`S`.
4. When A's withdraw executes, `anotherTokenQuant` is recomputed against the new, attacker-skewed balances in `ExchangeWithdrawActuator.execute()` (lines 74-89), giving A far less of the counter token than expected, with no revert possible since there is no `expected`/minimum field to enforce — the transaction still returns `code.SUCESS`.
5. Attacker B then reverses their trade (or exploits arbitrage) to capture the value difference, analogous to the ShortCollateral `liquidate()` case where a caller could receive far less than intended with no `minCollateralAmount` guard.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-89)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L91-112)
```java
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
