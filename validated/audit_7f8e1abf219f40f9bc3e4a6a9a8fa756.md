### Title
Exchange trading arithmetic silently degrades from SafeMath-checked ops to raw unchecked `long` arithmetic when `AllowHardenExchangeCalculation` is not enabled - (File: actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java)

### Summary
The Bancor-style TRX/TRC10 exchange (`ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract`) is java-tron's closest analog to the reported AMM/yield-source contracts. Its actuators expose `addExact`/`subtractExact` helpers that are supposed to guard the pool-balance math the same way `SafeMath` guards arithmetic in the referenced Solidity yield sources, but these helpers only perform overflow-checked math when the on-chain `allowHardenExchangeCalculation` dynamic property is turned on. By default that flag is off, so every "safe" call actually executes the unchecked `x + y` / `x - y` fallback — i.e. `SafeMath` is not completely used, exactly the bug class from the report, but reachable directly by any unprivileged account broadcasting an Exchange* transaction.

### Finding Description
`AbstractExchangeActuator.addExact`/`subtractExact` are used throughout `ExchangeInjectActuator`, `ExchangeTransactionActuator`, and `ExchangeWithdrawActuator` to update pool balances and account balances: [1](#0-0) 

`allowHarden()` gates whether `StrictMathWrapper` (overflow-checked) or plain `+`/`-` is executed, and it is controlled by a chain parameter that defaults to disabled unless enabled via governance proposal.

The same conditional degradation exists in the core exchange math itself, `ExchangeCapsule.transaction`, where the negative-balance safety check is only performed in the `hardenedCalc` branch — the default (non-hardened) branch performs plain `firstTokenBalance + sellTokenQuant` / `secondTokenBalance - buyTokenQuant` with **no bounds or overflow check at all**: [2](#0-1) 

Additionally, in `ExchangeTransactionActuator.doValidate`, the balance-limit guard itself calls the same conditionally-safe `addExact`: [3](#0-2) 

If `tokenBalance` is near `Long.MAX_VALUE`, unchecked `tokenBalance + tokenQuant` wraps to a negative value, which is `<= balanceLimit`, so the validation silently passes an amount that should have been rejected — this is precisely the "arithmetic overflow bypasses a safety check" pattern described in the external report.

### Impact Explanation
An overflow/underflow in the Exchange pool-balance bookkeeping can let an attacker mint value out of the pool (unbacked balance) or make the pool's recorded balances diverge from actual TRX/token custody, ultimately enabling theft of funds from other liquidity participants or permanent freezing of the pool once its invariants are corrupted. This mirrors the sponsor's own risk assessment in the original report ("if an overflow occurred this would significantly disrupt the yield sources").

### Likelihood Explanation
Exploitability depends on getting a token balance in an Exchange pool close to `Long.MAX_VALUE`, which requires control of a TRC10 asset with a very large total supply (attacker-issuable) and injecting it into a self-created exchange over multiple `ExchangeInjectContract` calls, bounded only by `getExchangeBalanceLimit()`. This is a purely permissionless path — creating assets, creating exchanges, and injecting/trading are all unprivileged actuator entry points — but requires the operator to not have enabled `allowHardenExchangeCalculation` (the default state) and requires sizable but attacker-controlled setup transactions, so likelihood is medium rather than trivial.

### Recommendation
Make the overflow/underflow-checked arithmetic (`StrictMathWrapper.addExact`/`subtractExact`, and the negative-balance guard in `ExchangeCapsule.transaction`) unconditional rather than gated behind the `allowHardenExchangeCalculation` feature flag, so pool-balance math is always protected regardless of chain-parameter state.

### Proof of Concept
1. Issue a TRC10 asset with total supply close to `Long.MAX_VALUE` via `AssetIssueContract` (unprivileged).
2. Create an Exchange pool pairing this asset with TRX via `ExchangeCreateContract`.
3. Repeatedly call `ExchangeInjectContract` to push `firstTokenBalance`/`secondTokenBalance` close to `Long.MAX_VALUE`, staying under `getExchangeBalanceLimit()`.
4. Submit an `ExchangeTransactionContract`/`ExchangeInjectContract` with `tokenQuant` sized so that `tokenBalance + tokenQuant` (in `ExchangeTransactionActuator.doValidate`, line 202) or `firstTokenBalance + sellTokenQuant` (in `ExchangeCapsule.transaction`, non-hardened path) overflows `long`, wrapping to a negative/small value that bypasses the `> balanceLimit` check while `allowHardenExchangeCalculation` remains disabled (default).
5. Observe that the pool state (`ExchangeCapsule`) is updated with corrupted balances, allowing subsequent trades to extract more value than was ever deposited.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-162)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

    long buyTokenQuant = 0;
    long firstTokenBalance = this.exchange.getFirstTokenBalance();
    long secondTokenBalance = this.exchange.getSecondTokenBalance();
    long newFirstTokenBalance;
    long newSecondTokenBalance;

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
