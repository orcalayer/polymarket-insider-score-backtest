# Method and pre-registration

We wrote the Early score method into a rule file on October 8, 2026, before computing any score. Every later change was added
as a new dated line with its reason. This page summarizes that file. Wallet addresses that appear in our internal copy (the
public cases we used to sanity-check the direction of the score) are left out.

## Entries

- **Trades.** Every on-chain Polymarket trade, with exchange and adapter contracts excluded so that an exchange never counts
  as a wallet or doubles a fill.
- **Entry.** A wallet's net position change in one market over one UTC day: shares of Yes minus shares of No. The larger side
  keeps its average price; the size in dollars is the net shares times that price. Selling No counts as buying Yes. A
  same-day round trip nets to zero, and so does buying both sides.
- **Minimum.** $100 per entry.
- **Excluded markets.** Sports and esports, and short Up or Down markets. A market is treated as sports if its category says
  so, or its slug looks like `league-team-team-YYYY-MM-DD`, or its question contains "vs", "Completed Match", "Spread:",
  "O/U", "Both Teams to Score" or "Moneyline". Categories alone are incomplete: in September 2026 most uncategorized markets
  were sports.

## Price after and move

- **Price after H hours** (H = 1 and 24): the dollar-weighted price of the side the wallet holds in other wallets' trades in
  the hour from t + H to t + H + 1, where t is the time of the entry's last trade. The hour counts only if other wallets traded
  at least $100 in it. If not, the latest such hour between t and t + H. If none, and the market closed with no trades after
  t + H, its final price. Otherwise the entry has no move for that H.
- **Move** = price after minus entry price, in cents, for the side held.

## Score

- **Weight** of an entry: min(size, $10,000) / $1,000.
- **Average move** m̄: weighted mean of the wallet's moves. **Effective entries** n = (Σw)² / Σw².
- **Shrinkage:** M = m̄ · n / (n + K), K = 5. A wallet with 5 effective entries keeps half of its raw average; one entry keeps a
  sixth.
- **Neutral band:** |M| under 1 cent (the usual price tick) scores 50. Outside the band, the percentile of M, cut to 0 to 49 below
  and 51 to 100 above, so the score stays monotone in M.
- **Groups.** Top 10% = score 90 or more; bottom 10% = 10 or less.

## The test written down in advance

- Score on period A, October 1, 2025 to March 31, 2026. Measure the same wallets on period B, April 1 to September 30, 2026.
- Pass if the top 10% of A has a positive move in B, counted with errors clustered by event and again without the 5 largest
  events. If not, say that the score describes the past and promises nothing.
- Also report the spread of scores, the share of sports in the top 10% (zero after the sports rule) and the share of top
  wallets with a single entry (26%).
- Publish the result whatever it is.

## Changes, in order (all before the period test)

| Date | Change | Why |
|---|---|---|
| October 8 | Group trades by exposure: buying Yes and selling No are one entry | One order was written as two rows, "bought Yes at 0.07" and "sold No at 0.93", and counted twice |
| October 8 | Shrink by effective entries; fix K = 5 | The first form assumed small entries are noisier; in the data the spread of a move was about 28 cents whatever the size, and K could not be estimated from the data |
| October 10 | Net position per wallet per day | On a test day, the top of the list was a group of wallets trading with each other at made-up prices in finished tennis matches (bought at 0.02, sold to a partner at 0.96 four minutes later). Netting removes it. An earlier fix that used only one side of each fill was dropped because it did not work on older data |
| October 10 | Price after needs $100 of other wallets' trades in the hour | In one market, trades of $1.50 to $15 at 0.15 set the price 24 hours later, though the market settled at 1 |
| October 10 | Sports and Up or Down markets out | A day after a match its price is the result; sports were about 70% of entry dollars on a test day, so the score would rank sports bettors |
| October 10 | Neutral band of 1 cent | The percentile stretched noise near zero: a market maker with M = +0.33 cents scored 75 |
| October 15 | Two more exchange contracts added to the exclusion list | Applying the existing rule to contracts identified later |

On a rerun over September 2026 data with the final rules, the method-of-moments estimate of K came out near 23 against the fixed 5. We
kept 5 and report other values in `data/k_scan.csv`: the result has the same sign for K from 0 to 100.

## Checks after the test (not pre-registered)

- **Mechanism.** Move after 1 hour against 24 hours, and rank correlation from A to B of the first-hour move and of the drift
  from 1 to 24 hours, on 32,867 wallets with at least 3 entries on both horizons in both periods.
- **Held to settlement.** Each period B entry in a resolved market held to its payout (1 or 0 for the side held), minus the
  entry price. 85.9% of period B entries are in closed, resolved markets; the rest are left out.
- **Fees.** The fee each wallet actually paid in that market on that day, from on-chain data (only takers pay; the rate depends
  on the category; geopolitics is free). Rebates are not added. An entry is "mostly taker" if 50% or more of that day's buying,
  in dollars, went through taker orders, "mostly maker" if less, and "built by selling" if the wallet bought nothing that day
  and the position came from selling the opposite outcome.
- **Dollars per $10.** 10 × Σ[size × (payout / price - 1)] / Σ size, every dollar counted. Robustness: without the 5 largest
  events, the 5 largest wallets, both, and the largest 1% of entries.
- **By entry price and by month**, in `data/buckets_settlement.csv` and `data/months_net.csv`.

## Limits

- Groups, not wallets. A group average says nothing certain about one wallet in it.
- One split of time: one half-year to the next.
- The t-statistics are clustered by event, but wallets in the same group can still trade together.
- Open markets at the end of September 2026 are not in the settlement numbers.
