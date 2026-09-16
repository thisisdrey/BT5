## Title
Silent Integer Overflow/Underflow in `AbstractExchangeActuator` Balance-Limit and Balance-Update Arithmetic Bypasses Exchange Limit Checks - (`actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java`)

## Summary
The exchange actuators (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`) route all balance addition/subtraction through `AbstractExchangeActuator.addExact`/`subtractExact`, which only perform checked (`Math.addExact`/`subtractExact`-style) arithmetic when the committee-controlled `allowHardenExchangeCalculation` flag is enabled. By default (flag = 0) these helpers fall back to raw Java `long` `x + y` / `x - y`, which silently wraps around on overflow/underflow instead of throwing — the same root-cause class as the reported Solidity issue, where an implicit unsafe cast bypasses the language's built-in overflow protection.

## Finding Description
`AbstractExchangeActuator` defines: [1](#0-0) 

`allowHarden()` reads `chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation()`, a dynamic property that is only turned on via a committee proposal (`ProposalUtil`/`ProposalService`), meaning on a freshly configured or non-upgraded chain it defaults to disabled. When disabled, `subtractExact`/`addExact` degrade to unchecked `long` arithmetic.

These unsafe helpers are used both in the actuators' `execute()` paths (balance mutation) and in `doValidate()` for the exchange-balance-limit check, e.g. in `ExchangeTransactionActuator.doValidate()`: [2](#0-1) 

and identically in `ExchangeInjectActuator.execute()`/`doValidate()` balance mutation: [3](#0-2) 

`tokenBalance = addExact(tokenBalance, tokenQuant)` is meant to guard `tokenBalance > balanceLimit`. If `addExact` falls back to plain `+` (hardened flag off) and an attacker supplies a `quant` close to `Long.MAX_VALUE` (the protobuf `int64` field is only checked for `> 0`, not upper-bounded before this arithmetic), the sum silently wraps to a negative value. A negative `tokenBalance` will never satisfy `tokenBalance > balanceLimit`, so the limit check is bypassed and the exchange pool's on-chain balance field is corrupted with a wrapped value, exactly mirroring the reported bug class: an implicit overflow/underflow that should have reverted but instead silently produces an incorrect, exploitable numeric result.

The companion `ExchangeCapsule.transaction()` (used for actual swaps) has the identical dual-path design — plain `+`/`-` when `hardenedCalc` is false, checked `StrictMathWrapper.addExact/subtractExact` only when true: [4](#0-3) 

## Impact Explanation
Because the checked-arithmetic path is opt-in via governance rather than the default, any unprivileged account can broadcast `ExchangeInjectContract`, `ExchangeWithdrawContract`, or `ExchangeTransactionContract` transactions with crafted large `quant` values to trigger silent wraparound in the pool-balance bookkeeping. This corrupts `ExchangeCapsule` balances (potentially making them negative or otherwise inconsistent with real backing assets), which can be leveraged to bypass the `balanceLimit` check, create balances not backed by real deposits, or subsequently allow withdrawals/trades against a corrupted (and effectively unbacked) pool — a form of unbacked-balance/fund-theft vulnerability rather than a benign revert.

## Likelihood Explanation
Reaching this code path requires no special privilege: an attacker only needs to create an exchange pair (or use an existing one) and submit an `ExchangeInjectContract`/`ExchangeTransactionContract` with an extreme `quant`. The main constraint is having (or appearing to have) sufficient underlying asset balance to pass the `assetBalanceEnoughV2`/TRX balance checks, since TRC-10 token amounts and TRX are represented as signed `long`s and asset issuers can mint tokens with very large `total_supply` (bounded only by `long` range), making attacker-controlled large `quant` values plausible for an asset the attacker itself issues and controls.

## Recommendation
Make the checked-arithmetic path (`StrictMathWrapper.addExact`/`subtractExact`) the unconditional default in `AbstractExchangeActuator.addExact`/`subtractExact` and in `ExchangeCapsule.transaction()`, rather than gating it behind the `allowHardenExchangeCalculation` proposal. If backward compatibility requires keeping the flag, ensure the flag defaults to enabled on new deployments and that upper bounds are validated on `quant`/balances before performing arithmetic, independent of the flag.

## Proof of Concept
1. Attacker issues (or uses) a TRC-10 token `X` with `total_supply` close to `Long.MAX_VALUE`.
2. Attacker creates an exchange pair `TRX/X` via `ExchangeCreateContract`.
3. On a chain where `allowHardenExchangeCalculation` has not been enabled by committee proposal (default state), attacker submits `ExchangeInjectContract` for token `X` with `quant` chosen so that `secondTokenBalance + anotherTokenQuant` (computed via unchecked `addExact` in `ExchangeInjectActuator`) overflows `long`, wrapping to a negative or unexpectedly small value.
4. The `newAnotherTokenBalance > balanceLimit` check in `doValidate()` (`actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java:229-236`) is bypassed because the wrapped value is negative.
5. The exchange pool's `ExchangeCapsule` balance field is now corrupted/inconsistent with real backing, which can be leveraged in subsequent `ExchangeTransactionContract`/`ExchangeWithdrawContract` calls to extract more value than was actually deposited.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-23)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }

  public long subtractExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.subtractExact(x, y) : x - y;
  }

  public long addExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.addExact(x, y) : x + y;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-205)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-89)
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L136-162)
```java
    if (this.exchange.getFirstTokenId().equals(ByteString.copyFrom(sellTokenID))) {
      buyTokenQuant = processor.exchange(firstTokenBalance,
          secondTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(firstTokenBalance, sellTokenQuant)
          : firstTokenBalance + sellTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
          : secondTokenBalance - buyTokenQuant;

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
```
