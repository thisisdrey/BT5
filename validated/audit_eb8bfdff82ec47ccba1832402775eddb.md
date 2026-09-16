Based on the investigation, java-tron's TRC10 AMM-style `Exchange` module (Bancor-curve token swap) is the closest structural analog to the JOJO `FundingRateUpdateLimiter` sandwich issue: a state-changing operation with a **zero trading fee** that any unprivileged order placer can exploit to sandwich other traders' swaps.

### Title
Zero-fee `ExchangeTransactionContract` swaps allow free sandwich/front-running attacks against TRC10 exchange traders - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
`ExchangeTransactionActuator`, which executes TRC10-to-TRC10/TRX swaps against a Bancor-curve liquidity pool (`ExchangeCapsule#transaction`), charges **zero fee** (`calcFee()` returns `0`). Any account can submit `ExchangeTransactionContract` transactions through the public `/wallet/exchangetransaction` HTTP endpoint or gRPC equivalent (`ExchangeTransactionServlet`), and pending transactions are visible in the node's pending-transaction pool before block inclusion, exactly as in the original JOJO report where the "ordersender" observes an imminent price-moving action and sandwiches it.

### Finding Description
The swap price for an exchange pair is entirely determined by the current pool balances via the Bancor formula in `ExchangeProcessor#exchange` / `SafeExchangeProcessor#exchange`, invoked from `ExchangeCapsule#transaction`: [1](#0-0) 

Each swap mutates the pool balances, so any transaction that changes the pool ratio (in particular, another trader's swap awaiting confirmation) predictably shifts the price for subsequent swaps in the same or next block. Because `ExchangeTransactionActuator.calcFee()` returns `0`, unlike the swap-fee mechanisms typical of AMMs (which return part of the value to the pool/LPs), an attacker can front-run a visible pending swap with a larger-fee transaction, let the victim's swap execute at a worse price, and then immediately back-run to capture the price differential — at zero cost to the attacker: [2](#0-1) 

The public creation path for these swaps is unauthenticated/unprivileged — any signer can build and broadcast the contract: [3](#0-2) 

The only user protection is the `expected` (minimum-received) field checked in `doValidate`, which bounds the attacker's extractable profit per victim transaction but does not prevent the sandwich, and does not compensate the pool or the protocol in any way since the fee is zero: [4](#0-3) 

This mirrors the JOJO finding precisely: a state-changing, price-affecting operation reachable by any unprivileged actor, combined with the absence of any fee, converts ordinary MEV/front-running risk into a systematically profitable, cost-free extraction strategy against ordinary traders and against the exchange pool's implicit liquidity value.

### Impact Explanation
An attacker who monitors the pending-transaction pool for `ExchangeTransactionContract` calls can systematically extract value from every trader's slippage tolerance with no fee cost, degrading the effective execution price for all TRC10 exchange users and draining value from the liquidity pool (and thus indirectly from the pool's creator/depositors) over time. This constitutes ongoing theft of funds from users of a core, reachable protocol feature.

### Likelihood Explanation
High. Any account can observe pending transactions and submit competing transactions with higher priority (e.g., higher energy/bandwidth allocation or fee incentives to the block producer) ahead of a target swap, then submit a back-run swap. No special privilege, contract deployment, or bug in signature/permission verification is required — only participation as a normal order/transaction broadcaster, and the target action (a large TRC10 exchange swap) is common and easily identified from its `ExchangeTransactionContract` fields.

### Recommendation
- Introduce a non-zero swap fee in `ExchangeTransactionActuator.calcFee()` (e.g., a percentage of trade value returned to the pool or burned), removing the "free arbitrage" property that makes sandwiching costless.
- Consider commit-reveal or batch-auction style matching for exchange swaps, or minimum trade intervals per pool, to reduce the effectiveness of same-block sandwiching.
- Encourage/enforce tighter `expected` bounds and document the sandwich risk for integrators relying on the TRC10 Exchange module.

### Proof of Concept
1. Attacker watches the pending pool / broadcast stream for `ExchangeTransactionContract` transactions with large `quant` relative to pool depth (visible via `wallet/exchangetransaction` broadcast or P2P propagation).
2. Attacker submits Tx A: swap token X → Y in the same pool, sized to move the price against the victim, prioritized ahead of the victim's transaction.
3. Victim's original transaction executes at the worse post-Tx-A price (still passes because `expected` tolerance allows some slippage) via `ExchangeTransactionActuator.execute` and `ExchangeCapsule#transaction`.
4. Attacker submits Tx B: swap Y → X back, realizing the price differential as profit.
5. Because `calcFee()` for `ExchangeTransactionContract` is `0`, the attacker pays no fee for either leg beyond baseline bandwidth/energy costs, making the strategy consistently profitable whenever `quant` and slippage tolerance are large enough — reachable by any unprivileged order placer via the public API in `ExchangeTransactionServlet`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-150)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-224)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L227-235)
```java
  @Override
  public ByteString getOwnerAddress() throws InvalidProtocolBufferException {
    return any.unpack(ExchangeTransactionContract.class).getOwnerAddress();
  }

  @Override
  public long calcFee() {
    return 0;
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/ExchangeTransactionServlet.java (L22-36)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      ExchangeTransactionContract.Builder build = ExchangeTransactionContract.newBuilder();
      JsonFormat.merge(params.getParams(), build, params.isVisible());
      Transaction tx = wallet
          .createTransactionCapsule(build.build(), ContractType.ExchangeTransactionContract)
          .getInstance();
      JSONObject jsonObject = JSONObject.parseObject(params.getParams());
      tx = Util.setTransactionPermissionId(jsonObject, tx);
      response.getWriter().println(Util.printCreateTransaction(tx, params.isVisible()));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```
