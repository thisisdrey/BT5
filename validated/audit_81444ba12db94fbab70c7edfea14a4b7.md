## Analysis

This maps directly onto the "operation reverts on an amount that should succeed" bug class from the external report. In `WstEth.withdraw()`, `unwrap(_amount)` computes a proportional value via a division that can floor to `0` even for a valid non-zero input, and a `require(stEthAmount > 0)` then reverts the whole withdrawal instead of proceeding.

The java-tron analog is in `ExchangeWithdrawActuator.doValidate()`, which computes the counter-asset amount for an AMM-style token-pair pool via integer division and then unconditionally rejects the entire withdrawal if that computed amount floors to zero. [1](#0-0) 

### Title
Exchange pool withdrawal permanently reverts (fund lock) when floor-division of the paired-token amount rounds to zero - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract` lets the creator of a bonding-curve token-pair exchange (created via `ExchangeCreateContract`) withdraw liquidity by specifying `quant` of one side of the pool. The actuator computes the proportional amount of the *other* side (`anotherTokenQuant`) via `BigDecimal` multiplication/floor-division of the two pool reserves, then throws `ContractValidateException("withdraw another token quant must greater than zero")` whenever that computed value is `<= 0`, aborting the entire withdrawal even though the requested `tokenQuant` (the amount actually being validated) is itself strictly positive and otherwise valid.

### Finding Description
In `doValidate()`, after confirming `tokenQuant > 0` [2](#0-1) , the code derives `anotherTokenQuant` by floor-dividing `secondTokenBalance * tokenQuant / firstTokenBalance` (or the symmetric case) and immediately reverts if that result is `<= 0`: [3](#0-2) 
and the symmetric branch: [4](#0-3) 

Because `firstTokenBalance`/`secondTokenBalance` are the exchange's live reserves and can be highly asymmetric (e.g. one side scaled to `1_000_000_000_000_000_000` supply-style precision while the other is small), any `tokenQuant` below `ceil(firstTokenBalance / secondTokenBalance)` will always compute `anotherTokenQuant == 0` and always revert — no matter what `tokenQuant` the creator chooses within that range. This is precisely the pattern flagged in the referenced report: a legitimate non-zero withdrawal request is blocked purely because an internally derived, secondary quantity floors to zero, rather than the actuator allowing the withdrawal to proceed (crediting `0` of the other asset, or requiring the caller to pick a larger `tokenQuant`, which the current code enforces by hard-reverting instead of clamping/looping).

The existing test suite documents this exact behavior as "expected", confirming it is reachable with ordinary parameters (e.g. `quant = 1` against a `100000000`-scale pool reverts with "withdraw another token quant must greater than zero"): [5](#0-4) 

The identical unguarded-floor-to-zero pattern also exists in `ExchangeInjectActuator.doValidate()`: [6](#0-5) 

### Impact Explanation
The exchange creator's assets deposited into the pool (via `ExchangeCreateContract`/`ExchangeInjectContract`) can become effectively frozen for withdrawal through `ExchangeWithdrawContract`: any attempt to withdraw an amount of the majority-reserve token whose proportional counterpart floors to zero is rejected outright by `ContractValidateException`, with no path in the actuator to still process the withdrawal (e.g. by treating `anotherTokenQuant` as `0` and skipping the credit, analogous to the report's recommended `if (stEthAmount > 0) return;` fix). If the creator's reachable `tokenQuant` values (bounded by pool reserve ratios and their own balance) never cross this threshold, that portion of their pool position becomes permanently unwithdrawable via this actuator — a fund-freezing condition confined to the specific market's creator, but concretely reachable purely from actuator validation logic on a normal signed `ExchangeWithdrawContract` transaction.

### Likelihood Explanation
Only the actuator's own creator can call `ExchangeWithdrawContract` (`accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())` check [7](#0-6) ), so the affected party is limited to that address, but the trigger condition (reserve ratio skew combined with a small requested `tokenQuant`) is easy to reach on any long-lived or heavily imbalanced token-pair exchange, and is already exercised/asserted by the project's own unit tests as the actual runtime behavior.

### Recommendation
When `anotherTokenQuant` computes to `0`, do not reject the transaction outright: either (a) allow the withdrawal to proceed with `anotherTokenQuant = 0` (crediting nothing on that side, mirroring the report's `if (amount > 0) return;`-style fix), or (b) require/derive a minimum viable `tokenQuant` so the creator can always withdraw their full balance without hitting a permanently unreachable ratio, instead of hard-failing with `ContractValidateException`.

### Proof of Concept
1. Create an exchange with a heavily skewed reserve ratio (e.g. `firstTokenBalance = 100_000_000` vs `secondTokenBalance = 1`) as the creator.
2. As the creator, submit `ExchangeWithdrawContract` with `tokenId = firstTokenID`, `quant = 1` (a small, otherwise valid amount).
3. `anotherTokenQuant = secondTokenBalance * 1 / firstTokenBalance = 0` after floor division.
4. `doValidate()` throws `ContractValidateException("withdraw another token quant must greater than zero")`, and the withdrawal never executes — reproduced directly by the existing test `SameTokenNameCloseTnotherTokenQuantLessThanZero` [8](#0-7) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L190-227)
```java
    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();

    long anotherTokenQuant;

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }

    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("withdraw token quant must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L245-254)
```java
    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
      if (secondTokenBalance < tokenQuant || firstTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeWithdrawActuatorTest.java (L1215-1257)
```java
  /**
   * SameTokenName close, withdraw another token quant must greater than zero
   */
  @Test
  public void SameTokenNameCloseTnotherTokenQuantLessThanZero() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(0);
    InitExchangeBeforeSameTokenNameActive();
    long exchangeId = 1;
    String firstTokenId = "abc";
    long quant = 1L;
    String secondTokenId = "def";
    long secondTokenQuant = 400000000L;

    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_FIRST);
    AccountCapsule accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    accountCapsule.addAssetAmount(firstTokenId.getBytes(), 1000L, false);
    accountCapsule.addAssetAmount(secondTokenId.getBytes(), secondTokenQuant, false);
    accountCapsule.setBalance(10000_000000L);
    dbManager.getAccountStore().put(ownerAddress, accountCapsule);

    ExchangeWithdrawActuator actuator = new ExchangeWithdrawActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, exchangeId, secondTokenId, quant));

    TransactionResultCapsule ret = new TransactionResultCapsule();

    try {
      actuator.validate();
      actuator.execute(ret);
      fail();
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertEquals("withdraw another token quant must greater than zero",
          e.getMessage());
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    } finally {
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
    }
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
