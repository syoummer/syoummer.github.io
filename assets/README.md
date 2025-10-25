# Assets

`assets/people/` stores curated celebrity portraits for `people.html`. Add optimized `.jpg`/`.webp` files here and update the `CELEBRITY_ASSETS` map inside `people.html` so the game can load them without hitting Wikipedia.

For each new asset, capture:
1. `path`: relative path such as `assets/people/jackson-yee.webp`.
2. `sourceLabel`: short credit, e.g. `Wikipedia Commons` or photographer name.
3. `sourceUrl`: canonical page for attribution.

Keep files under ~400 KB to avoid slowing down the quiz on mobile networks.
