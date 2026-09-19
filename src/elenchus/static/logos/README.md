# Partner logos

The strip of partner institutions on the sign-in page, the home page and
the participant welcome / thank-you screens is driven by
[`../partners.json`](../partners.json). No code change is needed to add,
remove, reorder or re-label a partner.

## Adding a logo

1. Get the file **from the organisation** — their official asset, used
   with their permission and within their brand guidelines. Don't redraw,
   recolour, crop or screenshot one.
2. Put it in this directory. **SVG** if they have one; otherwise a PNG at
   least 200 px tall with a transparent background. Prefer the horizontal
   ("lockup") version, and the full-colour one meant for light
   backgrounds — logos are shown on a white plate so they read correctly
   in both the light and the dark theme.
3. In `partners.json`, set that partner's `"logo"` to the file name, e.g.
   `"logo": "indelab.svg"`.

While `"logo"` is `null` the partner is shown as a plain text link, so
the strip is always presentable.

## Fields

| Field | |
|---|---|
| `label` (top level) | Optional small heading above the strip, e.g. `"A collaboration of"`. Leave `""` for none. The wording states a relationship — agree it with the partners. |
| `name` | Short name; shown when there is no logo. |
| `title` | Full name; the logo's alt text and the link's tooltip. |
| `url` | Where the logo links to. Must be `https://`. |
| `logo` | File name in this directory, or `null`. |

`tests/test_partners.py` checks the file is well-formed and that every
logo it names exists. These two paths are deliberately not cached by the
service worker, so an edit shows up on the next page load.
