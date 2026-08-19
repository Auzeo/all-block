# Press images

The All Block card, for Reddit, Product Hunt, Hacker News, X, or anywhere else
that wants a picture with the link.

| File | Size | Use it for |
|---|---|---|
| `all-block-card-2400x1256.png` | 2400 × 1256 | **Start here.** Reddit image posts, X, LinkedIn, Discord. |
| `all-block-card-3600x1884.png` | 3600 × 1884 | Anywhere that might crop or zoom, or print. |
| `all-block-card-1200x628.png` | 1200 × 628 | Same file the site serves as its link preview. |

All three are re-rendered from the same source at their own resolution, not
upscaled — the type is sharp at every size.

## Changing the card

The source is [`../docs/og-card.html`](../docs/og-card.html). Edit the HTML,
then re-render at whichever scale you want:

```
msedge --headless=new --disable-gpu --hide-scrollbars \
       --force-device-scale-factor=2 --window-size=1200,628 \
       --screenshot="all-block-card-2400x1256.png" \
       "file:///path/to/docs/og-card.html"
```

`--force-device-scale-factor` is the multiplier: 1 gives 1200 × 628, 2 gives
2400 × 1256, 3 gives 3600 × 1884.

If you change the card, remember `docs/img/og-cover.png` is the 1× copy the
website's `og:image` points at — replace that one too, or the link preview and
the press images drift apart.
