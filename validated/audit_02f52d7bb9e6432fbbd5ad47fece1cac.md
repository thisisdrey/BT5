No vulnerability found for this question.

Based on my research, this NEAR Intents codebase has no analog to the Pendle PT Oracle bug class. The reported vulnerability concerns an EVM contract (`OraclePendlePT.sol`) assuming a fixed 1e18 scaling factor from an external `getPtToSyRate()` oracle call. Searching the in-scope directories (`contracts/defuse/**`, `contracts/wallet/**`, `contracts/poa/**`, `contracts/global-deployer/src/**`, `contracts/outlayer/app/src/**`, `contracts/treasury-logger/src/**`, `crates/**`), there is:

- No external price-oracle integration comparable to Pendle's `getPtToSyRate()`.
- The only decimal-scaling primitive is `UD128` in [1](#0-0)  which self-normalizes based on digits it's constructed with — it isn't populated from an external contract call assumed to be a fixed 1e18 scale.
- The only place `UD128`-based pricing is used for settlement math is `contracts/escrow-swap/src/contract/fill.rs`, where the price comes directly from the taker's own signed `FillAction.price` field (validated against `params.price`), not from any external decimal-ambiguous oracle call [2](#0-1) . This contract is explicitly out of scope per the rules (`contracts/escrow-swap/**` reject on sight).
- `contracts/defuse`'s `TokenDiff`/`TokenDeltas` conservation logic operates purely on user-declared signed deltas with no dependency on any fetched external "rate" value [3](#0-2) .

Since no in-scope code path calls an external rate/oracle function and assumes a fixed decimal scale that could be mismatched against actual returned values, there is no reachable custody-binding violation analogous to this bug class.

### Citations

**File:** crates/primitives/decimal/src/lib.rs (L25-66)
```rust
pub struct UD128(u8, u128);

impl UD128 {
    #[allow(clippy::as_conversions, clippy::cast_possible_truncation)]
    pub(crate) const MAX_DECIMALS: u8 = u128::MAX.ilog10() as u8;

    pub const MIN: Self = Self(Self::MAX_DECIMALS, 1);
    pub const MAX: Self = Self(0, u128::MAX);

    pub const ZERO: Self = Self(0, 0);
    pub const ONE: Self = Self(0, 1);

    #[inline]
    pub const fn new(mut decimals: u8, mut digits: u128) -> Option<Self> {
        if digits == 0 {
            return Some(Self::ZERO);
        }

        // normalize
        {
            macro_rules! strip {
                ($shift:expr) => {{
                    const FACTOR: u128 = 10u128.pow($shift);
                    while decimals >= $shift && digits % FACTOR == 0 {
                        digits /= FACTOR;
                        decimals -= $shift;
                    }
                }};
            }
            strip!(16);
            strip!(8);
            strip!(4);
            strip!(2);
            strip!(1);
        }

        if decimals > Self::MAX_DECIMALS {
            return None;
        }

        Some(Self(decimals, digits))
    }
```

**File:** contracts/escrow-swap/src/contract/fill.rs (L127-148)
```rust
    fn taker_swap(
        &self,
        taker_dst_in: u128,
        taker_price: UD128,
        partial_fills_allowed: bool,
    ) -> Result<(u128, u128)> {
        let taker_want_src = <u128 as CheckedDiv<UD128>>::checked_div(taker_dst_in, taker_price)
            .ok_or(Error::IntegerOverflow)?;
        if taker_want_src < self.maker_src_remaining {
            if !partial_fills_allowed {
                return Err(Error::PartialFillsNotAllowed);
            }
            Ok((taker_want_src, taker_dst_in))
        } else {
            Ok((
                self.maker_src_remaining,
                self.maker_src_remaining
                    .checked_mul_ceil(taker_price)
                    .ok_or(Error::IntegerOverflow)?,
            ))
        }
    }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L1-30)
```rust
use super::ExecutableIntent;
use crate::{
    AccountId, AccountIdRef, DefuseError, Result,
    accounts::AccountEvent,
    amounts::Amounts,
    engine::{Engine, Inspector, State, StateView},
    events::DefuseEvent,
    fees::Pips,
    intents::MaybeIntentEvent,
    token_id::{TokenId, TokenIdType},
};
use defuse_num_utils::CheckedMulDiv;
use impl_tools::autoimpl;
use serde::{Deserialize, Serialize};
use serde_with::{DisplayFromStr, serde_as};
use std::{borrow::Cow, collections::BTreeMap};

pub type TokenDeltas = Amounts<BTreeMap<TokenId, i128>>;

#[autoimpl(Deref using self.diff)]
#[autoimpl(DerefMut using self.diff)]
#[serde_as]
#[cfg_attr(feature = "schemars-v0_8", derive(::schemars::JsonSchema))]
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
/// The user declares the will to have a set of changes done to set of tokens. For example,
/// a simple trade of 100 of token A for 200 of token B, can be represented by `TokenDiff`
/// of {"A": -100, "B": 200} (this format is just for demonstration purposes).
/// In general, the user can submit multiple changes with many tokens,
/// not just token A for token B.
pub struct TokenDiff {
```
