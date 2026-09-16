This confirms the finding: `ExchangeCreateActuator` never validates or normalizes token precision when creating a Bancor-style exchange pair, and none of `ExchangeCapsule`, `ExchangeProcessor`, `SafeExchangeProcessor`, `ExchangeTransactionActuator`, or `ExchangeWithdrawActuator` reference `precision` at all in production code (only tests do). This is a solid analog to the `YieldReserve` decimals bug.

### Title
TRC10 Exchange Pools Ignore Token `precision`, Enabling Cross-Precision Swap Arbitrage - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
The `YieldReserve` bug involved swap math that ignored the destination token's decimal precision, letting an attacker swap a high-decimal token for a disproportionately large amount of a low-decimal token. Java-tron's native Bancor-style TRC10 exchange has the same class of bug: `AssetIssueContract.precision` (0–6, see `protocol/src/main/protos/core/contract/asset_issue_contract.proto:22`) is defined per-asset at issuance, but the exchange pool math treats `firstTokenBalance`/`secondTokenBalance`/`sellTokenQuant` as raw integer units with no precision normalization anywhere in the swap path.

### Finding Description
When an `ExchangeCreateContract` is processed, the actuator stores the two token IDs and their initial balances with no check that their `precision` values match or any recording of precision on the exchange pool itself, as seen in `actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java`. The core pricing logic in `ExchangeCapsule.transaction()` at [1](#0-0)  passes raw `long` balances/quantities straight into `ExchangeProcessor.exchange()` / `SafeExchangeProcessor.exchange()` ( [2](#0-1) , [3](#0-2) ) with no conversion factor for each token's `precision`. `ExchangeTransactionActuator.execute()`/`doValidate()` ( [4](#0-3) ) then credits/debits `tokenQuant`/`anotherTokenQuant` directly to account asset balances via `reduceAssetAmountV2`/`addAssetAmountV2`, again in raw integer units. Because a TRC10 issuer can freely choose `precision` from 0 to 6 (enforced only as an upper bound in `AssetIssueActuator.validate()`, [5](#0-4) ), two tokens with different precisions (e.g., precision 0 vs precision 6) can be paired in the same exchange pool, and the constant-product/Bancor formula will price 1 raw unit of one token as economically equivalent to 1 raw unit of the other — exactly the decimal-mismatch flaw described in the report, just implemented in java-tron's own AMM rather than Sperax's Solidity contract.

### Impact Explanation
An attacker who creates (or participates in) an `Exchange` pairing two TRC10 assets with different `precision` values can drain the pool: depositing raw units of the low-value-per-unit (high precision) token and withdrawing disproportionately large amounts of the high-value-per-unit (low precision) token, or vice versa, causing theft of funds from other liquidity/pool participants and unbacked asset balances credited to the attacker's account.

### Likelihood Explanation
`ExchangeCreateContract`, `ExchangeTransactionContract`, and `ExchangeWithdrawContract` are all standard broadcastable transactions available to any account with sufficient TRX balance and free TRC10 tokens — no special privilege required. An attacker only needs to issue (or acquire) two TRC10 tokens with differing precision values and create an exchange pair, which is a normal, permissionless user flow.

### Recommendation
Normalize `precision` between the two exchange tokens at `ExchangeCreateActuator` validation time (reject pools where `precision` differs, or scale balances/quantities to a common precision, e.g. 6 decimals) before they are ever passed into `ExchangeCapsule.transaction()`, `ExchangeProcessor`, and `SafeExchangeProcessor`. Alternatively, store each token's `precision` on the `Exchange` capsule and apply a `10^(6-precision)` scaling factor consistently in `transaction()`, `ExchangeTransactionActuator`, and `ExchangeWithdrawActuator` before computing and crediting swap amounts.

### Proof of Concept
1. Issue TRC10 token `A` with `precision=0` and TRC10 token `B` with `precision=6` via `AssetIssueContract` (both accepted since `AssetIssueActuator.validate()` only caps precision at 6, [5](#0-4) ).
2. Create an `ExchangeCreateContract` pairing `A` and `B` with equal raw-unit balances, e.g. `firstTokenBalance = 1_000_000` of `A` and `secondTokenBalance = 1_000_000` of `B` (via `ExchangeCreateActuator`).
3. Because `A` has precision 0 (1 raw unit = 1 whole token) and `B` has precision 6 (1 raw unit = 0.000001 whole token), 1,000,000 raw units of `A` represents 1,000,000 whole tokens while 1,000,000 raw units of `B` represents only 1 whole token — yet `ExchangeCapsule.transaction()` prices them as economically equal.
4. Submit an `ExchangeTransactionContract` selling raw units of `A` (cheap in real terms) for raw units of `B` (expensive in real terms), executed through `ExchangeTransactionActuator.execute()` ( [6](#0-5) ), realizing a large real-value profit at the expense of the pool/other participants, with no validation step anywhere checking or compensating for the `precision` mismatch.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L40-44)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-91)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L176-181)
```java
    int precision = assetIssueContract.getPrecision();
    if (precision != 0
        && dynamicStore.getAllowSameTokenName() != 0
        && (precision < 0 || precision > ActuatorConstant.PRECISION_DECIMAL)) {
      throw new ContractValidateException("precision cannot exceed 6");
    }
```
