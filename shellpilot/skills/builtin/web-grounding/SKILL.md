---
name: web-grounding
description: Guidance for grounding answers in approved web sources.
---
Web tools available does not mean use web. Use them only for current, external, or source-backed information that local context cannot answer.

Search snippets are leads, not evidence. Before asserting a factual, current, or numeric claim (versions, prices, release details, exact figures), fetch the official source with web_fetch and ground the answer in its text. If snippets disagree or a name or number looks unfamiliar, fetch the authoritative page before committing — don't repeat an unverified figure, and don't assume the version or name in the question is current; confirm the generation from the source.

Shape the first query to find the source: lead with `<entity> <fact> official`, `<entity> documentation`, or `<entity> latest release`; don't guess a `site:` filter up front. For a comparison or a question spanning several entities, run a separate search per entity rather than one combined query.

Fetch a specific page, not a homepage — prefer a `/releases`, `/docs`, or `/changelog` URL, and fetch only URLs from the search results rather than inventing one. If a fetch is blocked or fails (403/404), search again for another authoritative source instead of guessing; if a page is truncated or lacks the answer, fetch a more specific URL.

Always cite sources for web-derived claims. Network calls require approval.
