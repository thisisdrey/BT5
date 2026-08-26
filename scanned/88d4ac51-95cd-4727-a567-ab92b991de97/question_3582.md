# Q3582: shard layout account-to-shard mapping stability — sharding.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, account ids chosen adjacent to boundary accounts, including maximum-length ids, when transaction conversion cost alone approaches the chunk gas limit, and additionally when the pool is filled exactly to its bound by many attacker keys, reach `get_receipts_shuffle_salt` in `chain/chain/src/sharding.rs` and make two components map one account to different shards, breaking the invariant that account-to-shard mapping is one deterministic function used everywhere, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `chain/chain/src/sharding.rs` :: `get_receipts_shuffle_salt`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: account ids chosen adjacent to boundary accounts, including maximum-length ids; when transaction conversion cost alone approaches the chunk gas limit; when the pool is filled exactly to its bound by many attacker keys
- Exploit idea: make two components map one account to different shards
- Invariant to test: account-to-shard mapping is one deterministic function used everywhere
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test over boundary account ids across all mapping call sites
