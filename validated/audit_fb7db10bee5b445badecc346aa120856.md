### Title
Exchange pool creator can pick an unconstrained, extreme `firstTokenBalance`/`secondTokenBalance` ratio that the floating-point Bancor formula cannot safely price, allowing a follow-up swap to extract value disproportionate to what was deposited - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java)

### Summary
`ExchangeCreateActuator` lets any account act as the "first liquidity provider" of a Bancor-style two-token pool by freely choosing `firstTokenBalance` and `secondTokenBalance`, subject only to non-zero and `ExchangeBalanceLimit` checks, with no requirement that the two values maintain any specific mathematical relationship. This mirrors the Numoen report's root cause: the first depositor's freely-chosen amounts are not validated against the invariant the protocol's swap math actually depends on, so a badly (or maliciously) chosen ratio produces a state from which subsequent trades can extract more value than was deposited.

### Finding Description
`doValidate()` in `ExchangeCreateActuator` only checks that `firstTokenBalance`/`secondTokenBalance` are positive, within `dynamicStore.getExchangeBalanceLimit()`, and that the creator's account holds sufficient balance: [1](#0-0) 

No check enforces that the chosen `firstTokenBalance`/`secondTokenBalance` pair is a "safe" starting ratio for the pricing formula. The pool's price/exchange math is subsequently computed by `ExchangeProcessor.exchange`, which relies on floating-point `Math.pow` calculations (`exchangeToSupply`/`exchangeFromSupply`) rather than exact integer arithmetic: [2](#0-1) 

When the creator sets an extremely skewed ratio (e.g., `firstTokenBalance = 1` and `secondTokenBalance` near `balanceLimit`, or vice versa), the ratio `quant/newBalance` fed into `Math.pow` can push the floating-point computation into a region where double-precision rounding error is large relative to the tiny balance, so the derived "relay supply" and resulting `buyTokenQuant` no longer conserve value the way the Bancor invariant intends. This is structurally the same class of issue as the Numoen bug: the initial reserve amounts are supplied by an unprivileged actor without any on-chain enforcement of the relationship the pricing invariant requires, so a subsequent `swap`/`exchange` transaction (here, `ExchangeTransactionActuator`) can pull out disproportionate value: [3](#0-2) 

The presence of a "hardened" calculation path (`SafeExchangeProcessor`, `StrictMathWrapper`, `AllowHardenExchangeCalculation`) that is off by default, and only used when `allowHarden()` is enabled, further indicates that the non-hardened, floating-point default path is the historically riskier one still reachable by any signed `ExchangeCreateContract`/`ExchangeTransactionContract` pair: [4](#0-3) 

### Impact Explanation
If an attacker can find (or is incentivized to construct) an extreme-ratio pool where the floating-point Bancor pricing diverges enough from the exact invariant, they can create the pool themselves as "first LP" and then immediately trade against it, extracting more of one token than the exact math would allow, at the expense of the pool's other-side reserve (unbacked balance / fund loss for the pool, analogous to Numoen's free `token0` extraction via `swap()`). This is a concrete unauthorized value-extraction path reachable purely from a signed `ExchangeCreateContract` followed by a signed `ExchangeTransactionContract`.

### Likelihood Explanation
Medium. Exploitation requires the attacker to construct a precise combination of `firstTokenBalance`/`secondTokenBalance` and trade size such that the floating-point rounding in `Math.pow`-based `exchangeToSupply`/`exchangeFromSupply` produces a favorable divergence from the exact invariant; this is more involved than a trivial call but does not require any special privilege, only ordinary account balances and two normal transactions (create + transact). No SR/witness/peer compromise is needed.

### Recommendation
Either (a) require the exchange-create/transaction math to always run through the exact, overflow/rounding-safe path (`SafeExchangeProcessor`) by default rather than gating it behind `AllowHardenExchangeCalculation`, or (b) add validation in `ExchangeCreateActuator.doValidate()` that rejects reserve ratios/extremes for which the floating-point Bancor approximation cannot be trusted to conserve value, and add a way to preview/verify the resulting price curve before committing funds.

### Proof of Concept
1. Attacker calls `ExchangeCreateContract` with `firstTokenBalance = 1` and `secondTokenBalance` close to `ExchangeBalanceLimit`, funding it themselves; `ExchangeCreateActuator.doValidate` accepts this since it only checks non-zero and the balance limit [1](#0-0) .
2. Attacker immediately issues `ExchangeTransactionContract` to swap a small amount of `firstTokenId` for `secondTokenId`; `ExchangeCapsule.transaction` invokes the non-hardened `ExchangeProcessor.exchange`, whose floating-point `Math.pow` computation can diverge from the exact conserved-value curve given the extreme starting ratio [5](#0-4) .
3. The resulting `anotherTokenQuant` computed by `exchange` is credited back to the attacker via `ExchangeTransactionActuator.execute` without any independent invariant check [6](#0-5) , letting the attacker extract value the exact Bancor formula would not have permitted.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L201-208)
```java
    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (firstTokenBalance > balanceLimit || secondTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-45)
```java
  private long exchangeToSupply(long balance, long quant) {
    logger.debug("balance: " + balance);
    long newBalance = balance + quant;
    logger.debug("balance + quant: " + newBalance);

    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }

  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-69)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L86-91)
```java
      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```
