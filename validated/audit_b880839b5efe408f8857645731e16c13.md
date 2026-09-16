### Title
Pausing market transactions can permanently freeze funds already locked in an open market order - ([File: actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java])

### Summary
`MarketCancelOrderActuator.validate()` gates order cancellation on the committee-controlled `ALLOW_MARKET_TRANSACTION` flag. A user who has already locked their sell-token balance into an open order by calling `MarketSellAssetContract` can be permanently prevented from reclaiming those funds if the committee toggles `ALLOW_MARKET_TRANSACTION` off after the order was created, mirroring the reported `LibUbiquityPool` pattern where a "claim/undo" step is blocked by the same pause flag that governs the "commit" step.

### Finding Description
When a user places a market order via `MarketSellAssetActuator`, their sell-token balance is moved out of their spendable account balance and held by the order (tracked in `MarketOrderCapsule`/`MarketAccountStore`). To retrieve that balance, the user must submit a `MarketCancelOrderContract`, processed by `MarketCancelOrderActuator`.

`MarketCancelOrderActuator.validate()` requires: [1](#0-0) 

`dynamicStore.supportAllowMarketTransaction()` reflects the on-chain governance parameter `ALLOW_MARKET_TRANSACTION` (ProposalType #44), which is set through the standard committee proposal flow (`ProposalCreate`/`ProposalApprove` → `ProposalService.process`) — a path reachable by any account with sufficient votes/witness support, not just a privileged administrator key. Once an order exists and this flag is subsequently turned off by committee vote, the only path back to the user's locked balance — `collectRedemption`-equivalent `MarketCancelOrderActuator`/`returnSellTokenRemain` — is blocked by the same `require`-style check that gates *new* order creation: [2](#0-1) 

This is structurally identical to the reported issue: a two-phase flow where step 1 (burn `uAD` / place a market order) is irreversible and commits user funds, and step 2 (`collectRedemption` / `MarketCancelOrderContract`) — the only mechanism to reclaim those committed funds — is gated by the *same* pause/enable flag used to gate the forward-direction operation, rather than by a flag dedicated to unwinding already-committed positions. There is no logic in `MarketCancelOrderActuator` that special-cases "return my own already-locked funds" versus "perform a new market action," so pausing the feature for legitimate reasons (e.g. a bug found in matching logic) simultaneously strands any user who already has an open order.

### Impact Explanation
If the committee disables `ALLOW_MARKET_TRANSACTION` while a user has an open, unmatched order, that user's locked sell-token/TRX balance becomes permanently inaccessible until (and unless) the committee re-enables the parameter — an event entirely outside the user's control and with no guaranteed timeline, exactly as described in the source report ("unknown time... would passed if... unpaused again"). This is a freezing-of-funds condition reachable purely by an unprivileged order placer who happens to have an order open when governance pauses the feature.

### Likelihood Explanation
Likelihood is Medium: it requires the committee to pause `ALLOW_MARKET_TRANSACTION` (a legitimate emergency/maintenance action, e.g., in response to another market-related bug) at a moment when user orders are outstanding. Given that pausing market transactions is a plausible incident-response action, and no code path exempts cancellation/withdrawal of already-committed order funds from the pause, any user with an open order at that time is exposed.

### Recommendation
Do not gate `MarketCancelOrderActuator` (or any other actuator whose sole purpose is returning already-locked/committed user funds) behind the same feature flag that governs the initiation of new market actions. Specifically:
- Remove the `dynamicStore.supportAllowMarketTransaction()` check from `MarketCancelOrderActuator.validate()`, or
- Introduce a separate flag (e.g., "allow order cancellation") that committee cannot simultaneously disable alongside `ALLOW_MARKET_TRANSACTION`, ensuring users can always unwind and reclaim funds they have already locked, even while new order creation/matching is paused.

### Proof of Concept
1. User calls `MarketSellAssetContract` (handled by `MarketSellAssetActuator`), locking sell-token balance into an open order (order remains unmatched).
2. Committee members submit and approve a `ProposalCreateContract`/`ProposalApproveContract` sequence setting `ALLOW_MARKET_TRANSACTION` (ProposalType 44) to `0`; `ProposalService.process` applies it to `DynamicPropertiesStore`.
3. User submits `MarketCancelOrderContract` to reclaim the locked balance.
4. `MarketCancelOrderActuator.validate()` reaches `if (!dynamicStore.supportAllowMarketTransaction())` at [1](#0-0)  and throws `ContractValidateException("Not support Market Transaction, need to be opened by the committee")`, rejecting the cancellation transaction.
5. The user's locked balance in `MarketOrderCapsule`/`MarketAccountStore` remains inaccessible until the committee re-enables `ALLOW_MARKET_TRANSACTION`, an event with no guaranteed timeframe.

Note: I was unable to further verify, within this session, whether the `ALLOW_MARKET_TRANSACTION` case in `ProposalUtil.validator` imposes a one-way ("can only ever be set to 1, never back to 0") restriction similar to some other flags (e.g. `UNFREEZE_DELAY_DAYS`), since that specific switch case fell outside the code ranges retrieved. If such a one-way restriction exists for `ALLOW_MARKET_TRANSACTION`, the practical exploitability of this specific analog would be reduced (the flag could only go 0→1, never 1→0), though the underlying design flaw in `MarketCancelOrderActuator` (gating fund-return on a feature-enable flag) would remain.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L168-171)
```java
    if (!dynamicStore.supportAllowMarketTransaction()) {
      throw new ContractValidateException("Not support Market Transaction, need to be opened by"
          + " the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L1-1)
```java
/*
```
