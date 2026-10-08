---
name: khetru
last_updated: 2026-10-08
---

# khetru Strategy

## Purpose

Indian smallholders make recurring decisions where money is at risk — when to sow, what fertilizer to apply, whether to spray — on the word of dealers, neighbours and habit, even though free public agricultural data bears on them. The crux: that data arrives too coarse, stale, in the wrong form and at the wrong moment to out-trust a face-to-face source whose advice is tied to what they sell.

## Positioning

khetru wins by being a free, open-source, non-selling companion that turns public data into insights on the farmer's own field and decisions — with honest sources and uncertainty — rather than handing over raw data or teaching in the abstract. Farmers who see it work hands-on come to trust data-driven farming, whether they stay with khetru or move to a paid product later.

## Users

**Primary:** Village smallholders in Himachal Pradesh — Android-phone cultivators of own or leased land (roughly ≤5 acres) who make their own input and timing decisions. They're hiring khetru to tell them what their soil and the weather mean for a sow, fertilize or spray decision on their field, before they make it.

## Boundaries

- Commercial apple growers and orchardists are not targeted; they are mostly well-off and already agtech-aware.
- No dashboards for public officers or authorities; farmer-direct first, with KVKs and FPOs as channels for spreading the word only.
- Chat assistant is a future core feature, held until claim reliability shows the underlying insights hold.
- No in-app decision prompts until 5000 farmers return for a second crop cycle; opt-in after that.
- No new data domain reaches farmers until it informs a primary-user decision and the Evidence track can verify its claims.

_Resist a change when:_ it serves someone other than the village smallholder, makes farmers do work for khetru, or puts a claim in front of them the Evidence track can't verify.

## Key metrics

- **Claim reliability** - share of khetru's falsifiable claims that held, scored so stated uncertainty and honest "can't tell" count; weekly, server-side against public observations per altitude band, no user data.
- **Next-cycle return** - share of farmers who come back unprompted for the same decision type in the next crop cycle; seasonal, passive aggregate counts. Also sets the 5000-farmer threshold.
- **Understood-and-unharmed rate** - in a small opt-in panel, share who can explain their decision in data terms and report no loss from following khetru; seasonal, independent interviewer.
- **Decision-moment consult share** - share of reported money-at-risk decisions, including "wait", preceded by a khetru consult; weekly, pooled by crop stage. Active only after the 5000-farmer threshold, opt-in.
- **Contested follow rate** - where khetru's advice differed from the farmer's default (dealer, neighbour, SHC), share where the farmer followed khetru, with non-follows coded "didn't trust" vs "constraint"; weekly. Active only after the threshold, opt-in.

All metrics are aggregate-only, with small cells suppressed; no stored farm coordinates or cross-season linking without consent. Instrumentation lives in a separate measurement spec.

## Tracks

### Soil & weather insight

Turning public soil and weather data into calls for one decision on one field, with honest uncertainty.

_Why it serves the approach:_ it is the "insights on the farmer's own field" that the positioning promises.

### Evidence

Server-side checking of khetru's own claims against what actually happened, with no work asked of farmers.

_Why it serves the approach:_ it is where "backed by results" comes from, and it gates the chat assistant and any new data domains.

### Farmer reach in Himachal

Website proof of concept, then an Android app as the farmer MVP; Hindi/Pahari voice and icons; low bandwidth; FPO and KVK channels.

_Why it serves the approach:_ hands-on trust needs to reach village smallholders where they are.

### Open platform

Open-source monorepo, contributors, and pluggable data sources so khetru can extend or pivot. A new data domain reaches farmers only when it informs a money-at-risk decision our primary user makes and the Evidence track can verify its claims; until then it may be merged as a dormant plugin.

_Why it serves the approach:_ free, open and non-selling is the positioning; extensibility keeps khetru from being boxed into soil and weather.
