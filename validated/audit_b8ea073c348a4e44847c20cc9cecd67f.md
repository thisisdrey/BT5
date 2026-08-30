### Title
`socialize-debt` decouples the `lindex` write-down from actual debt-ledger reduction via independently-rounded quantities - ([File: mainnet/contracts/vault/v0-vault-sbtc.clar])

### Finding Description
`socialize-debt` computes two conceptually-linked but numerically independent quantities from the same `scaled-amount` input: [1](#0-0) 

- `debt-reduction = mul-div-down(scaled-amount, idx, INDEX-PRECISION)` — used only to compute `new-lindex`.
- `principal-reduction = mul-div-down(scaled-amount, borrowed, scaled-principal)` — used to decrement `total-borrowed` and `assets`.

These two divisions use different divisors (`INDEX-PRECISION`, a fixed constant, vs. `scaled-principal`, a pool-wide aggregate that grows with total outstanding scaled debt) and both floor independently: [2](#0-1) 

Because `borrowed`/`scaled-principal` is only an *approximation* of `idx/INDEX-PRECISION` (it drifts from the "ideal" ratio due to independently-rounded increments across every prior `borrow`/`repay`/`accrue` operation on the pool), it is possible for a `scaled-amount` to satisfy `mul-div-down(scaled-amount, idx, INDEX-PRECISION) > 0` while `mul-div-down(scaled-amount, borrowed, scaled-principal) = 0` at the same time. When that happens:
- `lindex` is written down proportionally to `debt-reduction` (i.e., suppliers' redeemable value shrinks), while
- `total-borrowed` and `assets` are saturating-subtracted by `principal-reduction = 0` (no actual ledger reduction).

Repeating this with fresh dust positions (each opened cheaply via `collateral-add` → `borrow` → wait a block → self-liquidate, which presumably triggers `socialize-debt` inside the market's liquidation path in `mainnet/contracts/market/v0-4-market.clar`) compounds the `lindex` decay multiplicatively while `total-borrowed`/`assets` remain essentially unchanged, breaking the invariant that `total-borrowed` (and hence supplier redeemable value via `lindex`) should track real removed bad debt 1:1.

**Caveat on verification**: I was able to fully confirm the internal math flaw inside `socialize-debt` itself (both vault and market-mainnet contract are consistent across all vault variants). I was **not** able to fully confirm, within the available tool budget, the exact call site in `mainnet/contracts/market/v0-4-market.clar` that invokes `socialize-debt` during self-liquidation — specifically whether `scaled-amount` passed there is fully attacker-controlled (e.g., equal to the liquidated position's own leftover `principal-scaled`, which an attacker can size via choosing borrow amount) or is otherwise constrained. This is required to fully validate the "repeat N times" exploit chain end-to-end, and I flag this as an open verification gap. `check-caller-auth` (referenced in `socialize-debt`) likely restricts direct calls to the market contract only, so the attacker's actual entry point is the market's liquidation function, not `socialize-debt` directly — that authorization gate is not itself broken, but the internal rounding mismatch it guards is.

### Impact Explanation
If confirmed reachable end-to-end, this is a **Critical / protocol insolvency** issue: `lindex` governs the redemption value of supplier shares, and its value is written down as if real debt were extinguished, while `total-borrowed`/`assets` (the actual debt/asset ledger) are saturated to a smaller (or zero) reduction. Over many repetitions this permanently decouples the sum of user debt (`principal-scaled * idx`) from the assets actually backing supplier shares (`lindex`-priced), destroying value without a corresponding write-off, which is the definition of insolvency in scope.

### Likelihood Explanation
Per-call decay is bounded by the size of `debt-reduction` relative to `old-total-assets`, so a single dust cycle produces a negligible effect; achieving material drift requires many repetitions (N large), each needing a new dust position, a 1-block wait (to dodge the same-block-borrow guard), and a self-liquidation transaction — feasible in principle at low capital cost but bounded by attacker patience/gas cost, and by whether `scaled-amount` passed into `socialize-debt` from the market's liquidation logic is actually attacker-shaped to hit the rounding-mismatch boundary each time (unverified — see caveat above).

### Recommendation
Derive `principal-reduction` and the `lindex` write-down from a single consistent quantity instead of two independently-rounded computations — e.g., compute `principal-reduction` first from `scaled-amount` and `scaled-principal`/`borrowed`, then derive `debt-reduction` from that same `principal-reduction` (or vice-versa), so that a zero ledger reduction can never coincide with a non-zero `lindex` write-down. Additionally, consider requiring `principal-reduction > 0` (or bounding `scaled-amount` below a minimum) before allowing `lindex` to be written down at all.

### Proof of Concept
Not fully producible without confirming the market-side call site into `socialize-debt` (see caveat). A Clarinet/vitest simnet PoC would need to:
1. Read `v0-4-market.clar`'s liquidation function to confirm it computes `scaled-amount` from the liquidated position's own `principal-scaled` and passes it to `socialize-debt`.
2. Seed the vault with a large pool `principal-scaled`/`total-borrowed` (many normal borrowers) to create rounding "slack".
3. Open N dust borrow positions (1 block apart), self-liquidate each, and assert after each cycle: `principal-reduction == 0` (no change in `total-borrowed`) while `lindex` strictly decreases.
4. After N cycles, compare cumulative `lindex` decay ratio vs. cumulative real `total-borrowed` reduction (expected: `total-borrowed` reduction ≈ 0 while `lindex` has decayed), confirming the decoupling.

Given the unverified market-side call path, this cannot currently be certified as a fully proven, reproducible exploit chain — it is a **structural rounding-inconsistency finding inside `socialize-debt`** that likely needs confirmation against `v0-4-market.clar`'s liquidation logic before final severity sign-off.

### Citations

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L946-960)
```text
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L961-968)
```text
    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

    (print {
      action: "socialize-debt",
      caller: contract-caller,
```
