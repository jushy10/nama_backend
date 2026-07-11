# Nama Insights — Launch & Off-Page SEO Assets

Copy-paste-ready marketing assets for launching **Nama Insights** (https://www.namainsights.com),
a free stock & ETF research tool with a free-cash-flow focus, daily-fresh fundamentals, and
AI-generated analysis (Claude on Amazon Bedrock).

> **Informational only, not investment advice.** Every asset here is deliberately written to
> avoid hype, guarantees, price predictions, or anything that reads as a recommendation to buy
> or sell. Keep it that way when you edit — it protects the project and it's what these
> communities respond to.

---

## 0. Principles (read before posting anything)

- **Value first.** Lead with something useful — a metric, a screenshot, a genuine question —
  not "check out my product." Every post should stand on its own even if nobody clicks.
- **Disclose you're the maker.** Say "I built this" plainly. Every sub and directory expects
  it; hiding it is what gets accounts banned.
- **Never frame it as advice.** No "great buy," no "undervalued," no targets. It's a research
  tool that surfaces data; the reader decides. Say that explicitly.
- **Don't spam.** One post per community, spaced out. Respect the ~9:1 rule (nine genuinely
  useful/unrelated contributions for every one self-promo). Reply to comments like a person.
- **Space out submissions.** Directories, Reddit, HN, X — stagger over 2–3 weeks, not one
  afternoon. A wall of same-day identical posts looks coordinated and gets filtered.

---

## 1. Product Hunt launch kit

**Tagline (≤60 chars):**
> Free stock research with a cash-flow focus + AI (50 chars)

Alternates:
> Free stock & ETF research, FCF-first, AI-analyzed (49)
> See any stock's free cash flow — free, no login (47)

**Short description (the one-liner under the name):**
> Nama Insights is a free stock & ETF research tool for US-listed companies — live quotes,
> fundamentals, earnings history, analyst coverage, and free-cash-flow metrics most free tools
> bury, plus plain-English AI analysis. No login, no paywall.

**Longer description:**
> I kept opening five tabs to research a stock's cash flow — free cash flow yield, price/FCF,
> operating cash flow — because most free tools hide it three clicks deep or leave it out
> entirely. So I built Nama Insights.
>
> It's a free research tool for US-listed stocks and ETFs. For any ticker you get:
> - **Free-cash-flow metrics up front** — FCF yield, price/FCF, OCF yield — not buried
> - Live quotes and fundamentals, with **quarterly & annual earnings history**
> - **Analyst coverage** — recommendation trends, price targets, upgrades/downgrades
> - **Revenue segments** pulled straight from SEC filings (what a company actually sells)
> - A **stock screener** and a **plain-English AI screener** ("show me profitable companies
>   with high FCF yield")
> - An **ETF screener** and a **market heat map**
> - **AI-generated analysis** (Claude on Amazon Bedrock) that reads the data and explains it
>   in plain English
>
> Data refreshes daily. There's no account and no paywall.
>
> It's **not** trying to beat Yahoo Finance on real-time quotes — the niche is cash-flow-oriented
> fundamental research. It's informational only, not investment advice. I'd love feedback on
> what metrics or views are missing.

**Maker's first comment:**
> Hey Product Hunt! Maker here.
>
> I'm a DIY investor and I built Nama because free tools make cash-flow analysis annoyingly
> hard — free cash flow yield, price/FCF and operating cash flow are usually buried or missing,
> even though they're what I actually care about when I'm comparing companies. Nama puts those
> front and center for any US-listed stock or ETF, alongside earnings history, analyst coverage,
> revenue segments from SEC filings, and screeners.
>
> Two things I had fun building: an **AI screener** where you type what you want in plain English
> and it turns it into filters, and **AI analysis** (Claude on Bedrock) that reads a ticker's data
> and explains it in plain language. Everything refreshes daily. No login, no paywall.
>
> To be honest about what it isn't: it's not a real-time trading terminal and it's not investment
> advice — it's a research tool for people who like digging into fundamentals, especially cash flow.
>
> I'd genuinely love feedback: what's missing, what's confusing, what metric you wish it had.
> Happy to answer anything. Thanks for taking a look!

**Suggested topics / tags:** `Fintech`, `Investing`, `Analytics`, `Artificial Intelligence`,
`SaaS` (or `Web App`). Pick 3–4; Product Hunt usually caps at four.

**Best launch-day timing:** Post at **12:01 AM PT** (Product Hunt's day resets on Pacific time)
so you get a full day to accumulate upvotes; **Tuesday–Thursday** are the highest-traffic,
most-competitive days, while **Sunday/Monday** are quieter and easier to rank on if you're not
chasing a top-3 finish.

---

## 2. Reddit posts

**Reddit rules of thumb — read first:**
- **The ~9:1 rule** applies everywhere: for every self-promo post, you should have ~9 genuinely
  helpful, non-promotional contributions. A brand-new account whose only activity is dropping a
  link will get removed and can get shadowbanned. Comment usefully in these subs for a week or
  two *before* you post a link.
- **r/investing** — strict. Self-promotion and "tool I made" posts are frequently removed;
  there's a rule against promoting your own site/app/blog. Safest path: contribute in comments,
  or post a genuinely useful **data/analysis** piece (no link in the body) and only mention the
  tool if someone asks. Consider the **Daily Discussion** thread instead of a standalone post.
- **r/stocks** — also restricts self-promotion; has a dedicated rule and periodic removal of
  "my app/website" posts. A "feedback on a free tool I built, no signup" post *sometimes*
  survives if it's clearly non-commercial and you engage — but check the sidebar/wiki and
  consider messaging the mods first.
- **r/SecurityAnalysis** — smaller, high-signal, value-heavy. Low-effort promo dies fast, but a
  genuinely substantive post about *methodology* (e.g. how you compute cash-flow metrics, sourcing
  segments from XBRL) can land well. Lead with the analysis, not the tool.
- **Also worth it, generally more promo-tolerant:** r/ValueInvesting, r/dividends,
  r/fatFIRE-adjacent DIY subs, r/algotrading (for the AI/data angle), and smaller subs like
  r/UndervaluedStonks. Always check each sidebar.

> When in doubt, message the mods before posting a link. A one-line "is this allowed?" saves a
> ban.

### Draft A — r/SecurityAnalysis / r/ValueInvesting (value-first, methodology-led)

**Title:** Pulling revenue-segment breakdowns straight from 10-K XBRL — what I learned building a free research tool

**Body:**
> I've been building a free stock research tool and wanted to share one part that turned out
> harder than expected, in case it's useful to anyone doing fundamental work.
>
> I wanted per-company **revenue by segment / product / geography** — the stuff in the notes, like
> Google Services vs Google Cloud, or iPhone vs Services. The clean SEC JSON APIs
> (`companyconcept`/`companyfacts`) only return the *consolidated* value of a concept; they drop
> the dimensional breakdown. That breakdown only lives in the filing's raw XBRL instance document,
> so you have to walk ticker → CIK → latest 10-K → the `_htm.xml` and parse the dimensioned facts.
>
> The subtle part: filers often tag a fact with **two** axes at once (a product *within* a
> business segment), so a naive single-axis filter silently drops the whole product cut. You have
> to sum across segments to recover the product total.
>
> I also lean the tool toward **cash-flow metrics** (FCF yield, price/FCF, OCF yield) since those
> are what I actually compare on and most free tools bury them.
>
> Happy to go deeper on the parsing if anyone's interested — and if it's useful, it's free and
> there's no login: https://www.namainsights.com. It's informational only, not advice. Would love
> to hear how others source segment data.

### Draft B — r/stocks (feedback framing — check sidebar/mods first)

**Title:** I built a free, no-login stock research tool that puts free cash flow front and center — feedback welcome

**Body:**
> Longtime lurker, DIY investor. I got tired of opening five tabs every time I wanted a stock's
> free cash flow yield, price/FCF, and operating cash flow — most free tools bury those or skip
> them — so I built a tool that shows them up front.
>
> For any US-listed stock or ETF you get: live quote + fundamentals, quarterly & annual earnings
> history, analyst coverage (recommendation trends, price targets, upgrades/downgrades), revenue
> segments from SEC filings, a screener, an ETF screener, a heat map, and an AI-generated plain-
> English analysis. Data refreshes daily. No account, no paywall.
>
> It's **not** a real-time trading terminal and it's **not** investment advice — it's for
> fundamental research, especially cash-flow-oriented. I'm the maker and I'm mainly after honest
> feedback: what's missing, what metric you'd add, what's confusing.
>
> Link if you want to poke at it: https://www.namainsights.com
>
> (Mods — happy to remove if this isn't allowed; wasn't sure and erred toward asking for feedback.)

### Draft C — r/investing Daily Discussion comment (safest, no standalone post)

> Been comparing a few names on cash flow this week and finally got sick of digging for FCF yield /
> price/FCF on free sites, so I put together a tool that surfaces those up front (plus earnings
> history, analyst coverage, SEC revenue segments). It's free and no-login. Full disclosure, I made
> it — not advice, just a research aid. If cash-flow metrics are your thing I'm happy to hear what
> you'd want added. Not going to drop a link in the daily unless that's cool with mods.

---

## 3. X / Twitter launch thread (5–7 tweets)

**1/**
> I built a free stock research tool that puts the metric most free sites bury front and center:
> free cash flow.
>
> FCF yield, price/FCF, operating cash flow — for any US stock or ETF. No login, no paywall.
>
> https://www.namainsights.com 🧵

**2/**
> Why cash flow?
>
> Earnings can be massaged; cash is harder to fake. When I compare companies I want FCF yield and
> price/FCF side by side — and I was tired of opening five tabs to find them.
>
> So Nama shows them up front for every ticker.

**3/**
> For any US-listed stock or ETF you get:
>
> • Live quote + fundamentals
> • Quarterly & annual earnings history
> • Analyst coverage — trends, price targets, upgrades/downgrades
> • Revenue segments pulled from SEC filings
> • Screeners + a market heat map
>
> Refreshed daily.

**4/**
> The part I had the most fun with: a plain-English AI screener.
>
> Type "profitable companies with high free-cash-flow yield" and it turns your words into filters.
> No memorizing field names.

**5/**
> And AI-generated analysis (Claude on Amazon Bedrock) that reads a ticker's data and explains it
> in plain language — earnings trend, cash flow, valuation, analyst view — in a few paragraphs.

**6/**
> What it's NOT:
>
> • Not a real-time trading terminal (Yahoo/your broker win there)
> • Not investment advice — it's a research aid, you decide
>
> Its niche is cash-flow-first fundamental research. That's the whole point.

**7/**
> It's free and there's no signup, so just poke around: https://www.namainsights.com
>
> I'm the maker and I'd love feedback — what metric or view is missing? Reply or DM. 🙏

---

## 4. Hacker News — Show HN

**Title** (HN prefers plain and specific; no emoji, no hype):
> Show HN: Nama Insights – free stock research with a free-cash-flow focus and AI analysis

**First comment:**
> Hi HN, maker here.
>
> I'm a DIY investor and built this because free stock tools make cash-flow analysis harder than
> it should be — free cash flow yield, price/FCF and operating cash flow are usually buried or
> missing, even though they're what I compare companies on. Nama surfaces them up front for any
> US-listed stock or ETF, along with earnings history, analyst coverage, SEC revenue segments,
> screeners, and a heat map. Data refreshes daily; no login, no paywall.
>
> A few technical notes that might interest this crowd:
> - It's a FastAPI backend in a clean-architecture vertical-slice layout — each data source
>   (Alpaca for prices, Finnhub for fundamentals, Yahoo/yfinance for earnings & analyst data,
>   SEC EDGAR for segments) is isolated behind a port so vendors are swappable.
> - Revenue segments come from parsing the raw 10-K XBRL instance, because SEC's clean JSON APIs
>   drop the dimensional (segment) breakdown and only return consolidated concept values.
> - There's an AI screener that translates plain English into structured filters, and AI analysis
>   over a ticker's data — both run Claude on Amazon Bedrock.
> - The public content/SEO pages are server-rendered from the DB (no live vendor calls per crawl),
>   since the app itself is a client-rendered SPA that crawlers can't see.
>
> It's informational only, not investment advice, and deliberately not trying to be a real-time
> trading terminal — the niche is cash-flow-oriented fundamental research.
>
> Happy to answer questions about the architecture, the data sourcing, or the cost of running it.
> Feedback very welcome, especially on what's missing.

> **HN timing note:** submit weekday mornings ~**8–10 AM ET** for the best shot at the front page;
> be around to answer comments for the first couple of hours.

---

## 5. Directory / backlink submission checklist

Legend: **[DF]** typically gives a real *dofollow* backlink · **[NF/disc]** nofollow or
discovery-only (traffic/eyeballs, little/no link equity) · **[varies]** depends on listing tier
or moderation. Link-follow status changes over time — verify with a "check nofollow" browser
extension after you're listed.

### General startup / product / tool directories
- **Product Hunt** — https://www.producthunt.com — [NF/disc] huge discovery, links are nofollow but the traffic + secondary pickups are the point
- **BetaList** — https://betalist.com — [varies] good for early-stage discovery
- **AlternativeTo** — https://alternativeto.net — [DF] list it as a free alternative to Yahoo Finance / Finviz / Stock Analysis; strong, relevant backlink
- **SaaSHub** — https://www.saashub.com — [DF] software directory, dofollow on listing
- **Startup Stash** — https://startupstash.com — [varies] curated
- **G2** — https://www.g2.com — [NF/disc] mostly nofollow but high-authority discovery/reviews
- **Capterra / GetApp** — https://www.capterra.com — [NF/disc] finance-software category, discovery + reviews
- **Crunchbase** — https://www.crunchbase.com — [NF/disc] company profile; good for entity/knowledge-graph presence
- **Slant** — https://www.slant.co — [varies] "best free stock research tools" style lists
- **SaaSworthy** — https://www.saasworthy.com — [varies]
- **Startup Ranking** — https://www.startupranking.com — [DF] often dofollow
- **Launching Next** — https://www.launchingnext.com — [varies]
- **Uneed / Toolfolio / Fazier** — smaller "new tools" directories — [varies] quick, low-effort listings

### AI-tool directories (it has an AI screener + AI analysis — lean into this)
- **There's An AI For That** — https://theresanaiforthat.com — [NF/disc] the biggest AI-tool directory; huge discovery
- **Futurepedia** — https://www.futurepedia.io — [varies] large AI directory
- **Future Tools** — https://www.futuretools.io — [varies]
- **AI Tool Hunt / AI Scout / AITools.fyi** — [varies] smaller AI directories, fast to submit
- **Toolify.ai** — https://www.toolify.ai — [varies]
- **Insidr.ai / TopAI.tools / AI Tool Directory** — [varies] batch-submit these together
- **Product Hunt "AI" topic** — reuse the PH listing's AI tag for cross-discovery

### Finance / investing–specific communities & directories
- **r/investing, r/stocks, r/SecurityAnalysis, r/ValueInvesting, r/dividends** — [NF] discussion, not links, but the highest-intent audience (see Section 2 for rules)
- **Stocktwits** — https://stocktwits.com — [NF/disc] profile + posts; retail-investor audience
- **Bogleheads forum** — https://www.bogleheads.org — [varies] tool mentions tolerated if genuinely useful and disclosed; read forum rules
- **Hacker News** — https://news.ycombinator.com — [NF] Show HN (Section 4); nofollow but huge tech-savvy discovery
- **Indie Hackers** — https://www.indiehackers.com — [varies] post a build/launch story; founder audience
- **Fintech-focused directories / "best free stock screeners" roundup blogs** — pitch to be *added* to existing listicles (email the author — see Section 6); these are often the highest-value contextual, dofollow links
- **Financial-tools subreddits' wikis & "recommended tools" lists** — ask mods to add it where such a list exists
- **Quora / niche Q&A** — answer "best free stock research tool" / "how to find free cash flow yield" questions with a genuinely useful answer that links out — [NF] discovery
- **Wikipedia / Wikidata** — do **not** self-add; only relevant far later if independent coverage exists

### Practical order & pacing
1. Product Hunt + Show HN first (biggest one-time spikes; coordinate with Sections 1 & 4).
2. AlternativeTo, SaaSHub, Startup Ranking next (the best relevant dofollow links).
3. AI-tool directories in a batch (reuse the same copy).
4. Finance communities last and slowest — those need real participation, not drops.
- Submit **a few per day over 2–3 weeks**, not all at once. Keep name, tagline, and description
  consistent across listings (entity consistency helps AI/knowledge-graph pickup).

---

## 6. Blogger / newsletter outreach email

Short, specific, no pressure. Reference something they actually wrote. Send from a real address;
one follow-up max.

**Subject line options:**
- `A free, no-login stock tool for the cash-flow crowd`
- `Free cash-flow research tool — thought of your [newsletter/post]`
- `Quick one re: your piece on free stock screeners`

**Template:**
> Hi [Name],
>
> I read your [post/newsletter] on [specific topic — e.g. "free stock screeners" / "why FCF beats
> earnings"] and it's exactly the angle I care about, so I wanted to share something I built.
>
> I made **Nama Insights** (https://www.namainsights.com), a free, no-login research tool for
> US-listed stocks and ETFs. Its one real differentiator: it puts **free-cash-flow metrics** —
> FCF yield, price/FCF, operating cash flow — front and center, where most free tools bury or omit
> them. It also has earnings history, analyst coverage, revenue segments from SEC filings, a stock
> and ETF screener, and plain-English AI analysis. Everything refreshes daily.
>
> I'm not asking for a review or anything paid — just thought it might be genuinely useful to your
> readers, and if you ever update your "[free tools / screeners]" roundup, it'd be a fair fit.
> Happy to answer any questions or walk you through how the cash-flow numbers are sourced.
>
> Either way, thanks for the writing — it's good stuff.
>
> Best,
> [Your name]
> [Optional: one-line who-you-are]
>
> *(Nama is informational only, not investment advice.)*

**Outreach do's and don'ts:**
- **Do** personalize the first line — reference a specific post. Generic blasts get ignored/marked spam.
- **Do** make the ask soft and optional ("if you ever update your roundup").
- **Don't** attach anything, don't ask for a "dofollow link," don't send more than one follow-up.
- **Do** target writers who already publish "best free stock tools / screeners" listicles — the
  most natural, contextual placements.

---

*Keep every asset honest: free-cash-flow focus, daily-fresh data, AI analysis, no login — and
always "informational only, not investment advice." No hype, no guarantees, no price targets.*
